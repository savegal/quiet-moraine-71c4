import asyncio
import json
from contextlib import closing
from hashlib import blake2s
from types import SimpleNamespace
import pytest
from proxima_bot.config import Config
from proxima_bot.db import Store
from proxima_bot.worker import RECONCILE_RETRY_LIMIT, Worker
from proxima_bot.api import ApiError

class FakeTelegram:
    def __init__(self):self.messages=[]
    async def send(self,uid,text):self.messages.append((uid,text))

FINISHED_AT='2026-11-16T16:00:00Z'

class FakeTokora:
    def __init__(self):
        self.started=[];self.active=[];self.final={};self.issue=None;self.history=[]
        self.sessions=None
        self.account_reads=[]
        self.folders={}
    async def active_tasks(self):return self.active
    async def accounts_for(self,ids):
        self.account_reads.append(list(ids))
        if isinstance(self.sessions,ApiError):raise self.sessions
        rows = self.sessions if self.sessions is not None else {
            aid:{'accountId':aid,'status':'active'} for aid in ids}
        folder = blake2s('\0'.join(sorted(ids)).encode(),digest_size=4).hexdigest()
        rows = {aid:{'folderId':folder,**row} for aid,row in rows.items()}
        self.folders[folder] = rows
        return rows
    async def accounts_in_folder(self,folder,*,expected_count=None):
        rows = self.folders.get(folder,{})
        return rows if expected_count is None or len(rows)==expected_count else {}
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
        api.final['job1']={'id':'job1','state':'completed','published':3,'finishedAt':FINISHED_AT}
        await worker.tick()
        assert db.task(tid)['state']=='completed'
        assert len(api.started)==1
        db.close()
    asyncio.run(run())


@pytest.mark.parametrize('response', [
    {'id':'job1','state':'running','done':2}, ApiError('job_not_found',404),
])
def test_late_poll_does_not_replace_a_newer_snapshot(response):
    async def run():
        with closing(Store(':memory:')) as db:
            db.grant(7,30)
            db.assign(7,'acc7')
            tid = db.queue_task(7,'Сбор','fetch',{'assets':{'accountIds':['acc7']}})
            db.transition(tid,'running',upstream_id='job1',snapshot={'id':'job1','state':'running','done':1})
            api = FakeTokora()
            api.final['job1'] = response
            worker = Worker(db,api,FakeTelegram(),Config('fake','fake',99))
            previous = db.task(tid)
            latest = {'id':'job1','state':'running','done':5}
            db.transition(tid,'running',snapshot=latest)

            await worker.refresh_job(previous)

            current = db.task(tid)
            assert current['state']=='running'
            assert json.loads(current['snapshot_json'])==latest
    asyncio.run(run())


def test_two_slots_share_a_users_pool_and_count_remote_and_local_only_once():
    class Provider(FakeTokora):
        async def start(self, payload):
            self.started.append(payload)
            return {'id': f'job-{len(self.started)}', 'state': 'running'}

    async def run():
        db = Store(':memory:')
        try:
            for uid, aid in ((7, 'a'), (8, 'b'), (9, 'c')):
                db.grant(uid, 30)
                db.assign(uid, aid)
            first = db.queue_task(7, 'Первый', 'fetch', {'assets': {'accountIds': ['a']}})
            overlap = db.queue_task(7, 'Тот же пул', 'fetch', {'assets': {'accountIds': ['a']}})
            second = db.queue_task(8, 'Другой пул', 'fetch', {'assets': {'accountIds': ['b']}})
            third = db.queue_task(9, 'Третий пул', 'fetch', {'assets': {'accountIds': ['c']}})
            api = Provider()
            cfg = Config('fake', 'fake', 99, dispatch_enabled=True, max_active_external=2)
            worker = Worker(db, api, FakeTelegram(), cfg)
            await worker.tick()
            api.active = [{'id': 'job-1', 'state': 'running'}]
            await worker.tick()
            await worker.tick()
            assert [payload['assets']['accountIds'] for payload in api.started] == [['a'], ['a']]
            assert db.task(first)['state'] == db.task(overlap)['state'] == 'running'
            assert db.task(second)['state'] == db.task(third)['state'] == 'queued'
            assert not db.claim_start(third, 'queued', 99, max_active=2)
            api.final['job-1'] = {'id': 'job-1', 'state': 'completed','finishedAt':FINISHED_AT}
            api.active = [{'id': 'job-2', 'state': 'running'}]
            await worker.tick()
            assert db.task(first)['state'] == 'completed'
            assert db.task(second)['state'] == 'running'
            assert db.task(third)['state'] == 'queued'
        finally:
            db.close()
    asyncio.run(run())


