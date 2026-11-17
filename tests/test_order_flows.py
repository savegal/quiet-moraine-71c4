import asyncio
from base64 import urlsafe_b64encode
from datetime import datetime, timedelta
import json

import pytest

from proxima_bot.api import TelegramError
from proxima_bot.catalog import MODES
from proxima_bot.config import Config
from proxima_bot.operations import load_spec, valid_body
from proxima_bot.worker import Worker

from test_extended import FakeAPI, button_callback, new_app, collected_cid
from test_worker import FakeTokora
from test_api_coverage import sample


VIDEO_ID = '7420000000000000000'
COMMENT_ID = '7520000000000000000'
VIDEO = f'https://www.tiktok.com/@author/video/{VIDEO_ID}'
COMMENT = f'{VIDEO}?comment_id={COMMENT_ID}'
REPLY_TARGET = f'{VIDEO_ID}:{COMMENT_ID}'
OWNER = 99
MEMBER = 7
SCENARIOS = {
    'publish': ([VIDEO, 'Текст комментария', 'Текст ответа'], {
        'awemeIds': [VIDEO_ID],
        'assets': {'commentTexts': ['Текст комментария'], 'replyTexts': ['Текст ответа']},
    }),
    'reply_links': ([COMMENT, 'Текст ответа'], {
        'replyLinks': [REPLY_TARGET], 'assets': {'replyTexts': ['Текст ответа']},
    }),
    'fetch': (['https://vt.tiktok.com/example/', '100'], {
        'awemeIds': [VIDEO_ID],
        'settings': {'fetchCount': 100, 'collectAll': False, 'includeReplies': False,'delaySeconds':2,'uniqueText':False,'fetchKeywords':[]},
    }),
    'commentlike': ([COMMENT, '3'], {'likeLinks': [COMMENT], 'likesPerComment': 3}),
    'keyword_reply': ([VIDEO, 'Тестовая фраза', 'Текст ответа'], {
        'awemeIds': [VIDEO_ID], 'settings': {
            'fetchKeywords':['Тестовая фраза'],'fetchCount':100,'collectAll':False,
            'commentsPerVideo':0,
            'repliesPerComment':3,'repliesPerVideo':30000,'likesPerComment':0,'likesPerReply':10,
            'uniqueText':False,'delaySeconds':2,
            'likeEnabled':True,'likeComments':False,'likeReplies':True,
        },
        'assets': {'replyTexts': ['Текст ответа']},
    }),
    'feedwarm': (['обзор телефона\nсравнение камер'], {
        'feedWarm': {'topics':['обзор телефона','сравнение камер'],'topTag':True,'phraseSearch':True,
                     'searchLimit':100,'periodDays':7,
                     'minLikes':0,'minComments':0,'like':False,'comment':False,'dryRun':True,'search':True,'noFeed':True},
    }),
    'usernames': ([VIDEO_ID], {'awemeIds': [VIDEO_ID]}),
}


class OrderProvider(FakeTokora, FakeAPI):
    def __init__(self):
        FakeTokora.__init__(self)
        FakeAPI.__init__(self)
        self.entry = {'kind': 'video', 'outcome': 'matched', 'url': VIDEO, 'text': VIDEO}

    async def call(self, method, path, **kwargs):
        if path.endswith('/reply-links/resolve'):
            self.calls.append((method, path, kwargs))
            return {'items': [{'value': REPLY_TARGET}], 'ok': 1, 'failed': 0}
        if path.endswith('/entries'):
            return {'items': [self.entry], 'total': 1, 'more': False}
        return await super().call(method, path, **kwargs)

    async def publication(self, upstream_id):
        return {'comments': {'published': 1, 'attempts': 1}, 'likes': {'confirmed': 3, 'total': 3}}


def test_commentlike_accepts_video_link_with_explicit_comment_id():
    async def run():
        app=new_app()
        try:
            await app.begin(MEMBER,'commentlike')
            await app.input_draft(MEMBER,'Лайки на комментарий')
            await app.input_draft(MEMBER,VIDEO)
            assert app.db.draft(MEMBER)['step']=='like_links'
            await app.input_draft(MEMBER,VIDEO+' '+COMMENT_ID)
            await app.input_draft(MEMBER,'3')
            await click(app,MEMBER,'wiz:quick:')
            await click(app,MEMBER,'wiz:submit:')
            task,=app.db.recent_tasks(MEMBER)
            assert json.loads(task['payload_json'])['likeLinks']==[REPLY_TARGET]
        finally:
            app.db.close()
    asyncio.run(run())


