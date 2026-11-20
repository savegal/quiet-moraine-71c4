"""Comprehensive tests for owner access to all workspace accounts (unfoldered, multi-folder, traffer-assigned)."""
import asyncio
from contextlib import closing
from dataclasses import replace
import json

import pytest

from proxima_bot.api import ApiError, Tokora
from proxima_bot.advanced_db import ExtendedStore
from proxima_bot.config import Config
from proxima_bot.db import Store
from proxima_bot.worker import Worker
from test_extended import button_callback, new_app, select_order_template
from test_worker import FakeTelegram, FakeTokora


def _tokora_account(aid: str, *, status: str = 'active', folder_id: str | None = None) -> dict:
    item = {'accountId': aid, 'status': status}
    if folder_id is not None:
        item['folderId'] = folder_id
    return item


def test_owner_with_zero_folders_and_unfoldered_accounts_can_create_and_dispatch_order():
    async def run():
        app = new_app()
        app.cfg = replace(app.cfg, dispatch_enabled=True)
        rows = [
            _tokora_account('unfoldered-1'),
            _tokora_account('unfoldered-2'),
            _tokora_account('unfoldered-expired', status='expired'),
        ]

        async def call(method, path, **kwargs):
            if (method, path) == ('GET', '/v1/account-folders'):
                return {'items': [], 'unassigned': 3, 'available': True}
            raise AssertionError((method, path, kwargs))

        async def page(offset, limit=200, **kwargs):
            folder = kwargs.get('folder')
            assert folder in (None, '', 'none')
            matched = [row for row in rows if not row.get('folderId')]
            return {'accounts': matched[offset:offset + limit], 'total': len(matched)}

        tokora = Tokora(None, 'https://tokora.example', 'api-key')
        tokora.account_page = page
        posted = []

        async def tokora_call(method, path, **kwargs):
            if (method, path) == ('POST', '/v1/tasks'):
                posted.append(kwargs.get('payload'))
                return {'id': 'job-unfoldered', 'state': 'running', 'done': 0, 'total': 3}
            if (method, path) == ('GET', '/v1/tasks'):
                return {'items': [], 'more': False}
            return {'accounts': [], 'total': 0}

        tokora.call = tokora_call

        app.api.call = call
        app.api.account_page = page
        app.api.accounts_in_folder = tokora.accounts_in_folder
        try:
            # 1. Owner opens "Папки кабинета" with 0 folders and sees default "Все аккаунты кабинета" + "Аккаунты без папки" button.
            await app.callback(99, {'data': 'ac:folders:0'})
            folders_text = app.tg.out[-1][1]
            assert 'Все аккаунты кабинета' in folders_text
            assert button_callback(app, 'ac:order_folder:none') == 'ac:order_folder:none'

            # 2. Switch to explicit "none" ("Аккаунты без папки") and confirm a wizard order.
            await app.callback(99, {'data': 'ac:order_folder:none'})
            assert 'Аккаунты без папки' in app.tg.out[-1][1]

            await app.create(99)
            await app.callback(99, {'data': button_callback(app, 'new:fetch')})
            await app.input_draft(99, '7420000000000000000')
            await app.input_draft(99, '100')
            await app.callback(99, {'data': button_callback(app, 'wiz:fetch_without_replies:')})
            await app.callback(99, {'data': button_callback(app, 'wiz:skip:')})
            await app.callback(99, {'data': button_callback(app, 'wiz:quick:')})
            confirm = button_callback(app, 'wiz:submit:')
            draft = app.db.draft(99)
            assert draft['data']['review_account_folder'] == 'none'
            assert draft['data']['review_account_ids'] == ['unfoldered-1', 'unfoldered-2', 'unfoldered-expired']

            await app.callback(99, {'data': confirm})
            job, = app.db.recent_tasks(99)
            assert json.loads(job['payload_json'])['accountFolder'] == 'none'

            # 3. Worker verifies live unfoldered accounts and sends accountFolder="none" without assets.accountIds.
            await Worker(app.db, tokora, app.tg, app.cfg).tick()
            assert app.db.task(job['task_id'])['state'] == 'running'
            assert len(posted) == 1
            assert posted[0]['accountFolder'] == 'none'
            assert 'accountIds' not in posted[0].get('assets', {})
        finally:
            app.db.close()

    asyncio.run(run())