def test_members_can_fill_shared_slots_with_their_own_pools_alongside_site_tasks():
    class Provider(FakeTokora):
        async def start(self,payload):
            self.started.append(payload)
            job={'id':f'job-{len(self.started)}','state':'running'}
            self.active.append(job)
            return job

    async def run():
        db = Store(':memory:')
        try:
            db.grant(7, 30)
            db.assign(7, 'a')
            db.grant(8, 30)
            db.assign(8, 'b')
            cfg=Config('fake','fake',99,dispatch_enabled=True)
            with pytest.raises(ValueError,match='Назначенные аккаунты'):
                db.queue_task(7,'Чужой пул','fetch',{'assets':{'accountIds':['b']}})
            orders=[db.queue_task(7,f'Заказ {n}','fetch',{'assets':{'accountIds':['a']}},
                                  max_open=cfg.max_open_tasks) for n in range(5)]
            with pytest.raises(ValueError,match='количество одновременно'):
                db.queue_task(7,'Шестой','fetch',{'assets':{'accountIds':['a']}},max_open=cfg.max_open_tasks)
            other=db.queue_task(8,'Другой трафер','fetch',{'assets':{'accountIds':['b']}})
            api=Provider()
            api.active = [{'id': 'website', 'state': 'running', 'accountsAssigned': 1}]
            worker=Worker(db,api,FakeTelegram(),cfg)
            for _ in range(6):
                await worker.tick()
            assert [db.task(tid)['state'] for tid in orders]==['running']*4+['queued']
            assert db.task(other)['state']=='queued'
            assert len(api.active)==5

            api.active=[job for job in api.active if job['id']!='website']
            await worker.tick()
            assert all(db.task(tid)['state']=='running' for tid in orders)
            assert db.task(other)['state']=='queued'
            assert [payload['assets']['accountIds'] for payload in api.started]==[['a']]*5

            api.final['job-1']={'id':'job-1','state':'completed','finishedAt':FINISHED_AT}
            api.active=[job for job in api.active if job['id']!='job-1']
            await worker.tick()
            assert len(db.running())==5 and db.task(other)['state']=='running'
            assert api.started[-1]['assets']['accountIds']==['b']
            assert db.account_in_use('a')
        finally:
            db.close()
    asyncio.run(run())


@pytest.mark.parametrize('limit', [1, 2])
def test_configured_slot_limit_and_tokora_busy_keep_second_order_queued(limit):
    async def run():
        db = Store(':memory:')
        try:
            db.grant(7, 30)
            db.assign(7, 'a')
            db.grant(8, 30)
            db.assign(8, 'b')
            first = db.queue_task(7, 'Первый', 'fetch', {'assets': {'accountIds': ['a']}})
            second = db.queue_task(8, 'Второй', 'fetch', {'assets': {'accountIds': ['b']}})
            api = FakeTokora()
            cfg = Config('fake', 'fake', 99, dispatch_enabled=True, max_active_external=limit)
            worker = Worker(db, api, FakeTelegram(), cfg)
            await worker.tick()
            api.issue = ApiError('job_busy', 400)
            await worker.tick()
            assert len(api.started) == limit
            assert db.task(first)['state'] == 'running'
            assert db.task(second)['state'] == 'queued'
            assert db.task(second)['error_code']==('job_busy' if limit==2 else '')
            assert db.task(second)['launch_payload_json'] is None
        finally:
            db.close()
    asyncio.run(run())


def test_status_502_backs_off_without_delaying_the_execution_deadline(monkeypatch):
    start = 1_800_000_000
    clock = [start]
    monkeypatch.setattr('proxima_bot.db.now', lambda: clock[0])
    monkeypatch.setattr('proxima_bot.worker.now', lambda: clock[0])
    monkeypatch.setattr('proxima_bot.worker.time', SimpleNamespace(monotonic=lambda: clock[0] - start))

    class Provider(FakeTokora):
        reads = 0
        stops = 0

        async def start(self, payload):
            return {**await super().start(payload), 'phase': 'publish'}

        async def task(self, job_id):
            self.reads += 1
            raise ApiError('upstream_error', 502)

        async def stop(self, job_id):
            self.stops += 1
            return {'id': job_id, 'state': 'stopped','finishedAt':FINISHED_AT}

    async def run():
        db = Store(':memory:')
        try:
            db.grant(7, 30)
            db.assign(7, 'a')
            tid = db.queue_task(7, 'Заказ', 'publish', {'assets': {'accountIds': ['a']}})
            api = Provider()
            worker = Worker(db, api, FakeTelegram(), Config('fake', 'fake', 99, dispatch_enabled=True))
            await worker.tick()
            for elapsed in (60, 65, 90, 590, 595):
                clock[0] = start + elapsed
                await worker.tick()
            assert api.reads == 3
            assert api.stops == 0
            assert db.task(tid)['state'] == 'running'
            clock[0] = start + 600
            await worker.tick()
            assert api.stops == 1
            assert db.task(tid)['state'] == 'stopped'
            assert json.loads(db.task(tid)['snapshot_json'])['executionTimeLimit'] is True
        finally:
            db.close()
    asyncio.run(run())