async def click(app, uid, prefix):
    await app.callback(uid, {'data': button_callback(app, prefix)})


def test_owner_sets_late_confirmation_window_via_settings_screen():
    async def run():
        app=new_app()
        await app.callback(MEMBER,{'data':'owner_edit:settlement'})
        assert app.db.draft(MEMBER) is None
        await app.settings(OWNER)
        await click(app,OWNER,'owner_edit:settlement')
        await app.input_draft(OWNER,'15')
        assert app.api.calls[-1]==('PATCH','/v1/settings',{'payload':{'settleMinutes':15},'retries':0})

        await app.owner_edit(OWNER,'settings')
        await app.input_draft(OWNER,'{"settleMinutes":"15"}')
        assert len(app.api.calls)==1
        assert app.db.draft(OWNER) is None
        await app.input_draft(OWNER,'{"settleMinutes":20,"uniqueText":true}')
        assert len(app.api.calls)==1
        app.db.close()

    asyncio.run(run())


@pytest.mark.parametrize('actor,remote_state,expected_state',[(MEMBER,'stopped','stopped'),(OWNER,'running','stopping')])
def test_stop_uses_api_snapshot_on_member_and_owner_cards(actor,remote_state,expected_state):
    async def run():
        app=new_app()
        tid=app.db.queue_task(MEMBER,'Заказ','publish',{'assets':{'accountIds':['acc7']}})
        app.db.transition(tid,'running',upstream_id='external')
        calls=[]

        async def stop(upstream_id):
            calls.append(upstream_id)
            return {'id':upstream_id,'state':remote_state,'done':2,'total':4,
                    'finishedAt':'2026-11-16T16:00:00Z' if remote_state=='stopped' else ''}

        app.api.stop=stop
        if actor==OWNER:
            await app.callback(actor,{'data':'adm:jobedit:stop:'+tid})
            await click(app,actor,'adm:jobapply:')
        else:
            await app.callback(actor,{'data':'stopconfirm:'+tid})
            await click(app,actor,'stop:')
        assert calls==['external']
        assert app.db.task(tid)['state']==expected_state
        assert '50%' in app.tg.out[-1][1]
        app.db.close()

    asyncio.run(run())


def test_owner_can_check_all_accounts_without_personal_assignments():
    async def run():
        app=new_app()
        assert app.db.account_ids(OWNER)==[]
        await app.callback(MEMBER,{'data':'new:check'})
        await app.callback(MEMBER,{'data':'owner_go:accounts_check'})
        assert app.api.calls==[]
        await app.create(OWNER)
        await click(app,OWNER,'new:check')
        assert app.api.calls[-1][:2]==('GET','/v1/accounts/check')
        await click(app,OWNER,'owner_go:accounts_check')
        assert app.api.calls[-1][:2]==('POST','/v1/accounts/check')
        assert app.db.recent_tasks(OWNER)==[]
        app.db.close()

    asyncio.run(run())


def test_owner_can_attach_photos_and_use_cabinet_defaults():
    async def run():
        app=new_app()
        app.db.assign(OWNER,'owner-account')
        await app.begin(OWNER,'publish')
        await click(app,OWNER,'wiz:auto_name:')
        for value in (VIDEO_ID,'Комментарий','Ответ'):
            await app.input_draft(OWNER,value)
        await click(app,OWNER,'wiz:quick:')
        await click(app,OWNER,'wiz:photo_both:')
        await click(app,OWNER,'wiz:submit:')
        task,=app.db.recent_tasks(OWNER)
        payload=json.loads(task['payload_json'])
        assert payload['usePhoto'] is True
        assert payload['disableCommentPhotos'] is False and payload['disableReplyPhotos'] is False
        assert payload['settings']['commentsPerVideo']==50
        assert payload['settings']['repliesPerComment']==3
        assert payload['settings']['likeEnabled'] is True
        assert task['fire_at']==0
        app.db.close()

    asyncio.run(run())