class ConcurrentTokora(FakeTokora):
    async def start(self, payload):
        self.started.append(payload)
        if isinstance(self.issue, ApiError):
            raise self.issue
        if self.issue:
            raise ApiError(self.issue, 503)
        job_id = f'job-{len(self.started)}'
        self.final.setdefault(job_id, {'id': job_id, 'state': 'running'})
        return {'id': job_id, 'state': 'running', 'done': 0, 'total': 2}


def test_owner_shares_workspace_pool_with_site_tasks_and_retries_when_tokora_is_busy():
    class Provider(ConcurrentTokora):
        async def start(self, payload):
            result=await super().start(payload)
            self.active.append(result)
            return result

    async def run():
        with closing(ExtendedStore(':memory:')) as db:
            db.owner_id=99
            db.grant(7,30)
            db.assign(7,'shared')
            account={'assets':{'accountIds':['shared']}}
            orders=[db.queue_task(99,f'Заказ {i}','fetch',{**account,'accountFolder':''}) for i in range(4)]
            api=Provider()
            api.folders['']={'shared':_tokora_account('shared')}
            api.active=[{'id':'from-site-1','state':'running'}, {'id':'from-site-2','state':'running'}]
            worker=Worker(db,api,FakeTelegram(),Config('fake','fake',99,dispatch_enabled=True))

            api.issue=ApiError('job_busy',409)
            await worker.tick()
            assert db.task(orders[0])['state']=='queued' and db.task(orders[0])['error_code']=='job_busy'
            request_id=api.started[0]['requestId']
            api.issue=None
            worker.next_dispatch_at=0
            for _ in range(4):
                await worker.tick()

            assert api.started[1]['requestId']==request_id
            assert len(api.active)==5
            assert [db.task(tid)['state'] for tid in orders]==['running','running','running','queued']
            assert len(api.started)==4
            assert all(payload['accountFolder']=='' and payload['assets']['accountIds']==['shared'] for payload in api.started)

            member=db.queue_task(7,'Трафер','fetch',account)
            api.active=[item for item in api.active if item['id']!='from-site-1']
            await worker.tick()
            assert db.task(orders[-1])['state']=='running'
            assert db.task(member)['state']=='queued'
            assert len(api.active)==5

    asyncio.run(run())