def test_late_confirmations_survive_restart_and_read_final_snapshot(tmp_path,monkeypatch):
    clock=[1_800_000_000]
    monkeypatch.setattr('proxima_bot.db.now',lambda:clock[0])
    monkeypatch.setattr('proxima_bot.worker.now',lambda:clock[0])

    async def run():
        path=str(tmp_path/'settlements.sqlite3')
        cfg=Config('fake','fake',99,dispatch_enabled=True)
        db=Store(path)
        db.grant(1,30)
        db.assign(1,'acc1')
        tid=db.queue_task(1,'Run','publish',{'assets':{'accountIds':['acc1']}})
        api=FakeTokora()
        tg=FakeTelegram()
        worker=Worker(db,api,tg,cfg)
        await worker.tick()
        deadline=(clock[0]+22)*1000
        api.final['job1']={'id':'job1','state':'finished','settleUntil':deadline,'published':1,'finishedAt':FINISHED_AT}
        await worker.tick()
        assert db.task(tid)['state']=='completed'
        assert db.running()==[]
        assert db.task(tid)['settle_poll_at']==clock[0]+5
        db.close()

        db=Store(path)
        worker=Worker(db,api,tg,cfg)
        clock[0]+=5
        api.final['job1']=ApiError('rate_limited',429,16)
        await worker.refresh_settlements()
        assert db.task(tid)['settle_poll_at']==clock[0]+16

        clock[0]+=16
        api.final['job1']={'id':'job1','state':'finished','settleUntil':deadline,'finishedAt':FINISHED_AT,
                           'published':2,'lateConfirmed':1}
        await worker.refresh_settlements()
        assert db.task(tid)['settle_poll_at']==deadline//1000
        assert json.loads(db.task(tid)['snapshot_json'])['lateConfirmed']==1

        clock[0]+=1
        api.final['job1'].update(published=3,publishedAfterFinish=2)
        await worker.refresh_settlements()
        saved=db.task(tid)
        assert saved['state']=='completed'
        assert saved['settle_poll_at']==0
        assert json.loads(saved['snapshot_json'])['published']==3
        assert db.due_settlements(clock[0])==[]
        db.close()

    asyncio.run(run())


@pytest.mark.parametrize('field,value',[('checkerFolder','folder'),('likerFolder','folder'),('likersOnly',True)])
def test_shared_queue_rejects_cabinet_account_routing(field,value):
    async def run():
        db=Store(':memory:')
        db.grant(1,30)
        db.assign(1,'acc1')
        tid=db.queue_task(1,'Run','publish',{'assets':{'accountIds':['acc1']},field:value})
        api=FakeTokora()
        await Worker(db,api,FakeTelegram(),Config('fake','fake',99,dispatch_enabled=True)).tick()
        assert api.started==[]
        assert db.task(tid)['error_code']=='task_limits'
        db.close()

    asyncio.run(run())


def test_invalid_tokora_key_keeps_order_queued_with_an_actionable_status():
    async def run():
        db=Store(':memory:')
        db.grant(1,30)
        db.assign(1,'acc1')
        tid=db.queue_task(1,'Run','publish',{'assets':{'accountIds':['acc1']}})
        api=FakeTokora()
        tg=FakeTelegram()
        worker=Worker(db,api,tg,Config('fake','fake',99,dispatch_enabled=True))
        active_tasks=api.active_tasks

        async def denied():
            raise ApiError('api_key_invalid',401)

        api.active_tasks=denied
        await worker.tick()
        assert db.task(tid)['state']=='queued'
        assert db.task(tid)['error_code']=='admin_action_required'
        assert not api.started and 'api_key_invalid' in tg.messages[-1][1]

        api.active_tasks=active_tasks
        worker.next_dispatch_at=0
        await worker.tick()
        assert db.task(tid)['state']=='running'
        assert not db.task(tid)['error_code']
        db.close()

    asyncio.run(run())