def test_member_cannot_enable_shared_photos_from_callback():
    async def run():
        app=new_app()
        await app.begin(MEMBER,'publish')
        for value in ('Тест',VIDEO_ID,'Текст','Ответ'):
            await app.input_draft(MEMBER,value)
        await click(app,MEMBER,'wiz:quick:')
        token=app.db.draft(MEMBER)['data']['wizard_token']
        await app.callback(MEMBER,{'data':'wiz:photo_both:'+token})
        assert 'photo_mode' not in app.db.draft(MEMBER)['data']
        await click(app,MEMBER,'wiz:submit:')
        task,=app.db.recent_tasks(MEMBER)
        assert json.loads(task['payload_json'])['usePhoto'] is False
        app.db.close()

    asyncio.run(run())


def test_owner_can_attach_photos_to_replies_from_collected_comments():
    async def run():
        app=new_app()
        app.db.assign(OWNER,'owner-account')
        source=app.db.queue_task(OWNER,'Сбор','fetch',{'assets':{'accountIds':['owner-account']}})
        app.db.transition(source,'completed',upstream_id='source')
        await app.callback(OWNER,{'data':'source_reply:'+source})
        await app.input_draft(OWNER,'100')
        await app.input_draft(OWNER,'Ответ')
        await click(app,OWNER,'source:skip:')
        await click(app,OWNER,'source:photo:')
        assert app.db.draft(OWNER)['data']['photo_mode']=='replies'
        await click(app,OWNER,'source:submit:')
        task=app.db.recent_tasks(OWNER)[0]
        payload=json.loads(task['payload_json'])
        assert payload['sourceJob']=='source' and payload['targetIds']==[collected_cid()]
        assert payload['usePhoto'] is True
        assert payload['disableCommentPhotos'] is True and payload['disableReplyPhotos'] is False
        app.db.close()

    asyncio.run(run())


