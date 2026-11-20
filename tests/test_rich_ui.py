import asyncio
import json
from html.parser import HTMLParser

import pytest

from proxima_bot.api import ApiError, TelegramError
from proxima_bot.catalog import MODES, OWNER_ONLY
from proxima_bot.db import now
from proxima_bot.rich_ui import button_row, screen
from proxima_bot.order_templates import template_screen
from test_extended import approved_order_data, new_app, seed_approved_template
from test_guided_order_form import click, options
from test_order_flows import COMMENT, SCENARIOS
from test_release_flows import TelegramTransport


class NativeRich(HTMLParser):
    """Validate the documented subset used by these screens, including callbacks."""
    allowed={'h2','h3','p','b','i','code','footer','br','hr','table','tr','td','th',
             'details','summary','tg-button-row','tg-button','tg-time','ul','li','ol','blockquote','a'}

    def __init__(self):
        super().__init__()
        self.stack=[]
        self.callbacks=set()
        self.headings=0
        self.row_buttons=[]

    def handle_starttag(self, tag, attrs):
        assert tag in self.allowed, tag
        attrs=dict(attrs)
        assert not any(key.startswith('on') or key in ('class','id') for key in attrs)
        if tag!='tg-button':assert 'style' not in attrs
        if tag=='h2':self.headings+=1
        if tag=='tg-button-row':self.row_buttons.append(0)
        if tag=='tg-button':
            assert attrs['type']=='callback_data'
            assert attrs.get('style') in (None,'primary','success','danger','link')
            assert 1<=len(attrs['data'].encode('utf-8'))<=64
            self.callbacks.add(attrs['data'])
            if self.row_buttons:self.row_buttons[-1]+=1
        if tag not in ('br','hr'):self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop()==tag
        if tag=='tg-button-row':assert 1<=self.row_buttons.pop()<=8


def validate_rich(payload):
    parser=NativeRich()
    body=payload['rich_message']['html']
    parser.feed(body)
    assert not parser.stack
    assert parser.headings==1
    assert len(body)<32768
    assert set(payload['rich_message'])=={'html','skip_entity_detection'}
    return parser.callbacks|keyboard_callbacks(payload)


def keyboard_callbacks(payload):
    return {button['callback_data'] for row in payload.get('reply_markup',{}).get('inline_keyboard',[]) for button in row}


def test_live_local_order_does_not_overwrite_navigation():
    class SlowEditTransport(TelegramTransport):
        def __init__(self):
            super().__init__()
            self.edit_listeners = []
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def edit_rich(self, *args):
            self.started.set()
            await self.release.wait()
            return await super().edit_rich(*args)

    async def run():
        app = new_app()
        transport = app.tg = SlowEditTransport()
        running = []
        try:
            order = app.db.queue_task(7, 'Сбор', 'fetch', {'assets': {'accountIds': ['acc7']}})
            await app.task(7, order)
            message_id = app.live_cards.cards[7].message_id
            app.db.transition(order, 'running', upstream_id='order-id')
            refresh = asyncio.create_task(app.live_cards.tick())
            running.append(refresh)
            await asyncio.wait_for(transport.started.wait(), timeout=2)
            token = transport.edit_in_place(7, message_id)
            try:
                navigate = asyncio.create_task(app.callback(7, {'data': 'tasks', 'message': {'message_id': message_id}}))
                running.append(navigate)
                await asyncio.sleep(0)
                assert not navigate.done()
                transport.release.set()
                await asyncio.wait_for(asyncio.gather(refresh, navigate), timeout=2)
            finally:
                transport.reset_edit(token)
            edits = [payload for method, payload in transport.calls if method == 'editMessageText']
            assert len(edits) == 2
            assert 'Мои задачи' in edits[-1]['rich_message']['html']
            assert not app.live_cards.cards
        finally:
            transport.release.set()
            await asyncio.gather(*running, return_exceptions=True)
            app.db.close()

    asyncio.run(run())