def test_worker_uncertain_retry_uses_same_request_id():
    async def run():
        cfg=Config('fake','fake',99,dispatch_enabled=True,max_active_external=1)
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
        api.history.append({'id':'remote_id','name':payload['name'],'kind':'publish','state':'finished','finishedAt':FINISHED_AT})
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
        payload = json.loads(db.task(tid)['payload_json'])
        assert db.claim_start(tid,'queued',99,launch_payload={**payload,'accountFolder':'1234abcd'})
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


def test_legacy_unconfirmed_launch_with_account_ids_is_not_replayed():
    async def run():
        db=Store(':memory:')
        try:
            db.grant(7,30)
            db.assign(7,'old-account')
            tid=db.queue_task(7,'Заказ старой версии','publish',{'assets':{'accountIds':['old-account']}})
            assert db.claim_start(tid,'queued',99)
            db.recover()
            api=FakeTokora()
            worker=Worker(db,api,FakeTelegram(),Config('fake','fake',99,dispatch_enabled=True))
            await worker.tick()
            job=db.task(tid)
            assert job['state']=='reconcile' and job['error_code']=='reconcile_review'
            assert not api.started
        finally:
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
        api.final['known_id']={'id':'known_id','state':'completed','finishedAt':FINISHED_AT}
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


def test_config_supports_higher_concurrency_and_restricted_modes(monkeypatch):
    monkeypatch.setattr('proxima_bot.config.load_dotenv', lambda: False)
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', '123:test')
    monkeypatch.setenv('TOKORA_API_KEY', 'key')
    monkeypatch.setenv('OWNER_ID', '99')
    monkeypatch.delenv('MAX_ACTIVE_TASKS', raising=False)
    monkeypatch.delenv('MAX_OPEN_TASKS', raising=False)
    monkeypatch.setenv('RESTRICTED_MODES', 'fetch, feedwarm, fetch')
    assert Config.from_env().max_active_external == 5
    assert Config.from_env().max_open_tasks == 5

    monkeypatch.setenv('MAX_ACTIVE_TASKS', '4')
    cfg = Config.from_env()
    assert cfg.max_active_external == 4
    assert cfg.restricted_modes == ('fetch', 'feedwarm')

    monkeypatch.setenv('MAX_ACTIVE_TASKS', '0')
    with pytest.raises(ValueError, match='MAX_ACTIVE_TASKS'):
        Config.from_env()

    monkeypatch.setenv('MAX_ACTIVE_TASKS', '2')
    monkeypatch.setenv('RESTRICTED_MODES', 'unknown_mode')
    with pytest.raises(ValueError, match='RESTRICTED_MODES'):
        Config.from_env()


def test_worker_runs_three_concurrent_disjoint_tasks():
    class Provider(FakeTokora):
        async def start(self, payload):
            self.started.append(payload)
            job = {'id': f'job-{len(self.started)}', 'state': 'running'}
            self.active.append(job)
            return job

    async def run():
        db = Store(':memory:')
        try:
            for uid, aid in ((7, 'a'), (8, 'b'), (9, 'c')):
                db.grant(uid, 30)
                db.assign(uid, aid)
                db.queue_task(uid, f'Заказ {uid}', 'publish', {'assets': {'accountIds': [aid]}})
            api = Provider()
            cfg = Config('fake', 'fake', 99, dispatch_enabled=True, max_active_external=3)
            worker = Worker(db, api, FakeTelegram(), cfg)
            for _ in range(3):
                await worker.tick()
            assert [p['assets']['accountIds'] for p in api.started] == [['a'], ['b'], ['c']]
            assert len(db.running()) == 3
        finally:
            db.close()
    asyncio.run(run())