@pytest.mark.parametrize('mode', [mode for mode in MODES if mode!='check'])
def test_each_order_from_menu_through_template_dispatch_and_results(mode):
    async def run():
        app = new_app()
        app.api = OrderProvider()
        if mode == 'fetch':
            app.api.entry = {'kind': 'comment', 'outcome': 'collected', 'text': 'Текст из TikTok'}
        if mode == 'usernames':
            app.api.entry = {'kind': 'username', 'outcome': 'found', 'text': 'telegram_user',
                             'mentions': 2, 'author': 'tiktok.author'}
        if mode == 'commentlike':
            app.api.entry = {'kind': 'like', 'outcome': 'partial', 'goal': 3, 'confirmed': 2, 'failed': 1}
        app.cfg = Config('fake', 'fake', OWNER, dispatch_enabled=True)
        uid = MEMBER

        await app.create(uid)
        await click(app, uid, 'new:' + mode)
        inputs, expected = SCENARIOS[mode]
        assert app.db.draft(uid)['step']!='name'
        for value in inputs:
            await app.input_draft(uid, value)
        if mode == 'fetch':
            await click(app, uid, 'wiz:fetch_without_replies:')
            await click(app, uid, 'wiz:skip:')
        await click(app, uid, 'wiz:skip:')
        await click(app, uid, 'wiz:skip:')
        assert app.db.draft(uid)['step'] == 'review'
        assert 'Проверка заказа' in app.tg.out[-1][1]
        if mode == 'fetch':
            assert 'До 100' in app.tg.out[-1][1]
            assert 'Пропустить' in app.tg.out[-1][1]

        await click(app, uid, 'wiz:template:')
        await click(app, uid, 'lt:save:')
        template, = app.db.templates(uid)
        assert template['mode'] == mode
        assert app.db.template(8, template['template_id']) is None
        await click(app, uid, 'templates')
        await click(app, uid, 'tpl:')
        await click(app, uid, 'tpl_use:')
        if mode!='feedwarm':
            await app.input_draft(uid, COMMENT if mode in ('reply_links','commentlike') else VIDEO)
        await click(app, uid, 'wiz:quick:')
        assert not app.db.recent_tasks(uid)
        confirm = button_callback(app, 'wiz:submit:')
        await app.callback(uid, {'data': confirm})
        await app.callback(uid, {'data': confirm})
        task, = app.db.recent_tasks(uid)
        assert task['state'] == 'queued' and app.db.draft(uid) is None

        worker = Worker(app.db, app.api, app.tg, app.cfg)
        await worker.tick()
        sent, = app.api.started
        assert valid_body(load_spec()['components']['schemas']['TaskStart'], sent)
        assert sent['kind'] == ('publish' if mode == 'reply_links' else mode)
        assert sent['assets']['accountIds'] == app.db.account_ids(uid)
        for key, value in expected.items():
            if key == 'assets':
                assert {field: sent[key][field] for field in value} == value
            else:
                assert sent[key] == value
        app.api.final['job1'] = {'id':'job1','state':'completed','finishedAt':'2026-11-16T16:00:00Z',
                                 'done':1,'total':1,'liked':3}
        await worker.tick()
        await app.task(uid, task['task_id'])
        assert 'Завершено' in app.tg.out[-1][1]
        await click(app, uid, 'entries:')
        assert 'Результаты' in app.tg.out[-1][1]
        if mode == 'usernames':
            assert '@telegram_user' in app.tg.out[-1][1]
            assert 'tiktok.author' not in app.tg.out[-1][1]
        if mode == 'commentlike':
            assert '2 / 3' in app.tg.out[-1][1]
            await click(app, uid, 'exports:')
            await click(app, uid, 'export_csv:')
            assert b'goal,failed' in app.tg.documents[-1][1]
            await app.task_entries(uid, task['task_id'])
        if mode == 'feedwarm':
            assert VIDEO in app.tg.out[-1][1]
        if mode in ('publish', 'reply_links', 'keyword_reply', 'commentlike'):
            await click(app, uid, 'report:')
            assert 'Отчёт' in app.tg.out[-1][1]
        else:
            await click(app, uid, 'exports:')
            await click(app, uid, 'export_csv:')
            assert b'entry' not in app.tg.documents[-1][1]
            if mode == 'feedwarm':
                assert VIDEO.encode() in app.tg.documents[-1][1]
            if mode == 'usernames':
                assert b'telegram_user' in app.tg.documents[-1][1]
                assert b'tiktok.author' not in app.tg.documents[-1][1]
            if mode == 'fetch':
                assert 'Текст из TikTok'.encode() in app.tg.documents[-1][1]
                await app.exports(uid, task['task_id'])
                await click(app, uid, 'export_texts:')
                assert app.tg.documents[-1][1] == 'Текст из TikTok\n'.encode()
                assert app.tg.documents[-1][2].endswith('_texts.txt')
        app.db.close()

    asyncio.run(run())


def test_publication_links_use_tokora_download_without_exposing_extra_query_fields():
    async def run():
        app = new_app()
        task = app.db.queue_task(MEMBER, 'Публикация', 'publish', {'assets': {'accountIds': ['acc7']}})
        app.db.transition(task, 'completed', upstream_id='post-job')
        cid = urlsafe_b64encode(COMMENT_ID.encode()).decode().rstrip('=')
        link = f'https://www.tiktok.com/share/video/{VIDEO_ID}?cid={cid}'

        async def download(path):
            assert path == '/v1/tasks/post-job/links'
            content = f'Заголовок\n{link}&token=private\n{link}\nhttps://tokora.online/private\n'
            return content.encode(), 'text/plain'

        async def entries(*args, **kwargs):
            raise AssertionError('Publication links come from the separate download')

        app.api.download = download
        app.api.call = entries
        try:
            await app.callback(MEMBER, {'data': 'export_links:' + task})
            document, = app.tg.documents
            assert document[1] == (link+'\n').encode()
        finally:
            app.db.close()

    asyncio.run(run())