@pytest.mark.parametrize('uid', [7,99])
@pytest.mark.parametrize('rich', [True,False])
def test_create_catalog_preserves_all_actions_in_native_and_basic_layouts(uid,rich):
    async def run():
        app=new_app()
        try:
            app.tg=TelegramTransport(rich)
            app.save_wizard_step(uid,'publish','name',{})
            await app.create(uid)
            method,payload=app.tg.calls[-1]
            actions=validate_rich(payload) if rich else keyboard_callbacks(payload)
            assert {item for item in actions if item.startswith('new:')}=={
                'new:'+mode for mode in MODES if uid==99 or mode not in OWNER_ONLY}
            assert {'wiz_continue','templates','home'}<=actions
            if rich:
                body=payload['rich_message']['html']
                assert body.index('wiz_continue') < body.index('new:publish')
            if uid==99:assert {'upload:video','owner_edit:album'}<=actions
            assert method==('sendRichMessage' if rich else 'sendMessage')
        finally:app.db.close()
    asyncio.run(run())


@pytest.mark.parametrize('mode', list(SCENARIOS))
def test_order_volume_and_review_are_valid_native_rich_with_all_editors(mode):
    async def run():
        app=new_app()
        try:
            await options(app,mode)
            app.tg=TelegramTransport()
            await app.show_wizard(7)
            payload=app.tg.calls[-1][1]
            actions=validate_rich(payload)
            token=app.db.draft(7)['data']['wizard_token']
            assert 'wiz:quick:'+token in actions
            assert '<b>Объём</b>' in payload['rich_message']['html']
            await app.callback(7,{'data':'wiz:quick:'+token})
            payload=app.tg.calls[-1][1]
            actions=validate_rich(payload)
            token=app.db.draft(7)['data']['wizard_token']
            assert {'wiz:submit:'+token,'wiz:edit_options:'+token,'wiz:back:'+token}<=actions
            assert '<b>Проверка</b>' in payload['rich_message']['html']
            assert '10 минут' in payload['rich_message']['html']
        finally:app.db.close()
    asyncio.run(run())


@pytest.mark.parametrize('rich', [True,False])
def test_scheduled_order_keeps_launch_notice_and_actions_in_both_layouts(rich):
    async def run():
        app=new_app()
        try:
            fire_at=now()+3600
            app.save_wizard_step(7,'publish','review',approved_order_data(app,7,'publish',{
                'name':'Запланированный заказ','videos':['7420000000000000000'],
                'comments':['Текст'],'fire_at':fire_at,
            }))
            app.tg=TelegramTransport(rich)
            await app.preview(7)
            payload=app.tg.calls[-1][1]
            actions=validate_rich(payload) if rich else keyboard_callbacks(payload)
            text=payload['rich_message']['html'] if rich else payload['text']
            assert text.index('Запуск очереди приостановлен') < text.index('Вид заказа')
            assert 'не раньше' in text and app.format_date(fire_at) in text
            assert (f'<tg-time unix="{fire_at}" format="dt">' in text)==rich
            submit=next(action for action in actions if action.startswith('wiz:submit:'))
            await app.callback(7,{'data':submit})
            task,=app.db.recent_tasks(7)
            assert task['fire_at']==fire_at
            card,plain,_=app.render_task(task)
            assert f'<tg-time unix="{fire_at}"' in card
            assert all(view.index('Запуск приостановлен') < view.index('Вид заказа') for view in (card,plain))
            assert 'Ожидание свободной очереди' not in card
            earlier=app.db.queue_task(7,'Более ранний заказ','fetch',{'assets':{'accountIds':['acc7']}},fire_at=fire_at-1800)
            await app.schedules(7)
            payload=app.tg.calls[-1][1]
            actions=validate_rich(payload) if rich else keyboard_callbacks(payload)
            text=payload['rich_message']['html'] if rich else payload['text']
            assert text.index('Более ранний заказ') < text.index('Запланированный заказ')
            assert {'task:'+earlier,'task:'+task['task_id']}<=actions
        finally:app.db.close()
    asyncio.run(run())


