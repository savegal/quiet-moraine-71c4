import asyncio
import json
import pytest
from proxima_bot.config import Config
from proxima_bot.db import Store
from proxima_bot.worker import RECONCILE_RETRY_LIMIT, Worker
from proxima_bot.api import ApiError

class FakeTelegram:
    def __init__(self):self.messages=[]
    async def send(self,uid,text):self.messages.append((uid,text))

class FakeTokora:
    def __init__(self):
        self.started=[];self.active=[];self.final={};self.issue=None;self.history=[]
    async def active_tasks(self):return self.active
    async def task_page(self,offset=0,limit=100):
        return {'items':self.history[offset:offset+limit],'more':offset+limit<len(self.history)}
    async def start(self,payload):
        self.started.append(payload)
        if isinstance(self.issue,ApiError):raise self.issue
        if self.issue:raise ApiError(self.issue,503)
        return {'id':'job1','state':'running','done':0,'total':2}
    async def task(self,job_id):
        result=self.final.get(job_id,{'id':job_id,'state':'running'})
        if isinstance(result,ApiError):raise result
        return result


def test_worker_dispatch_and_completion():
    async def run():
        cfg=Config('fake','fake',99,dispatch_enabled=True)
        db=Store(':memory:');db.assign(1,'acc1');db.grant(1,30)
        tid=db.queue_task(1,'Run','publish',{'assets':{'accountIds':['acc1']},'awemeIds':['7420000000000000000']})
        api=FakeTokora();tg=FakeTelegram();worker=Worker(db,api,tg,cfg)
        await worker.tick()
        assert db.task(tid)['state']=='running'
        assert len(api.started)==1
        api.final['job1']={'id':'job1','state':'completed','published':3}
        await worker.tick()
        assert db.task(tid)['state']=='completed'
        assert len(api.started)==1
        db.close()
    asyncio.run(run())


def test_worker_uncertain_retry_uses_same_request_id():
    async def run():
        cfg=Config('fake','fake',99,dispatch_enabled=True)
        db=Store(':memory:');db.assign(1,'acc1');db.grant(1,30)
        tid=db.queue_task(1,'Run','publish',{'assets':{'accountIds':['acc1']}})
        api=FakeTokora();tg=FakeTelegram();worker=Worker(db,api,tg,cfg)
        api.issue='service_unavailable';await worker.tick()
        assert db.task(tid)['state']=='reconcile'
        api.issue=None;api.active=[{'id':'external-job'}]
        worker.next_dispatch_at=0  # simulate cooldown elapsed
        await worker.tick()
        assert len(api.started)==1
        api.active=[]
        await worker.tick()
        assert len(api.started)==2
        assert api.started[0]['requestId']==api.started[1]['requestId']
        db.close()
    asyncio.run(run())


def test_worker_rejection_after_uncertain_start_keeps_account_locked():
    async def run():
        cfg=Config('fake','fake',99,dispatch_enabled=True)
        db=Store(':memory:');db.assign(1,'acc1');db.grant(1,30)
        tid=db.queue_task(1,'Run','publish',{'assets':{'accountIds':['acc1']}})
        api=FakeTokora();worker=Worker(db,api,FakeTelegram(),cfg)
        api.issue=ApiError('connection_unavailable')
        await worker.tick()
        api.issue=ApiError('rate_limited',429,retry_after=7)
        worker.next_dispatch_at=0
        await worker.tick()
        assert db.task(tid)['state']=='reconcile'
        assert not db.cancel_queued(tid,1)
        with pytest.raises(ValueError):
            db.unassign(1,'acc1')
        api.issue=ApiError('missing_proxies',400)
        worker.next_dispatch_at=0
        await worker.tick()
        assert db.task(tid)['error_code']=='reconcile_review'
        assert db.next_eligible(99) is None
        assert {payload['requestId'] for payload in api.started}=={db.task(tid)['request_id']}
        db.close()
    asyncio.run(run())


def test_worker_read_only_reconciles_after_restart_and_revocation(tmp_path):
    async def run():
        path=str(tmp_path/'queue.sqlite3')
        db=Store(path);db.assign(1,'acc1');db.grant(1,30)
        title='Длинное имя задания ' * 5
        tid=db.queue_task(1,title,'publish',{'assets':{'accountIds':['acc1']}})
        assert db.claim_start(tid,'queued',99)
        payload=json.loads(db.task(tid)['payload_json'])
        db.block(1)
        db.close()

        db=Store(path);db.recover()
        api=FakeTokora()
        api.history=[{'id':f'other{i}','name':'Другой заказ','kind':'publish','state':'completed'} for i in range(100)]
        api.history.append({'id':'remote_id','name':payload['name'],'kind':'publish','state':'finished'})
        worker=Worker(db,api,FakeTelegram(),Config('fake','fake',99,dispatch_enabled=False))
        await worker.tick()
        job=db.task(tid)
        assert not api.started
        assert job['display_name']==title and len(payload['name'])<=60
        assert job['upstream_id']=='remote_id' and job['state']=='completed'
        assert db.unassign(1,'acc1')
        db.close()
    asyncio.run(run())