def test_source_template_selects_own_collection_and_excludes_old_source():
    async def run():
        app = new_app()
        old = app.db.queue_task(MEMBER, 'Первый сбор', 'fetch', {'assets': {'accountIds': ['acc7']}})
        new = app.db.queue_task(MEMBER, 'Второй сбор', 'fetch', {'assets': {'accountIds': ['acc7']}})
        other = app.db.queue_task(8, 'Чужой сбор', 'fetch', {'assets': {'accountIds': ['acc8']}})
        for task_id, provider_id in [(old, 'old'), (new, 'new'), (other, 'other')]:
            app.db.transition(task_id, 'completed', upstream_id=provider_id)
        await app.callback(MEMBER, {'data': 'source_reply:' + old})
        await app.input_draft(MEMBER,'100')
        await app.input_draft(MEMBER, 'Сохранённый текст')
        await click(app, MEMBER, 'source:skip:')
        await click(app, MEMBER, 'source:template:')
        await click(app, MEMBER, 'lt:save:')
        template, = app.db.templates(MEMBER)
        tid = template['template_id']
        saved=json.loads(app.db.template(MEMBER,tid)['payload'])
        assert saved['replies']==['Сохранённый текст']
        assert saved['options']['repliesPerComment']==3 and saved['options']['likeEnabled'] is True
        await app.from_template(MEMBER, tid)
        assert 'source_tpl_use:' + tid + ':' + new == button_callback(app, 'source_tpl_use:')
        await app.callback(MEMBER, {'data': f'source_tpl_use:{tid}:{other}'})
        assert 'недоступен' in app.tg.out[-1][1]
        await app.callback(MEMBER, {'data': f'source_tpl_use:{tid}:{new}'})
        await app.input_draft(MEMBER,'100')
        await click(app,MEMBER,'source:skip:')
        confirm = button_callback(app, 'source:submit:')
        await app.callback(MEMBER, {'data': confirm})
        await app.callback(MEMBER, {'data': confirm})
        payload = json.loads(app.db.recent_tasks(MEMBER)[0]['payload_json'])
        assert len(app.db.recent_tasks(MEMBER)) == 3
        assert payload['targetIds'] == [collected_cid()]
        assert any(method == 'GET' and path == '/v1/tasks/new/entries' for method, path, _ in app.api.calls)
        assert payload['assets']['replyTexts'] == ['Сохранённый текст']
        app.db.close()

    asyncio.run(run())


def test_template_last_page_is_accessible_and_deletes_its_own_template():
    async def run():
        app = new_app()
        ids = [app.db.save_template(MEMBER, f'Шаблон {n}', 'fetch', {}) for n in range(50)]
        await app.templates(MEMBER)
        for page in range(1, 5):
            await click(app, MEMBER, 'templates_page:' + str(page))
        await click(app, MEMBER, 'tpl:' + ids[0])
        await click(app, MEMBER, 'tpl_more:')
        await click(app, MEMBER, 'tpl_delete_confirm:')
        await click(app, MEMBER, 'tpl_delete:')
        assert app.db.template(MEMBER, ids[0]) is None
        assert len(app.db.templates(MEMBER)) == 49
        app.db.close()

    asyncio.run(run())


def test_scheduled_order_waits_until_its_selected_time(monkeypatch):
    async def run():
        app = new_app()
        api = OrderProvider()
        await app.begin(MEMBER, 'fetch')
        await app.input_draft(MEMBER, 'Запланированный сбор')
        await app.input_draft(MEMBER, VIDEO_ID)
        await app.input_draft(MEMBER, '100')
        await click(app, MEMBER, 'wiz:fetch_with_replies:')
        await click(app, MEMBER, 'wiz:skip:')
        await click(app, MEMBER, 'wiz:skip:')
        when = datetime.now(app.schedule_zone) + timedelta(hours=3)
        await app.input_draft(MEMBER, when.strftime('%d.%m.%Y %H:%M'))
        await click(app, MEMBER, 'wiz:edit_options:')
        await app.input_draft(MEMBER, '{"fetchWorkers":2}')
        assert app.db.draft(MEMBER)['step'] == 'options'
        await click(app, MEMBER, 'wiz:quick:')
        await click(app, MEMBER, 'wiz:submit:')
        task, = app.db.recent_tasks(MEMBER)
        assert task['fire_at'] == int(when.replace(second=0, microsecond=0).timestamp())
        worker = Worker(app.db, api, app.tg, Config('fake', 'fake', OWNER, dispatch_enabled=True))
        await worker.tick()
        assert not api.started
        monkeypatch.setattr('proxima_bot.db.now', lambda: task['fire_at'])
        await worker.tick()
        assert len(api.started) == 1
        assert api.started[0]['settings'] == {
            'fetchCount': 100, 'collectAll': False, 'includeReplies': True,'delaySeconds':2,'uniqueText':False,'fetchKeywords':[],
        }
        app.db.close()

    asyncio.run(run())