def test_owner_workspace_waits_for_active_traffer_and_blocks_overlap_and_runs_disjoint_none_concurrently():
    async def run():
        cfg = Config('fake', 'fake', 99, dispatch_enabled=True, max_active_external=2)
        db = Store(':memory:')
        db.owner_id = 99
        api = ConcurrentTokora()
        tg = FakeTelegram()
        worker = Worker(db, api, tg, cfg)
        try:
            db.grant(7, 30)
            db.assign(7, 'acc7')
            f7 = '1111aaaa'
            acc7_row = _tokora_account('acc7', folder_id=f7)
            free_row = _tokora_account('free-none')
            api.sessions = {'acc7': acc7_row}
            api.folders = {
                f7: {'acc7': acc7_row},
                'none': {'free-none': free_row},
                '': {'acc7': acc7_row, 'free-none': free_row},
            }

            # 1. Traffer starts on acc7, owner uses "none" -> zero account overlap -> runs concurrently!
            t_traffer = db.queue_task(7, 'Traffer', 'publish', {'assets': {'accountIds': ['acc7']}, 'awemeIds': ['7420000000000000001']})
            t_owner_none = db.queue_task(99, 'Owner None', 'publish', {
                'accountFolder': 'none',
                'assets': {'accountIds': ['free-none']},
                'awemeIds': ['7420000000000000002'],
            })
            await worker.tick()
            await worker.tick()
            assert db.task(t_traffer)['state'] == 'running'
            assert db.task(t_owner_none)['state'] == 'running'
            traffer_upstream = db.task(t_traffer)['upstream_id']
            owner_none_upstream = db.task(t_owner_none)['upstream_id']
            assert traffer_upstream != owner_none_upstream

            # 2. Finish owner_none while traffer is STILL running on acc7; owner queues "all" ('') task.
            api.final[owner_none_upstream] = {'id': owner_none_upstream, 'state': 'finished', 'finishedAt': '2026-11-16T16:00:00Z'}
            api.active = [{'id': traffer_upstream, 'kind': 'publish', 'state': 'running'}]
            await worker.tick()
            assert db.task(t_owner_none)['state'] == 'completed'
            assert db.task(t_traffer)['state'] == 'running'

            t_owner_all = db.queue_task(99, 'Owner All', 'publish', {
                'accountFolder': '',
                'assets': {'accountIds': ['acc7', 'free-none']},
                'awemeIds': ['7420000000000000003'],
            })
            started_before = len(api.started)
            await worker.tick()
            # Must stay queued while acc7 is locked by running traffer task.
            assert db.task(t_owner_all)['state'] == 'queued'
            assert len(api.started) == started_before

            # 3. Once traffer task completes, owner "all" task launches and locks acc7.
            api.active = []
            api.history = [{'id': traffer_upstream, 'kind': 'publish', 'state': 'finished', 'finishedAt': '2026-11-16T16:00:00Z'}]
            api.final[traffer_upstream] = {'id': traffer_upstream, 'state': 'finished', 'finishedAt': '2026-11-16T16:00:00Z'}
            await worker.tick()
            assert db.task(t_traffer)['state'] == 'completed'
            assert db.task(t_owner_all)['state'] == 'running'
            owner_all_upstream = db.task(t_owner_all)['upstream_id']
            assert api.started[-1]['accountFolder'] == ''
            assert api.started[-1]['assets']['accountIds'] == ['acc7', 'free-none']

            # 4. While owner "all" is running, a new traffer task on acc7 must wait cleanly in queue.
            t_traffer2 = db.queue_task(7, 'Traffer 2', 'publish', {'assets': {'accountIds': ['acc7']}, 'awemeIds': ['7420000000000000004']})
            api.active = [{'id': owner_all_upstream, 'kind': 'publish', 'state': 'running'}]
            started_during_owner = len(api.started)
            await worker.tick()
            assert db.task(t_traffer2)['state'] == 'queued'
            assert len(api.started) == started_during_owner
        finally:
            db.close()

    asyncio.run(run())


def test_owner_cabinet_task_refreshes_membership_on_drift_and_reconciles_idempotently(tmp_path):
    async def run():
        cfg = Config('fake', 'fake', 99, dispatch_enabled=True)
        path = str(tmp_path / 'owner-cabinet.sqlite3')
        db = Store(path)
        db.owner_id = 99
        api = FakeTokora()
        tg = FakeTelegram()
        worker = Worker(db, api, tg, cfg)
        try:
            # Queued with old snapshot [old-acc]; live cabinet now has [acc-busy-check?, new-a, new-b].
            db.grant(7, 30)
            db.assign(7, 'new-b')
            t_owner = db.queue_task(99, 'Owner Dynamic', 'publish', {
                'accountFolder': '',
                'assets': {'accountIds': ['old-acc']},
                'awemeIds': ['7420000000000000000'],
            })
            api.folders[''] = {
                'new-a': _tokora_account('new-a'),
                'new-b': _tokora_account('new-b', folder_id='2222bbbb'),
            }

            # Simulate uncertain start error on first launch -> should snapshot live ids [new-a, new-b] into launch_payload_json.
            api.issue = 'service_unavailable'
            await worker.tick()
            job = db.task(t_owner)
            assert job['state'] == 'reconcile'
            frozen = json.loads(job['launch_payload_json'])
            assert frozen['accountFolder'] == ''
            assert frozen['assets']['accountIds'] == ['new-a', 'new-b']
            assert job['error_code'] != 'account_folder_mismatch'

            # Even if cabinet gains another idle account before replay, reconcile replays identical requestId & payload.
            api.folders['']['later-added'] = _tokora_account('later-added', folder_id='3333cccc')
            api.issue = None
            worker.next_dispatch_at = 0
            await worker.tick()
            assert db.task(t_owner)['state'] == 'running'
            assert len(api.started) == 2
            assert api.started[0] == api.started[1]
            assert api.started[0]['requestId'] == frozen['requestId']
            assert api.started[0]['accountFolder'] == ''
            with closing(Store(path)) as reopened:
                assert reopened.account_in_use('later-added')
                assert reopened.account_in_use('new-a')
        finally:
            db.close()

    asyncio.run(run())


