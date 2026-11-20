"""The owner can use the linked cabinet while members keep their own pools."""
import asyncio
import json
from dataclasses import replace

from proxima_bot.worker import Worker
from test_extended import button_callback, new_app
from test_release_flows import TelegramTransport
from test_worker import FakeTokora


def test_workspace_stop_is_owner_only_confirmed_once_and_updates_local_order():
    async def run():
        app=new_app()
        remote={'id':'remote','name':'Заказ','kind':'publish','state':'running','published':17,
                'awemes':[{'comments':17,'replies':0}],'settings':{'commentsPerVideo':80,'repliesPerVideo':80}}
        writes=[]
        async def task(tid):return dict(remote)
        async def call(method,path,**kwargs):
            assert (method,path)==('POST','/v1/tasks/remote/stop')
            writes.append(path)
            remote.update(state='stopped',finishedAt='2026-11-16T16:00:00Z')
            return dict(remote)
        app.api.task=task
        app.api.call=call
        try:
            local=app.db.queue_task(7,'Полное название','publish',{'assets':{'accountIds':['acc7']}})
            app.db.transition(local,'running',upstream_id='remote')
            await app.callback(7,{'data':'wt:stopask:remote'})
            assert not writes and app.db.draft(7) is None
            await app.workspace_task(99,'remote')
            assert 'Комментарии' in app.tg.out[-1][1] and '17' in app.tg.out[-1][1]
            await app.callback(99,{'data':button_callback(app,'wt:stopask:')})
            assert not writes
            confirm=button_callback(app,'wt:stop:')
            await app.callback(99,{'data':confirm})
            await app.callback(99,{'data':confirm})
            assert len(writes)==1
            assert app.db.task(local)['state']=='stopped'
            assert 'wt:stopask:' not in str(app.render_workspace_task(await app.read_workspace_task('remote'))[2])
        finally:
            app.db.close()
    asyncio.run(run())


def test_workspace_names_hide_tracking_marker_and_restore_full_local_title():
    async def run():
        app=new_app()
        marker='[PX-c8d72ece612c40b0b782b36907754170]'
        remote={'id':'tracked','name':marker+' тест с бота','state':'running','kind':'publish'}
        async def page(offset,limit):
            return {'items':[remote],'more':False}
        async def task(tid):
            return remote
        app.api.task_page=page
        app.api.task=task
        try:
            await app.tasks(99)
            assert 'тест с бота' in app.tg.out[-1][1]
            assert marker not in str(app.tg.out[-1])
            card=await app.read_workspace_task('tracked')
            assert card['display_name']=='тест с бота'
            assert json.loads(card['snapshot_json'])['name']==remote['name']
            title='Полное длинное название которое пользователь ввёл сам'
            tid=app.db.queue_task(7,title,'publish',{'assets':{'accountIds':['acc7']}})
            app.db.transition(tid,'running',upstream_id='tracked')
            assert (await app.read_workspace_task('tracked'))['display_name']==title
        finally:
            app.db.close()
    asyncio.run(run())


def test_owner_views_website_tasks_and_local_queue_in_separate_tabs():
    async def run():
        app = new_app()
        app.db.assign(99,'owner-account')
        app.db.queue_task(99,'Локальный заказ','fetch',{'assets':{'accountIds':['owner-account']}})
        remote = {'id':'cabinet-job','name':'Задача <из сайта>','state':'running','kind':'publish','done':3,'total':10}
        reads = []

        async def page(offset,limit):
            reads.append(('page',offset,limit))
            return {'items':[remote] if offset==0 else [],'more':offset==0}

        async def task(tid):
            reads.append(('task',tid))
            return remote

        async def publication(tid):
            reads.append(('report',tid))
            return {'comments':{'published':3,'attempts':4},'replies':{}}

        app.api.task_page,app.api.task,app.api.publication = page,task,publication
        try:
            await app.callback(7,{'data':'tasks'})
            await app.callback(7,{'data':'tokora_task:cabinet-job'})
            await app.callback(7,{'data':'bot_tasks:0'})
            assert reads == []
            await app.callback(99,{'data':'tasks'})
            assert 'Задача &lt;из сайта&gt;' in app.tg.out[-1][1]
            await app.callback(99,{'data':button_callback(app,'tasks_page:1')})
            assert reads[-1] == ('page',10,10)
            await app.callback(99,{'data':button_callback(app,'bot_tasks:0')})
            assert 'Локальный заказ' in app.tg.out[-1][1]
            await app.callback(99,{'data':button_callback(app,'tasks')})
            await app.callback(99,{'data':button_callback(app,'cabinet_task:')})
            assert 'Все задачи' in app.tg.out[-1][1]
            assert 'tokora' not in app.tg.out[-1][1].casefold()
            await app.callback(99,{'data':button_callback(app,'cabinet_report:')})
            assert reads[-1] == ('report','cabinet-job')
            assert len(app.db.recent_tasks(99)) == 1
        finally:
            app.db.close()

    asyncio.run(run())