def test_older_keyword_template_keeps_texts_and_asks_for_video():
    async def run():
        app = new_app()
        try:
            template = app.db.save_template(MEMBER, 'Старый шаблон', 'keyword_reply', {
                'draft': {'name': 'Шаблон', 'keywords': ['поиск'], 'replies': ['Ответ']},
            })
            await app.from_template(MEMBER, template)
            assert app.db.draft(MEMBER)['step'] == 'videos'
            await app.input_draft(MEMBER, VIDEO)
            await click(app,MEMBER,'wiz:quick:')
            assert app.db.draft(MEMBER)['step'] == 'review'
            await click(app, MEMBER, 'wiz:submit:')
            task, = app.db.recent_tasks(MEMBER)
            body = json.loads(task['payload_json'])
            assert body['awemeIds'] == [VIDEO_ID]
            assert body['settings']['fetchKeywords'] == ['поиск']
            assert body['assets']['replyTexts'] == ['Ответ']
        finally:
            app.db.close()

    asyncio.run(run())


def test_collection_can_switch_between_100_and_1000_from_review():
    async def run():
        app=new_app()
        try:
            await app.begin(MEMBER,'fetch')
            await app.input_draft(MEMBER,'Комментарии')
            await app.input_draft(MEMBER,VIDEO_ID)
            for invalid in ('0','75','all','1001'):
                await app.input_draft(MEMBER,invalid)
                assert app.db.draft(MEMBER)['step']=='fetch_count'
            await click(app,MEMBER,'wiz:fetch_1000:')
            await click(app,MEMBER,'wiz:fetch_without_replies:')
            await click(app,MEMBER,'wiz:skip:')
            await click(app,MEMBER,'wiz:quick:')
            old_submit=button_callback(app,'wiz:submit:')
            await click(app,MEMBER,'wiz:fetch_edit:')
            await app.input_draft(MEMBER,'100')
            await click(app,MEMBER,'wiz:fetch_without_replies:')
            await click(app,MEMBER,'wiz:skip:')
            await click(app,MEMBER,'wiz:quick:')
            await app.callback(MEMBER,{'data':old_submit})
            assert app.db.recent_tasks(MEMBER)==[]
            await app.callback(MEMBER,{'data':'wiz_continue'})
            await click(app,MEMBER,'wiz:submit:')
            assert json.loads(app.db.recent_tasks(MEMBER)[0]['payload_json'])['settings']['fetchCount']==100
        finally:app.db.close()
    asyncio.run(run())


@pytest.mark.parametrize(('options','expected'), [
    ({}, {}),
    ({'fetchCount': 80}, {'fetchCount': 80, 'collectAll': False}),
    ({'fetchCount': 80, 'collectAll': True}, {'fetchCount': 80, 'collectAll': True}),
])
def test_collection_keeps_defaults_or_settings_from_existing_draft(options, expected):
    async def run():
        app = new_app()
        app.save_wizard_step(MEMBER, 'fetch', 'review', {
            'name': 'Старый сбор', 'videos': [VIDEO_ID], 'options': options,
        })
        await app.show_wizard(MEMBER)
        await click(app, MEMBER, 'wiz:submit:')
        task, = app.db.recent_tasks(MEMBER)
        assert json.loads(task['payload_json'])['settings']=={'fetchCount':100,'collectAll':False,'includeReplies':False,'delaySeconds':2,'uniqueText':False,'fetchKeywords':[]}
        app.db.close()

    asyncio.run(run())