def test_revoked_unmatched_launch_holds_while_next_user_can_run():
    async def run():
        cfg=Config('fake','fake',99,dispatch_enabled=True)
        db=Store(':memory:');db.assign(1,'acc1');db.grant(1,30)
        tid=db.queue_task(1,'Run','publish',{'assets':{'accountIds':['acc1']}})
        assert db.claim_start(tid,'queued',99)
        db.recover()
        db.block(1)
        db.assign(2,'acc2');db.grant(2,30)
        next_tid=db.queue_task(2,'Run','publish',{'assets':{'accountIds':['acc2']}})
        api=FakeTokora();tg=FakeTelegram();worker=Worker(db,api,tg,cfg)
        # Never auto-link an ordinary title or a coincidentally identical title
        # of another job kind.
        name=json.loads(db.task(tid)['payload_json'])['name']
        api.history=[
            {'id':'similar','name':'Run','kind':'publish','state':'completed'},
            {'id':'wrong_kind','name':name,'kind':'fetch','state':'completed'},
        ]
        await worker.tick()
        assert db.task(tid)['error_code']=='reconcile_access_revoked'
        assert db.task(next_tid)['state']=='running'
        assert [job['assets']['accountIds'] for job in api.started]==[['acc2']]
        assert tid in tg.messages[0][1]
        db.grant(1,30)
        assert db.next_eligible(99) is None
        with pytest.raises(ValueError):
            db.assign(2,'acc1')
        held=db.hold_reconciliation(tid)
        assert db.settle_reconciliation_absent(held)
        assert not db.settle_reconciliation_absent(held)
        assert db.unassign(1,'acc1')
        db.close()
    asyncio.run(run())


def test_worker_retries_missing_known_id_without_restarting_the_job():
    async def run():
        cfg=Config('fake','fake',99,dispatch_enabled=True)
        db=Store(':memory:');db.assign(1,'acc1');db.grant(1,30)
        tid=db.queue_task(1,'Run','publish',{'assets':{'accountIds':['acc1']}})
        db.transition(tid,'running',upstream_id='known_id')
        api=FakeTokora();worker=Worker(db,api,FakeTelegram(),cfg)
        api.final['known_id']=ApiError('job_not_found',404)
        await worker.tick()
        assert db.task(tid)['state']=='reconcile' and db.next_eligible(99) is None
        with pytest.raises(ValueError):
            db.unassign(1,'acc1')
        held=db.hold_reconciliation(tid)
        api.final['known_id']={'id':'known_id','state':'completed'}
        worker.next_reconcile_at=0
        await worker.tick()
        assert not api.started
        assert db.task(tid)['state']=='completed'
        assert not db.settle_reconciliation_absent(held)
        assert db.unassign(1,'acc1')
        db.close()
    asyncio.run(run())


def test_reconciliation_retry_budget_survives_restart(tmp_path):
    async def run():
        cfg=Config('fake','fake',99,dispatch_enabled=True)
        path=str(tmp_path/'queue.sqlite3')
        db=Store(path);db.assign(1,'acc1');db.grant(1,30)
        tid=db.queue_task(1,'Run','publish',{'assets':{'accountIds':['acc1']}})
        api=FakeTokora();api.issue=ApiError('service_unavailable',503)
        worker=Worker(db,api,FakeTelegram(),cfg)
        await worker.tick()
        for _ in range(RECONCILE_RETRY_LIMIT):
            worker.next_dispatch_at=0
            await worker.tick()
        assert db.task(tid)['reconcile_attempts']==RECONCILE_RETRY_LIMIT
        db.close()

        db=Store(path);db.recover()
        worker=Worker(db,api,FakeTelegram(),cfg)
        await worker.tick()
        assert len(api.started)==RECONCILE_RETRY_LIMIT+1
        assert db.task(tid)['error_code']=='reconcile_review'
        assert db.next_eligible(99) is None
        db.close()
    asyncio.run(run())


def test_worker_rechecks_account_assignment():
    async def run():
        cfg=Config('fake','fake',99,dispatch_enabled=True)
        db=Store(':memory:');db.assign(1,'acc1');db.grant(1,30)
        tid=db.queue_task(1,'Run','publish',{'assets':{'accountIds':['acc1']}})
        # Simulate manual DB change during queue (administrator may revoke access).
        db.conn.execute('DELETE FROM accounts WHERE account_id=?',('acc1',))
        api=FakeTokora();worker=Worker(db,api,FakeTelegram(),cfg)
        await worker.tick()
        assert not api.started
        assert db.task(tid)['error_code']=='account_assignment_changed'
        db.close()
    asyncio.run(run())