def test_owner_assigned_mode_templates_repeat_and_topup_behavior():
    async def run():
        app = new_app()
        app.cfg = replace(app.cfg, dispatch_enabled=True)
        rows = [
            _tokora_account('owner-local', folder_id='1234abcd'),
            _tokora_account('extra-unfoldered'),
        ]

        async def call(method, path, **kwargs):
            if (method, path) == ('GET', '/v1/account-folders'):
                return {'items': [{'id': '1234abcd', 'name': 'Local', 'active': 1, 'count': 1}],
                        'unassigned': 1, 'available': True}
            if path.endswith('/reply-topup'):
                return {'available': True, 'mode': 'all', 'comments': 2, 'repliesPerComment': 1}
            if path.endswith('/like-topup'):
                return {'available': True, 'likesPerItem': 1, 'likesPerReply': 0}
            raise AssertionError((method, path))

        async def page(offset, limit=200, **kwargs):
            return {'accounts': rows[offset:offset + limit], 'total': len(rows)}

        app.api.call = call
        app.api.account_page = page
        try:
            app.db.assign(99, 'owner-local')
            app.db.assign(7, 'acc7')
            await app.callback(99, {'data': 'ac:folders:0'})
            assert button_callback(app, 'ac:order_folder:assigned') == 'ac:order_folder:assigned'

            # 1. Choosing "assigned" restricts owner review to their local pool without accountFolder in payload.
            await app.callback(99, {'data': 'ac:order_folder:assigned'})
            app.save_wizard_step(99, 'fetch', 'review', {'name': 'AssignedTest', 'videos': ['7420000000000000000'], 'fetch_count': 50, 'with_replies': False})
            draft = app.db.draft(99)
            assert await app.review_order_accounts(99, draft) == (['owner-local'], 'Назначенный пул')
            assert app.db.draft(99)['data']['review_account_folder'] is None
            await app.save_draft_template(99)
            tmpl_draft = app.db.draft(99)
            assert 'review_account_folder' not in tmpl_draft['data']['values']
            assert 'review_account_ids' not in tmpl_draft['data']['values']

            # 2. Switch back to "all" (""), confirm template/task creation, verify topup preserves accountFolder="" for owner.
            await app.callback(99, {'data': 'ac:order_folder:all'})
            src_payload = {'accountFolder': '', 'awemeIds': ['7420000000000000000'], 'assets': {'accountIds': ['owner-local', 'extra-unfoldered'], 'replyTexts': ['hi']}}
            src_tid = app.db.queue_task(99, 'OwnerSource', 'publish', src_payload)
            app.db.transition(src_tid, 'completed', upstream_id='up-owner')
            src_task = app.db.task(src_tid)
            app.save_wizard_step(99, 'topup', 'review', {'source': src_tid, 'kind': 'like'})
            tok1 = app.db.draft(99)['data']['wizard_token']
            await app.submit_topup(99, src_task, 'like', tok1)
            topup_task = [t for t in app.db.recent_tasks(99) if t['task_id'] != src_tid][0]
            assert json.loads(topup_task['payload_json'])['accountFolder'] == ''

            # 3. Non-owner topup must NEVER inject accountFolder and keeps eligible account restriction.
            tr_payload = {'accountFolder': '', 'awemeIds': ['7420000000000000000'], 'assets': {'accountIds': ['acc7', 'extra-unfoldered'], 'replyTexts': ['hi']}}
            tr_tid = app.db.queue_task(99, 'SourceWithForeign', 'publish', tr_payload)
            app.db.transition(tr_tid, 'completed', upstream_id='up-tr')
            app.db.grant(7, 30)
            app.save_wizard_step(7, 'topup', 'review', {'source': tr_tid, 'kind': 'like'})
            tok2 = app.db.draft(7)['data']['wizard_token']
            await app.submit_topup(7, app.db.task(tr_tid), 'like', tok2)
            tr_topup, = app.db.recent_tasks(7)
            tr_topup_payload = json.loads(tr_topup['payload_json'])
            assert 'accountFolder' not in tr_topup_payload
            assert tr_topup_payload['assets']['accountIds'] == ['acc7']

            # 4. Copying a job strips stale accountFolder for non-owner and updates to active scope for owner.
            app.save_wizard_step(7, 'task_copy', 'review', {'name': 'Copy', 'job': {'kind': 'publish', 'accountFolder': '', 'awemeIds': ['7420000000000000000'], 'assets': {'accountIds': ['acc7']}}})
            await app.copy_preview(7)
            await app.callback(7, {'data': button_callback(app, 'wiz:submit:')})
            assert app.db.draft(7)['step']=='choose_template'
            await select_order_template(app,7,'publish',comments=['Текст'])
            await app.callback(7, {'data': button_callback(app, 'wiz:quick:')})
            await app.callback(7, {'data': button_callback(app, 'wiz:submit:')})
            tr_copy = [t for t in app.db.recent_tasks(7) if t['task_id'] != tr_topup['task_id']][0]
            assert 'accountFolder' not in json.loads(tr_copy['payload_json'])
        finally:
            app.db.close()

    asyncio.run(run())