@pytest.mark.parametrize('mode', ['reply', 'like'])
def test_topup_only_launches_once_for_each_confirmation(mode):
    async def run():
        app = new_app()
        api = OrderProvider()
        app.api = api
        source = app.db.queue_task(MEMBER, 'Публикация', 'publish',
                                   {'assets': {'accountIds': ['acc7'], 'replyTexts': ['Ответ']}})
        app.db.transition(source, 'completed', upstream_id='source')

        async def topup_info(method, path, **kwargs):
            assert method == 'GET' and path == f'/v1/tasks/source/{mode}-topup'
            return {'available': True, 'likesPerItem': 0, 'likesPerReply': 2,
                    'failedComments': 1, 'repliesPerComment': 1}

        api.call = topup_info
        await app.task(MEMBER, source)
        await click(app, MEMBER, mode + 'info:')
        confirm = button_callback(app, 'topup:')
        await app.callback(MEMBER, {'data': confirm})
        worker = Worker(app.db, api, app.tg, Config('fake', 'fake', OWNER, dispatch_enabled=True))
        await worker.tick()
        sent, = api.started
        if mode == 'like':
            assert sent['likeSourceJob'] == 'source' and sent['settings']['likesPerReply'] == 2
        else:
            assert sent['sourceJob'] == 'source' and sent['assets']['replyTexts'] == ['Ответ']
        api.final['job1'] = {'id':'job1','state':'completed','finishedAt':'2026-11-16T16:00:00Z'}
        await worker.tick()
        await app.callback(MEMBER, {'data': confirm})
        assert 'Подтверждение устарело' in app.tg.out[-1][1]
        assert len(app.db.recent_tasks(MEMBER)) == 2
        app.db.close()

    asyncio.run(run())


@pytest.mark.parametrize(('kind','mime'), [('video','application/octet-stream'), ('photo','image/png'),
                                          ('photo','image/webp')])
def test_owner_upload_caption_and_publish_plan(kind,mime):
    async def run():
        app = new_app()
        calls = []
        description = {}
        photos = []
        plan_state = 'checking'
        draft_path = '/v1/posts/drafts/media'

        async def fetch_upload(file_id, limit):
            if kind == 'video':
                return b'\x00\x00\x00\x14ftypisom0000', 'file.mp4'
            if mime == 'image/webp':
                return b'RIFF\x12\x00\x00\x00WEBPVP8L\x00', 'file.webp'
            return b'\x89PNG\r\n\x1a\n' + b'png data', 'file.png'

        async def upload(path, content, content_type='application/octet-stream', *, params=None):
            calls.append(('UPLOAD', path))
            assert content_type == mime
            if kind == 'video':
                assert path == '/v1/posts/drafts/upload' and params == {'kind': 'video', 'name': 'test..mp4'}
            else:
                assert path == draft_path + '/photos'
                photos.append({'id': f'photo-{len(photos)}'})
            return {'id': 'media', 'kind': kind, 'photos': photos}

        async def provider(method, path, **kwargs):
            nonlocal description
            calls.append((method, path))
            if path == '/v1/posts/drafts' and method == 'POST':
                assert kwargs['payload'] == {'kind': 'photo', 'name': 'Альбом'}
                return {'id': 'media', 'kind': 'photo'}
            if path == '/v1/posts/templates':
                if method == 'POST':
                    assert kwargs['payload']['name'] == 'Тест'
                    return {'id': 'caption'}
                return {'items': [{'id': 'caption', 'name': 'Тест', 'description': 'Текст', 'hashtags': '#tag'}]}
            if path == draft_path:
                if method == 'PATCH':
                    description = kwargs['payload']
                return {'id': 'media', 'kind': kind, 'photos': photos, **description}
            if path == '/v1/posts/plan':
                assert kwargs['payload']['draftIds']==['media']
                assert kwargs['payload']['settings']['perAccount']==1
                return {'id': 'plan', 'state': plan_state, 'postsTotal': 1}
            if path=='/v1/posts/settings':
                return sample(load_spec()['components']['schemas']['PostSettings'])
            if path == '/v1/posts/plan/plan':
                return {'id': 'plan', 'state': plan_state, 'postsTotal': 1}
            if path == '/v1/posts/plan/plan/launch':
                return {'planId': 'plan', 'queued': 1}
            if path == '/v1/posts':
                return {'items': [{'id': 'published_item', 'outcome': 'queued'}], 'total': 1}
            raise AssertionError(path)

        app.api.call = provider
        app.api.upload = upload
        app.tg.fetch_upload = fetch_upload
        await app.callback(MEMBER, {'data': 'draft_plan:media'})
        assert calls == []
        await app.posts(OWNER)
        if kind == 'video':
            await click(app, OWNER, 'upload:video')
            await app.handle({'message': {'chat': {'type': 'private', 'id': OWNER}, 'from': {'id': OWNER},
                                          'video': {'file_id': 'video', 'file_name': 'test..mp4'}}})
        else:
            await click(app, OWNER, 'owner_edit:album')
            await app.input_draft(OWNER, 'Альбом')
            assert app.db.draft(OWNER)['step'] == 'album_media'
            for number in range(2):
                assert await app.handle_upload(OWNER, {'photo': [{'file_id': f'photo-{number}'}]})
            await click(app, OWNER, 'draft:media')
            assert 'Фото в альбоме' in app.tg.out[-1][1] and len(photos) == 2
            assert app.db.draft(OWNER) is None
        await app.callback(OWNER, {'data': 'owner_edit:post_template'})
        await app.input_draft(OWNER, '{"name":"Тест","description":"Текст","hashtags":"#tag"}')
        await app.callback(OWNER, {'data': 'draft:media'})
        await click(app, OWNER, 'caption_pick:')
        await click(app, OWNER, 'caption_preview:')
        assert not description
        await click(app, OWNER, 'caption_apply:')
        assert description == {'description': 'Текст', 'hashtags': '#tag', 'ownText': True}
        await click(app, OWNER, 'draft_plan:')
        assert 'Публикаций на аккаунт' in app.tg.out[-1][1]
        await click(app,OWNER,'mo:prepare:')
        assert 'План публикаций' in app.tg.out[-1][1]
        assert not any(b['callback_data'].startswith('plan_confirm:')
                       for row in app.tg.out[-1][2]['inline_keyboard'] for b in row)
        plan_state = 'ready'
        await click(app, OWNER, 'plan:')
        await click(app, OWNER, 'plan_confirm:')
        assert '<b>1</b> публикаций' in app.tg.out[-1][1]
        assert ('POST', '/v1/posts/plan/plan/launch') not in calls
        stale_confirm = button_callback(app, 'owner_do:plan_launch:')
        plan_state = 'expired'
        await app.callback(OWNER, {'data': stale_confirm})
        assert ('POST', '/v1/posts/plan/plan/launch') not in calls
        plan_state = 'ready'
        await click(app, OWNER, 'plan:')
        await click(app, OWNER, 'plan_confirm:')
        await click(app, OWNER, 'owner_do:plan_launch:')
        await click(app, OWNER, 'owner_show:post_queue')
        assert 'published_item' in app.tg.out[-1][1]
        app.db.close()

    asyncio.run(run())


