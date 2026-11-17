import asyncio
import json

import aiohttp
from aiohttp import web

from proxima_bot.api import ApiError, Tokora
from proxima_bot.config import Config
from proxima_bot.db import Store
from proxima_bot.presentation import task_view
from proxima_bot.worker import Worker

from test_extended import new_app
from test_release_flows import TelegramTransport
from test_worker import FakeTelegram, FakeTokora


def queued_order(db, ids):
    db.grant(7,30)
    for aid in ids:db.assign(7,aid)
    return db.queue_task(7,'Заказ','publish',{'assets':{'accountIds':ids}})


def test_preflight_reserves_the_entire_verified_folder_and_freezes_payload(tmp_path):
    async def run():
        location=str(tmp_path/'bot.sqlite3')
        cfg=Config('fake','fake',99,dispatch_enabled=True)
        db=Store(location)
        ids=['expired','active','unknown','error']
        task_id=queued_order(db,ids)
        api=FakeTokora()
        api.sessions={key:{'accountId':key,'status':key} for key in ids}
        api.issue=ApiError('connection_unavailable')
        await Worker(db,api,FakeTelegram(),cfg).tick()
        job=db.task(task_id)
        assert job['state']=='reconcile'
        original_payload,=api.started
        assert original_payload['assets']['accountIds']==ids
        assert original_payload['accountFolder'] in api.folders
        assert json.loads(job['payload_json'])['assets']['accountIds']==ids
        assert json.loads(job['launch_payload_json'])==original_payload
        stage=json.loads(job['preflight_json'])
        assert stage=={'phase':'checked','requested':4,'active':1,'expired':1,'error':1,'unknown':1,'missing':0}
        assert 'Без активной сессии' in task_view(job)
        db.close()

        db=Store(location)
        api.issue=None
        api.sessions={}
        await Worker(db,api,FakeTelegram(),cfg).tick()
        assert db.task(task_id)['state']=='running'
        assert api.account_reads==[ids]
        assert api.started==[original_payload,original_payload]
        db.close()

    asyncio.run(run())


def test_no_active_sessions_means_no_remote_start():
    async def run():
        db=Store(':memory:')
        task_id=queued_order(db,['dead'])
        api=FakeTokora()
        api.sessions={'dead':{'accountId':'dead','status':'expired'}}
        worker=Worker(db,api,FakeTelegram(),Config('fake','fake',99,dispatch_enabled=True))
        await worker.tick()
        assert db.task(task_id)['state']=='failed'
        assert db.task(task_id)['error_code']=='no_active_accounts'
        assert api.started==[]
        db.close()

    asyncio.run(run())


def test_api_read_failure_waits_and_preserves_queue_until_it_recovers():
    async def run():
        db=Store(':memory:')
        task_id=queued_order(db,['good'])
        api=FakeTokora()
        api.sessions=ApiError('rate_limited',429,60)
        worker=Worker(db,api,FakeTelegram(),Config('fake','fake',99,dispatch_enabled=True))
        await worker.tick()
        job=db.task(task_id)
        assert job['state']=='queued' and job['launch_payload_json'] is None
        assert job['error_code']=='account_check_unavailable'
        assert api.started==[]
        api.sessions=None
        db.conn.execute('UPDATE tasks SET target_retry_at=0 WHERE task_id=?',(task_id,))
        await worker.tick()
        assert db.task(task_id)['state']=='running'
        assert len(api.started)==1
        db.close()

    asyncio.run(run())


def test_cancel_during_account_preflight_does_not_start_order():
    async def run():
        db=Store(':memory:')
        task_id=queued_order(db,['good'])
        api=FakeTokora()

        async def accounts_for(ids):
            assert db.cancel_queued(task_id,7)
            return {'good':{'accountId':'good','status':'active'}}

        api.accounts_for=accounts_for
        await Worker(db,api,FakeTelegram(),Config('fake','fake',99,dispatch_enabled=True)).tick()
        assert db.task(task_id)['state']=='canceled' and api.started==[]
        db.close()

    asyncio.run(run())