def test_execution_limit_waits_for_publish_phase_and_scales_per_link(monkeypatch):
    start = 1_800_000_000
    clock = [start]
    monkeypatch.setattr('proxima_bot.db.now', lambda: clock[0])
    monkeypatch.setattr('proxima_bot.worker.now', lambda: clock[0])
    monkeypatch.setattr('proxima_bot.worker.time', SimpleNamespace(monotonic=lambda: clock[0] - start))

    class PhasedProvider(FakeTokora):
        def __init__(self):
            super().__init__()
            self.phases = {}
            self.stops = []

        async def start(self, payload):
            jid = f'job-{len(self.started)+1}'
            self.started.append(payload)
            return {'id': jid, 'state': 'running', 'phase': self.phases.get(jid, 'setup'), 'attempts': 0}

        async def task(self, job_id):
            phase = self.phases.get(job_id, 'prepare')
            return {'id': job_id, 'state': 'running', 'phase': phase, 'attempts': 0 if phase == 'prepare' else 1}

        async def stop(self, job_id):
            self.stops.append(job_id)
            return {'id': job_id, 'state': 'stopped','finishedAt':FINISHED_AT}

    async def run():
        db = Store(':memory:')
        try:
            db.grant(7, 30)
            db.assign(7, 'a')
            db.grant(8, 30)
            db.assign(8, 'b')
            t1 = db.queue_task(7, 'Одна ссылка', 'publish', {'assets': {'accountIds': ['a']}, 'awemeIds': ['7420000000000000001']})
            api = PhasedProvider()
            worker = Worker(db, api, FakeTelegram(), Config('fake', 'fake', 99, dispatch_enabled=True))
            await worker.tick()
            assert db.task(t1)['execution_started_at'] == 0

            # Still in prepare after 15 minutes — deadline has NOT started yet.
            api.phases['job-1'] = 'prepare'
            clock[0] = start + 900
            await worker.tick()
            assert db.task(t1)['state'] == 'running'
            assert db.task(t1)['execution_started_at'] == 0
            assert not api.stops

            # Publishing starts at start+901; 1 link => stops at start+901+600 = start+1501.
            api.phases['job-1'] = 'publish'
            clock[0] = start + 901
            await worker.tick()
            assert db.task(t1)['execution_started_at'] == start + 901
            clock[0] = start + 1500
            await worker.tick()
            assert db.task(t1)['state'] == 'running'
            clock[0] = start + 1501
            await worker.tick()
            assert db.task(t1)['state'] == 'stopped'
            assert json.loads(db.task(t1)['snapshot_json'])['executionLimitSeconds'] == 600

            # Two links starting directly in publish => 20 minutes (1200s).
            api.phases['job-2'] = 'publish'
            t2 = db.queue_task(8, 'Две ссылки', 'publish', {'assets': {'accountIds': ['b']}, 'awemeIds': ['7420000000000000001', '7420000000000000002']})
            clock[0] = start + 1600
            await worker.tick()
            assert db.task(t2)['execution_started_at'] == start + 1600
            clock[0] = start + 1600 + 600
            await worker.tick()
            assert db.task(t2)['state'] == 'running'
            clock[0] = start + 1600 + 1200
            await worker.tick()
            assert db.task(t2)['state'] == 'stopped'
            assert json.loads(db.task(t2)['snapshot_json'])['executionLimitSeconds'] == 1200
        finally:
            db.close()

    asyncio.run(run())


def test_dispatch_pause_proxy_balance_alerts_and_folder_mismatch_alert(monkeypatch):
    clock = [100]
    monkeypatch.setattr('proxima_bot.worker.time', SimpleNamespace(monotonic=lambda: clock[0]))

    async def run():
        db = Store(':memory:')
        api = FakeTokora()
        tg = FakeTelegram()
        balance_status = ['low']

        async def call(method, path, **kwargs):
            if (method, path) == ('GET', '/v1/proxies/balance'):
                return {'hasPackage': True, 'status': balance_status[0], 'remainingBytes': 500_000_000}
            raise ApiError('not_found', 404)

        api.call = call
        try:
            db.grant(7, 30)
            db.assign(7, 'a')
            db.set_dispatch_paused(True)
            tid = db.queue_task(7, 'Заказ', 'publish', {'assets': {'accountIds': ['a']}})
            worker = Worker(db, api, tg, Config('fake', 'fake', 99, dispatch_enabled=True))
            await worker.tick()
            assert db.task(tid)['state'] == 'queued' and not api.started

            # Proxy balance alerts respect 600s cooldown and notify owner on low & exhausted.
            db.set_dispatch_paused(False)
            await worker.check_proxy_balance()
            assert any('прокси' in text.lower() or 'трафик' in text.lower() for _, text in tg.messages)
            prev_len = len(tg.messages)
            clock[0] += 100
            await worker.check_proxy_balance()
            assert len(tg.messages) == prev_len
            balance_status[0] = 'exhausted'
            clock[0] += 600
            await worker.check_proxy_balance()
            assert len(tg.messages) > prev_len

            # Folder mismatch alerts owner cleanly.
            api.sessions = {'a': {'accountId': 'a', 'status': 'active', 'folderId': '1111aaaa'}}
            api.folders = {'1111aaaa': {'a': {'accountId': 'a', 'status': 'active', 'folderId': '1111aaaa'},
                                        'other': {'accountId': 'other', 'status': 'active', 'folderId': '1111aaaa'}}}
            worker.warned.clear()
            await worker.tick()
            assert db.task(tid)['error_code'] == 'account_folder_mismatch'
            assert any('папк' in text.lower() for _, text in tg.messages)
        finally:
            db.close()

    asyncio.run(run())