@pytest.mark.parametrize('rich', [True,False])
def test_template_list_keeps_navigation_and_escaped_names(rich):
    async def run():
        app=new_app()
        try:
            tids=[seed_approved_template(app,7,'Название <>& '+str(i),'publish',{'draft':{'replies':['Ответ']}}) for i in range(12)]
            tids=[item['template_id'] for item in app.db.templates(7)]
            app.tg=TelegramTransport(rich)
            await app.templates(7)
            payload=app.tg.calls[-1][1]
            actions=validate_rich(payload) if rich else keyboard_callbacks(payload)
            assert {'tpl:'+tid for tid in tids[:10]}<=actions
            assert not {'tpl:'+tid for tid in tids[10:]}&actions
            assert {'templates_page:1','tpl_new','create','tools'}<=actions
            body=payload['rich_message']['html'] if rich else payload['text']
            assert 'Название &lt;&gt;&amp;' in body
        finally:app.db.close()
    asyncio.run(run())


@pytest.mark.parametrize('likes', [5,10,15,20])
def test_comment_like_presets_create_the_requested_volume_without_typing(likes):
    async def run():
        app=new_app()
        try:
            await app.callback(7,{'data':'new:commentlike'})
            await app.input_draft(7,COMMENT)
            assert app.db.draft(7)['step']=='likes'
            await click(app,'wiz:likes_'+str(likes)+':')
            assert app.db.draft(7)['step']=='options'
            await click(app,'wiz:quick:')
            await click(app,'wiz:submit:')
            assert json.loads(app.db.recent_tasks(7)[0]['payload_json'])['likesPerComment']==likes
        finally:app.db.close()
    asyncio.run(run())


def test_rich_in_place_failure_edits_basic_message_with_every_quantity_control():
    class UnsupportedRich(TelegramTransport):
        def __init__(self):
            super().__init__()
            self.edit_listeners=[]
        async def call(self,method,data,**kwargs):
            if method=='editMessageText' and 'rich_message' in data:
                self.calls.append((method,data))
                raise TelegramError(400,'Rich messages are not supported')
            return await super().call(method,data,**kwargs)
    async def run():
        app=new_app()
        try:
            await options(app,'publish')
            app.tg=UnsupportedRich()
            target=app.tg.edit_in_place(7,901)
            try:await app.show_wizard(7)
            finally:app.tg.reset_edit(target)
            assert [method for method,_ in app.tg.calls]==['editMessageText','editMessageText']
            payload=app.tg.calls[-1][1]
            assert payload['message_id']==901
            assert 'rich_message' not in payload
            assert '50' in payload['text'] and '15' in payload['text']
            assert len([cb for cb in keyboard_callbacks(payload) if cb.startswith('wiz:form_')])==4
        finally:app.db.close()
    asyncio.run(run())


def test_screen_and_buttons_escape_content_and_enforce_documented_callback_bytes():
    rich,plain=screen('Имя <>&', [('Поле','<script>')],stage=2)
    assert '<script>' not in rich and '<script>' not in plain
    assert '&lt;script&gt;' in rich
    assert '&lt;&gt;&amp;' in rich
    assert 'data="&quot;&amp;"' in button_row(('<>&','"&','link'))
    button_row(('64 bytes','я'*32,None))
    with pytest.raises(ValueError):button_row(('66 bytes','я'*33,None))
    with pytest.raises(ValueError):button_row(*[('one','cb',None)]*9)


def test_deferred_template_volume_does_not_advertise_old_cached_settings():
    rich,plain=template_screen('Шаблон','publish',{
        'comments':['Главный'],'replies':['Ответ'], 'options':{'commentsPerVideo':75,'repliesPerComment':5},
        'ask_fields':['options'], 'form_ready':True})
    assert 'Выбор при запуске' in rich and 'Выбор при запуске' in plain
    assert 'Сохранённый объём' not in rich and 'Сохранённый объём' not in plain
    assert '75' not in rich and '75' not in plain