def test_preflight_and_launch_use_real_http_contract():
    async def run():
        calls=[]

        async def provider(request):
            calls.append((request.method,request.path,request.query.get('folder')))
            if request.path=='/v1/accounts':
                assert request.query['limit']=='200' and request.query['offset']=='0'
                return web.json_response({'accounts':[{'accountId':'alive','status':'active','folderId':'1234abcd'},
                                                     {'accountId':'dead','status':'expired','folderId':'1234abcd'}],'total':2})
            if request.method=='GET':
                assert request.query.get('active')=='true'
                return web.json_response({'items':[]})
            payload=await request.json()
            assert payload['accountFolder']=='1234abcd' and 'assets' not in payload
            assert payload['requestId']
            return web.json_response({'id':'upstream','state':'running'},status=201)

        server=web.Application()
        server.router.add_route('*','/{tail:.*}',provider)
        runner=web.AppRunner(server)
        await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0)
        await site.start()
        port=runner.addresses[0][1]
        db=Store(':memory:')
        try:
            task_id=queued_order(db,['alive','dead'])
            async with aiohttp.ClientSession() as session:
                tokora=Tokora(session,f'http://127.0.0.1:{port}','unit-test')
                await Worker(db,tokora,FakeTelegram(),Config('fake','fake',99,dispatch_enabled=True)).tick()
            assert db.task(task_id)['state']=='running'
            assert calls==[('GET','/v1/tasks',None),('GET','/v1/accounts',None),
                           ('GET','/v1/accounts','1234abcd'),('POST','/v1/tasks',None)]
        finally:
            db.close()
            await runner.cleanup()

    asyncio.run(run())


def test_folder_with_foreign_account_waits_and_other_folder_can_start():
    async def run():
        with_db=Store(':memory:')
        try:
            first=queued_order(with_db,['first'])
            second=queued_order(with_db,['second'])

            class Provider(FakeTokora):
                async def accounts_in_folder(self,folder,*,expected_count):
                    rows=await super().accounts_in_folder(folder,expected_count=expected_count)
                    if 'first' in rows:
                        return {'foreign':{'accountId':'foreign','folderId':folder,'status':'active'}}
                    return rows

            api=Provider()
            worker=Worker(with_db,api,FakeTelegram(),Config('fake','fake',99,dispatch_enabled=True))
            await worker.tick()
            assert with_db.task(first)['error_code']=='account_folder_mismatch'
            assert with_db.task(first)['state']=='queued' and not api.started
            await worker.tick()
            assert with_db.task(second)['state']=='running'
        finally:
            with_db.close()

    asyncio.run(run())


def test_account_check_updates_in_place_using_tokora_progress():
    async def run():
        app=new_app()
        app.tg=TelegramTransport()
        calls=[]
        statuses=[
            {'state':'idle','total':0,'checked':0},
            {'state':'running','total':4,'checked':0,'valid':0,'expired':0,'failed':0,'waiting':4},
            {'state':'running','total':4,'checked':2,'valid':1,'expired':1,'failed':0,'waiting':2},
            {'state':'finished','total':4,'checked':4,'valid':2,'expired':1,'failed':1,'waiting':0},
        ]

        async def provider(method,path,**kwargs):
            calls.append((method,path))
            if path=='/v1/tasks':
                assert kwargs['params']['active']=='true'
                return {'items':[]}
            return statuses.pop(0)

        app.api.call=provider
        await app.callback(7,{'data':'owner_go:accounts_check'})
        assert calls==[]
        await app.callback(99,{'data':'owner_go:accounts_check'})
        message_id=app.live_cards.cards[99].message_id
        await app.live_cards.tick()
        await app.live_cards.tick()
        assert calls==[('GET','/v1/accounts/check'),('GET','/v1/tasks'),('POST','/v1/accounts/check'),
                       ('GET','/v1/accounts/check'),('GET','/v1/accounts/check')]
        assert not app.db.account_check_pending()
        edits=[body for method,body in app.tg.calls if method=='editMessageText']
        assert [item['message_id'] for item in edits]==[message_id,message_id]
        assert 'Завершена' in edits[-1]['rich_message']['html']
        assert 'Сессии истекли' in edits[-1]['rich_message']['html']
        assert not app.live_cards.cards
        app.db.close()

    asyncio.run(run())


def test_owner_sees_each_member_pool_with_exact_status_counts():
    async def run():
        app=new_app()
        app.db.assign(7,'dead')
        app.db.assign(7,'missing')

        async def page(offset,limit=200,**kwargs):
            return {'accounts':[{'accountId':'acc7','status':'active','profile':{'username':'first'}},
                                {'accountId':'dead','status':'expired'},
                                {'accountId':'acc8','status':'active','profile':{'username':'other'}}],
                    'total':3}

        app.api.account_page=page
        await app.callback(99,{'data':'adm:uaccounts:7'})
        text=app.tg.out[-1][1]
        assert '@first' in text and 'Сессия истекла' in text and '@other' not in text
        await app.callback(99,{'data':'adm:pool:7'})
        text=app.tg.out[-1][1]
        assert 'Активны' in text and 'Не найдены' in text
        total=len(app.tg.out)
        await app.callback(7,{'data':'adm:pool:8'})
        assert len(app.tg.out)==total
        app.db.close()

    asyncio.run(run())