@pytest.mark.parametrize('bad_folder', ['', 'none', '1234abcd'])
def test_non_owner_cannot_bypass_folder_isolation_via_queued_payload(bad_folder):
    async def run():
        cfg = Config('fake', 'fake', 99, dispatch_enabled=True)
        db = Store(':memory:')
        api = FakeTokora()
        tg = FakeTelegram()
        worker = Worker(db, api, tg, cfg)
        try:
            db.grant(7, 30)
            db.assign(7, 'acc7')
            tid = db.queue_task(7, 'Bypass', 'publish', {
                'accountFolder': bad_folder,
                'assets': {'accountIds': ['acc7']},
                'awemeIds': ['1'],
            })
            await worker.tick()
            assert db.task(tid)['state'] == 'failed'
            assert db.task(tid)['error_code'] == 'task_limits'
            assert not api.started
        finally:
            db.close()

    asyncio.run(run())


def test_tokora_http_contract_for_empty_all_and_none_folders():
    async def run():
        tokora = Tokora(None, 'https://tokora.example', 'api-key')
        calls = []

        async def call(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if (method, path) == ('GET', '/v1/accounts'):
                folder = (kwargs.get('params') or {}).get('folder')
                if folder == 'none':
                    return {'accounts': [{'accountId': 'u1', 'status': 'active', 'folderId': None}], 'total': 1}
                return {'accounts': [
                    {'accountId': 'u1', 'status': 'active', 'folderId': None},
                    {'accountId': 'f1', 'status': 'active', 'folderId': '1234abcd'},
                ], 'total': 2}
            if (method, path) == ('POST', '/v1/tasks'):
                return {'id': 'job-ok', 'state': 'running'}
            raise AssertionError((method, path, kwargs))

        tokora.call = call
        try:
            all_rows = await tokora.accounts_in_folder('')
            assert set(all_rows) == {'u1', 'f1'}
            assert 'folder' not in (calls[-1][2].get('params') or {})

            all_alias = await tokora.accounts_in_folder('all')
            assert set(all_alias) == {'u1', 'f1'}
            assert 'folder' not in (calls[-1][2].get('params') or {})

            none_rows = await tokora.accounts_in_folder('none')
            assert set(none_rows) == {'u1'}
            assert calls[-1][2]['params']['folder'] == 'none'

            await tokora.start({'kind': 'publish', 'accountFolder': '', 'assets': {'accountIds': ['u1', 'f1']}})
            assert calls[-1][2]['payload']['accountFolder'] == ''
            assert 'assets' not in calls[-1][2]['payload']

            await tokora.start({'kind': 'publish', 'accountFolder': 'none', 'assets': {'accountIds': ['u1']}})
            assert calls[-1][2]['payload']['accountFolder'] == 'none'

            with pytest.raises(ApiError):
                await tokora.start({'kind': 'publish', 'accountFolder': 'all'})
            with pytest.raises(ValueError):
                await tokora.accounts_in_folder('../bad')
        finally:
            pass

    asyncio.run(run())