def test_restricted_modes_hide_and_block_member_access_but_keep_owner_access():
    async def run():
        app=new_app()
        app.cfg=type(app.cfg)(**{**app.cfg.__dict__,'restricted_modes':('fetch','feedwarm')})
        try:
            app.tg=TelegramTransport(True)
            await app.show_home(7)
            home_actions=validate_rich(app.tg.calls[-1][1])
            assert {'new:fetch','new:feedwarm'}.isdisjoint(home_actions)
            assert 'new:commentlike' in home_actions

            await app.create(7)
            create_actions=validate_rich(app.tg.calls[-1][1])
            assert {'new:fetch','new:feedwarm'}.isdisjoint(create_actions)
            assert {'new:publish','new:commentlike','new:usernames'}<=create_actions

            await app.begin(7,'fetch')
            await app.begin(7,'feedwarm')
            assert app.db.draft(7) is None

            await app.template_menu(7)
            tpl_actions=validate_rich(app.tg.calls[-1][1])
            assert {'tpl_new:fetch','tpl_new:feedwarm'}.isdisjoint(tpl_actions)
            await app.edit_local_template(7,'fetch')
            assert app.db.draft(7) is None

            tid=seed_approved_template(app,7,'Старый парсер','fetch',{'draft':{'fetch_count':100}})
            await app.from_template(7,tid)
            assert app.db.draft(7) is None

            job=app.db.queue_task(7,'Старый сбор','fetch',{'assets':{'accountIds':['acc7']},'awemeIds':['7420000000000000000']})
            await app.prepare_copy(7,json.loads(app.db.task(job)['payload_json']),'Копия')
            assert app.db.draft(7) is None

            await app.create(99)
            owner_actions=validate_rich(app.tg.calls[-1][1])
            assert {'new:fetch','new:feedwarm'}<=owner_actions
            await app.begin(99,'fetch')
            assert app.db.draft(99) and app.db.draft(99)['mode']=='fetch'
        finally:app.db.close()
    asyncio.run(run())


def test_execution_phases_dynamic_limit_and_comment_check_note():
    from proxima_bot.presentation import COMMENT_CHECK_NOTE, report_view, task_rich, task_view

    base_task={'task_id':'abc12345','display_name':'Тест','kind':'publish','state':'running',
               'payload_json':json.dumps({'awemeIds':['1','2']}),'snapshot_json':json.dumps({'phase':'prepare','attempts':0}),
               'error_code':'','created_at':1000,'execution_started_at':0}
    prep_card=task_view(base_task)
    prep_rich=task_rich(base_task)
    assert 'Подготовка аккаунтов' in prep_card
    assert 'Подготовка аккаунтов' in prep_rich
    assert '20 минут' in prep_card and '20 минут' in prep_rich
    assert COMMENT_CHECK_NOTE in prep_card
    assert COMMENT_CHECK_NOTE in prep_rich.split('<details>')[0]

    pub_task={**base_task,'payload_json':json.dumps({'awemeIds':['1']}),
              'snapshot_json':json.dumps({'phase':'publish','attempts':2,'published':1}),'execution_started_at':1100}
    pub_card=task_view(pub_task)
    pub_rich=task_rich(pub_task)
    assert 'Публикации начались' in pub_card and 'Публикации начались' in pub_rich
    assert '10 минут' in pub_card and '10 минут' in pub_rich
    report_rich, report_plain = report_view(pub_task, {'comments': {'published': 1, 'attempts': 2}, 'replies': {}})
    assert COMMENT_CHECK_NOTE in report_rich and COMMENT_CHECK_NOTE in report_plain


