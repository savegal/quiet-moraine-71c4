import asyncio
from contextlib import closing

import pytest

from proxima_bot.api import ApiError, Tokora, scan_active_tasks
from proxima_bot.config import Config
from proxima_bot.db import Store
from proxima_bot.worker import Worker
from test_extended import button_callback, new_app
from test_worker import FINISHED_AT, FakeTelegram, FakeTokora


def test_pool_stays_reserved_until_the_provider_confirms_finish():
    async def run():
        with closing(Store(':memory:')) as db:
            db.grant(7,30)
            db.assign(7,'a')
            first=db.queue_task(7,'Первый','publish',{'assets':{'accountIds':['a']}})
            second=db.queue_task(7,'Второй','publish',{'assets':{'accountIds':['a']}})
            api=FakeTokora()
            worker=Worker(db,api,FakeTelegram(),Config('fake','fake',99,dispatch_enabled=True))
            await worker.tick()
            api.final['job1']={'id':'job1','state':'running','stopRequested':True}
            await worker.tick()
            assert db.task(first)['state']=='stopping'
            api.final['job1']={'id':'job1','state':'stopped','finishedAt':''}
            await worker.tick()
            assert db.task(first)['state']=='stopping'
            assert db.task(second)['state']=='queued' and len(api.started)==1
            api.final['job1']['finishedAt']=FINISHED_AT
            await worker.refresh()
            assert db.task(first)['state']=='stopped'
            assert db.next_eligible(99)['task_id']==second
    asyncio.run(run())


def test_active_scan_preserves_paused_and_unconfirmed_finish_but_releases_interrupted():
    async def run():
        class Provider:
            async def call(self,method,path,**kwargs):
                assert method=='GET' and path=='/v1/tasks'
                return {'items':[
                    {'id':'done','state':'interrupted','finishedAt':FINISHED_AT},
                    {'id':'finishing','state':'completed','finishedAt':''},
                    {'id':'paused','state':'paused'},
                ],'more':False}
        assert [job['id'] for job in await scan_active_tasks(Provider())]==['finishing','paused']
    asyncio.run(run())


@pytest.mark.parametrize(('code','state'),[('job_busy','queued'),('job_duplicate','failed')])
def test_import_does_not_take_a_slot_and_conflicts_have_distinct_outcomes(code,state):
    async def run():
        with closing(Store(':memory:')) as db:
            db.grant(7,30)
            db.assign(7,'a')
            task=db.queue_task(7,'Заказ','fetch',{'assets':{'accountIds':['a']}})
            api=FakeTokora()
            api.active=[{'id':'remote-import','kind':'import','state':'running'}]
            api.issue=ApiError(code,409)
            worker=Worker(db,api,FakeTelegram(),Config('fake','fake',99,dispatch_enabled=True,max_active_external=1))
            await worker.tick()
            assert len(api.started)==1
            saved=db.task(task)
            assert saved['state']==state
            assert saved['error_code']==('job_duplicate' if state=='failed' else '')
            assert not saved['upstream_id']
    asyncio.run(run())


def test_owner_can_read_current_task_pool_with_pagination_without_exposing_secrets():
    async def run():
        app=new_app()
        api=Tokora(None,'https://example.test','unused')
        calls=[]
        async def call(method,path,**kwargs):
            assert method=='GET'
            calls.append((path,kwargs.get('params')))
            if path=='/v1/tasks/site-task':
                return {'id':'site-task','name':'Заказ с сайта','kind':'publish','state':'paused',
                        'accountsAssigned':8,'accountsWorking':3}
            assert path=='/v1/accounts'
            params=kwargs['params']
            assert params['task']=='site-task' and 'folder' not in params
            offset,size=params['offset'],params['limit']
            return {'total':14,'accounts':[
                {'accountId':f'account-{i}','profile':{'username':f'user_{i}'},'status':'active',
                 'proxy':'secret-proxy','cookies':'secret-session'}
                for i in range(offset,min(14,offset+size))]}
        api.call=call
        app.api=api
        try:
            await app.callback(7,{'data':'task_accounts:site-task:0'})
            assert not calls
            await app.workspace_task(99,'site-task')
            await app.callback(99,{'data':button_callback(app,'task_accounts:site-task:0')})
            text=app.tg.out[-1][1]
            assert 'текущий состав пула' in text and 'Назначено при запуске' in text and 'В работе сейчас' in text
            assert 'account-0' in text and 'secret-' not in text
            await app.callback(99,{'data':button_callback(app,'task_accounts:site-task:1')})
            assert calls[-1]==('/v1/accounts',{'task':'site-task','offset':12,'limit':12})
            assert 'account-13' in app.tg.out[-1][1] and 'account-0' not in app.tg.out[-1][1]
        finally:app.db.close()
    asyncio.run(run())


def test_pause_during_folder_preflight_prevents_launch():
    async def run():
        with closing(Store(':memory:')) as db:
            class Provider(FakeTokora):
                async def accounts_in_folder(self,*args,**kwargs):
                    accounts=await super().accounts_in_folder(*args,**kwargs)
                    db.set_dispatch_paused(True)
                    return accounts

            db.grant(7,30)
            db.assign(7,'a')
            task_id=db.queue_task(7,'Заказ','fetch',{'assets':{'accountIds':['a']}})
            api=Provider()
            worker=Worker(db,api,FakeTelegram(),Config('fake','fake',99,dispatch_enabled=True))
            await worker.tick()
            assert db.task(task_id)['state']=='queued'
            assert not api.started
    asyncio.run(run())


def test_photo_permission_revoked_during_preflight_prevents_launch():
    async def run():
        app=new_app()
        db=app.db
        try:
            db.toggle_photo_access(7,99)
            task_id=db.queue_task(7,'С фото','publish',{'assets':{'accountIds':['acc7']},'usePhoto':True})

            class Provider(FakeTokora):
                async def accounts_in_folder(self,*args,**kwargs):
                    accounts=await super().accounts_in_folder(*args,**kwargs)
                    db.toggle_photo_access(7,99)
                    return accounts

            api=Provider()
            worker=Worker(db,api,FakeTelegram(),Config('fake','fake',99,dispatch_enabled=True))
            await worker.tick()
            assert not db.can_use_photos(7) and not api.started
            await worker.tick()
            assert db.task(task_id)['state']=='failed' and not api.started
        finally:
            db.close()
    asyncio.run(run())