def test_owner_can_dispatch_all_workspace_accounts_including_traffer_and_unfoldered():
    async def run():
        app = new_app()
        app.cfg = replace(app.cfg,dispatch_enabled=True)
        calls = []
        rows = [
            {'accountId':'acc7','status':'active','folderId':'8765fedc'},
            {'accountId':'will-be-assigned','status':'active','folderId':'1234abcd'},
            {'accountId':'free','status':'active'},
            {'accountId':'expired','status':'expired','folderId':'8765fedc'},
        ]

        async def page(offset,limit,**kwargs):
            assert kwargs == {}
            calls.append(offset)
            return {'accounts':rows[offset:offset+2],'total':len(rows)}

        app.api.account_page = page
        try:
            await app.create(99)
            await app.callback(99,{'data':button_callback(app,'new:fetch')})
            await app.input_draft(99,'7420000000000000000')
            await app.input_draft(99,'100')
            await app.callback(99,{'data':button_callback(app,'wiz:fetch_without_replies:')})
            await app.callback(99,{'data':button_callback(app,'wiz:skip:')})
            await app.callback(99,{'data':button_callback(app,'wiz:quick:')})
            confirm = button_callback(app,'wiz:submit:')
            assert calls == [0,2]
            expected_ids = ['acc7','expired','free','will-be-assigned']
            assert app.db.draft(99)['data']['review_account_ids'] == expected_ids
            assert app.db.draft(99)['data']['review_account_folder'] == ''

            # Assigning an account to a traffer does not block the owner from submitting.
            app.db.assign(8,'will-be-assigned')
            await app.callback(99,{'data':confirm})
            job, = app.db.recent_tasks(99)
            payload = json.loads(job['payload_json'])
            assert payload['accountFolder'] == ''
            assert payload['assets']['accountIds'] == expected_ids
            assert app.db.account_ids(99) == []
            assert app.db.account_ids(7) == ['acc7']
            assert app.db.account_ids(8) == ['acc8','will-be-assigned']

            tokora = FakeTokora()
            tokora.folders[''] = {row['accountId']:row for row in rows}
            await Worker(app.db,tokora,app.tg,app.cfg).tick()
            assert tokora.started[0]['accountFolder'] == ''
            assert tokora.started[0]['assets']['accountIds'] == expected_ids
            assert app.db.task(job['task_id'])['state'] == 'running'
        finally:
            app.db.close()

    asyncio.run(run())


def test_website_task_card_refreshes_in_place_until_late_confirmation(monkeypatch):
    clock = [1_800_000_000]
    monkeypatch.setattr('proxima_bot.extended.now',lambda: clock[0])

    async def run():
        app = new_app()
        app.tg = TelegramTransport()
        snapshot = {'id':'site-job','name':'Задача с сайта','kind':'publish','state':'running','done':2,'total':10}
        reads = []

        async def read(tid):
            reads.append(tid)
            return snapshot.copy()

        app.api.task = read
        try:
            await app.callback(7,{'data':'tokora_task:site-job'})
            assert not reads
            await app.callback(99,{'data':'tokora_task:site-job'})
            card = app.live_cards.cards[99]
            assert 'примерно каждые 5 секунд' in card.view[1]
            await app.live_cards.tick()
            assert len(app.tg.calls)==1

            snapshot.update(done=6)
            await app.live_cards.tick()
            assert '60%' in card.view[1]
            snapshot.update(state='completed_with_errors',done=10,finishedAt='2026-11-16T16:00:00Z',settleUntil=(clock[0]+5)*1000)
            await app.live_cards.tick()
            assert 'Поздняя проверка публикаций' in card.view[1]
            assert app.live_cards.cards[99] is card

            clock[0]+=5
            snapshot.update(lateConfirmed=1,published=5)
            await app.live_cards.tick()
            assert 'Завершено с ошибками' in card.view[1] and 'Поздние подтверждения' in card.view[1]
            assert not app.live_cards.cards
            await app.live_cards.tick()
            assert reads == ['site-job']*5
            edits = [body for method,body in app.tg.calls if method=='editMessageText']
            assert [body['message_id'] for body in edits] == [card.message_id]*3
            assert not app.db.recent_tasks(99)
        finally:
            app.db.close()

    asyncio.run(run())


def test_leaving_website_task_discards_an_in_flight_status_read():
    async def run():
        app = new_app()
        app.tg = TelegramTransport()
        entered = asyncio.Event()
        release = asyncio.Event()
        snapshot = {'id':'site-job','name':'Сайт','kind':'fetch','state':'running','done':0,'total':5}

        async def read(tid):
            if not entered.is_set() and app.live_cards.cards:
                entered.set()
                await release.wait()
            return snapshot.copy()

        async def page(offset,limit):
            return {'items':[snapshot],'more':False}

        app.api.task,app.api.task_page = read,page
        refresh = None
        try:
            await app.callback(99,{'data':'tokora_task:site-job'})
            mid = app.live_cards.cards[99].message_id
            snapshot['done'] = 2
            refresh = asyncio.create_task(app.live_cards.tick())
            await asyncio.wait_for(entered.wait(),timeout=2)
            await app.callback(99,{'data':'tasks','message':{'message_id':mid}})
            release.set()
            await refresh
            assert not app.live_cards.cards
            assert [method for method,_ in app.tg.calls] == ['sendRichMessage','sendRichMessage']
        finally:
            release.set()
            if refresh:
                await refresh
            app.db.close()

    asyncio.run(run())