def test_admin_queue_and_folder_sync_screens_are_valid_native_rich():
    async def run():
        app=new_app()
        app.tg=TelegramTransport(True)
        tid=app.db.queue_task(7,'Заказ <тест>','publish',{'assets':{'accountIds':['acc7']},'awemeIds':['1']})
        app.db.transition(tid,'running',upstream_id='remote1',snapshot={'phase':'prepare','attempts':0})

        async def api_call(method,path,**kwargs):
            if path=='/v1/account-folders':
                return {'available':True,'unassigned':1,'items':[{'id':'1234abcd','name':'Папка <A>','count':2,'active':2}]}
            if method=='GET' and path=='/v1/tasks':
                return {'items':[{'id':'remote1','state':'running'},{'id':'external','state':'paused'},
                                 {'id':'import','state':'running','kind':'import'}],'more':False}
            raise AssertionError(path)

        async def in_folder(fid):
            return {'f1':{'accountId':'f1','status':'active','folderId':fid}}

        app.api.call=api_call
        app.api.accounts_in_folder=in_folder
        try:
            await app.callback(99,{'data':'admin'})
            assert {'adm:liveq:0','ac:folders:0'}<=validate_rich(app.tg.calls[-1][1])
            await app.callback(99,{'data':'adm:liveq:0'})
            q_actions=validate_rich(app.tg.calls[-1][1])
            assert {'adm:lqpause:on',f'adm:job:{tid}'}<=q_actions
            queue_html=app.tg.calls[-1][1]['rich_message']['html']
            assert 'Подготовка' in queue_html
            assert 'Лимит слотов бота · <b>5</b>' in queue_html
            assert 'Занято всего · <b>2</b>' in queue_html and 'Свободно по лимиту · <b>3</b>' in queue_html
            assert 'Другие задачи кабинета · <b>1</b>' in queue_html
            await app.callback(99,{'data':'adm:foldersync:7'})
            fs_actions=validate_rich(app.tg.calls[-1][1])
            assert 'adm:fsp:7:1234abcd' in fs_actions
            await app.callback(99,{'data':'adm:fsp:7:1234abcd'})
            prev_actions=validate_rich(app.tg.calls[-1][1])
            assert {'adm:fsg:7:1234abcd:a','adm:fsg:7:1234abcd:e'}<=prev_actions
            await app.callback(99,{'data':'adm:fsg:7:1234abcd:a'})
            validate_rich(app.tg.calls[-1][1])

            from test_account_import_ui import account_rows, document
            app.tg.fetch_upload=lambda fid,limit: asyncio.sleep(0,(account_rows('one'),'accs.txt'))
            await app.callback(99,{'data':'acctimp:start:7'})
            await app.handle(document('f1'))
            imp_preview=app.tg.calls[-1][1]
            imp_actions=validate_rich(imp_preview)
            folders_cb=next(c for c in imp_actions if c.startswith('acctimp:folders:'))
            await app.callback(99,{'data':folders_cb})
            validate_rich(app.tg.calls[-1][1])
        finally:app.db.close()

    asyncio.run(run())


def test_admin_queue_retains_local_counts_and_order_actions_without_api_or_rich():
    async def run():
        app=new_app()
        app.tg=TelegramTransport(False)

        async def unavailable(method,path,**kwargs):
            assert (method,path)==('GET','/v1/tasks')
            raise ApiError('connection_unavailable',503)

        app.api.call=unavailable
        try:
            payload={'assets':{'accountIds':['acc7']}}
            running=app.db.queue_task(7,'Текущий заказ','fetch',payload)
            waiting=app.db.queue_task(7,'Тот же пул','fetch',payload)
            app.db.transition(running,'running',upstream_id='remote1')
            await app.callback(99,{'data':'adm:liveq:0'})
            method,response=app.tg.calls[-1]
            assert method=='sendMessage'
            assert {'adm:liveq:0','adm:lqpause:on',f'adm:job:{running}',f'adm:job:{waiting}'}<=keyboard_callbacks(response)
            assert 'Показаны только заказы бота' in response['text']
            assert 'Занято заказами бота · <b>1</b>' in response['text']
            assert 'Ожидают запуска · <b>1</b>' in response['text']
            assert 'Свободно по лимиту' not in response['text']
            assert '\nВ очереди' in response['text']
            assert 'Ждёт освобождения аккаунтов' not in response['text']
        finally:app.db.close()

    asyncio.run(run())