def test_invalid_or_incomplete_reply_inputs_keep_the_draft_and_allow_retry():
    async def run():
        app = new_app()
        await app.begin(MEMBER, 'reply_links')
        await app.input_draft(MEMBER, 'Повтор ввода')
        await app.input_draft(MEMBER, 'https://not-tiktok.example/')
        assert app.db.draft(MEMBER)['step'] == 'reply_links'

        async def upload(file_id, limit):
            if file_id == 'unavailable':
                raise TelegramError(503)
            return (COMMENT + '\n' + REPLY_TARGET).encode(), 'input.txt'

        app.tg.fetch_upload = upload
        for file_id in ('unavailable', 'ok'):
            await app.handle_upload(MEMBER, {'document': {'file_id': file_id, 'file_name': 'input.txt'}})
            assert app.db.draft(MEMBER)['step'] == ('reply_links' if file_id == 'unavailable' else 'replies')
        await app.input_draft(MEMBER, 'Текст')
        await click(app, MEMBER, 'wiz:skip:')
        await click(app, MEMBER, 'wiz:skip:')
        await click(app, MEMBER, 'wiz:submit:')
        assert app.db.draft(MEMBER) is None
        task,=app.db.recent_tasks(MEMBER)
        assert task['state']=='queued'
        assert json.loads(task['payload_json'])['replyLinks']==[COMMENT,REPLY_TARGET]
        assert not any(path.endswith('/reply-links/resolve') for _,path,_ in app.api.calls)
        app.db.close()

    asyncio.run(run())
