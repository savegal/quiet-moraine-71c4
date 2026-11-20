import asyncio
import json

import pytest

from proxima_bot.advanced_db import ExtendedStore
from proxima_bot.api import ApiError
from proxima_bot.config import Config
from proxima_bot.db import Store
from proxima_bot.worker import Worker
from test_worker import FakeTelegram, FakeTokora


def enqueue(db, uid, *account_ids):
    db.grant(uid, 7)
    for aid in account_ids:
        db.assign(uid, aid)
    return db.queue_task(uid, 'Заказ', 'publish', {'assets': {'accountIds': list(account_ids)}})


def test_claim_rechecks_ownership_after_api_preflight(tmp_path):
    async def run():
        path = str(tmp_path / 'queue.sqlite3')
        db = Store(path)
        other = Store(path)
        try:
            task_id = enqueue(db, 7, 'account')
            other.register(8)
            api = FakeTokora()
            read_accounts = api.accounts_for

            async def accounts_for(ids):
                # Simulate an old admin process changing ownership while the network read was pending.
                other.conn.execute("UPDATE accounts SET user_id=8 WHERE account_id='account'")
                return await read_accounts(ids)

            api.accounts_for = accounts_for
            worker = Worker(db, api, FakeTelegram(), Config('fake', 'fake', 99, dispatch_enabled=True))
            await worker.tick()
            assert not api.started
            await worker.tick()
            assert db.task(task_id)['error_code'] == 'account_assignment_changed'
            assert not api.started
        finally:
            other.close()
            db.close()

    asyncio.run(run())


def test_active_and_ambiguous_launches_lock_actual_sent_accounts():
    db = Store(':memory:')
    try:
        first = enqueue(db, 7, 'used', 'excluded')
        overlap = enqueue(db, 7, 'used')
        other = enqueue(db, 8, 'other')
        assert not db.account_in_use('used')
        payload = json.loads(db.task(first)['payload_json'])
        payload['assets']['accountIds'] = ['used']
        assert db.claim_start(first, 'queued', 99, launch_payload=payload)
        assert not db.claim_start(other, 'queued', 99)
        assert db.account_in_use('used')
        assert not db.account_in_use('excluded')
        db.transition(first, 'reconcile')
        db.hold_reconciliation(first)
        assert db.next_eligible(99)['task_id'] == overlap
        assert db.claim_start(overlap, 'queued', 99)
        with pytest.raises(ValueError):
            db.assign(8, 'used')
        assert db.assign(8, 'excluded')
    finally:
        db.close()


def test_preflight_discards_account_state_if_check_finished_during_its_read():
    async def run():
        db = Store(':memory:')
        try:
            task_id = enqueue(db, 7, 'account')
            api = FakeTokora()
            read_accounts = api.accounts_for

            async def accounts_for(ids):
                with db.account_action('account', user_id=7):
                    pass
                return await read_accounts(ids)

            api.accounts_for = accounts_for
            worker = Worker(db, api, FakeTelegram(), Config('fake', 'fake', 99, dispatch_enabled=True))
            await worker.tick()
            task = db.task(task_id)
            assert not api.started and task['state'] == 'queued'
            assert json.loads(task['preflight_json'])['phase'] == 'waiting'

            api.sessions = {'account': {'accountId': 'account', 'status': 'expired'}}
            api.accounts_for = read_accounts
            await worker.tick()
            assert not api.started and db.task(task_id)['error_code'] == 'no_active_accounts'
        finally:
            db.close()

    asyncio.run(run())


def test_account_and_workspace_maintenance_serialize_across_connections(tmp_path):
    path = str(tmp_path / 'queue.sqlite3')
    db = ExtendedStore(path)
    other = Store(path)
    try:
        task_id = enqueue(db, 7, 'assigned')
        with pytest.raises(ValueError), db.account_action('assigned', user_id=8):
            pytest.fail('foreign account was reserved')
        with db.account_action('assigned', user_id=7):
            assert other.account_in_use('assigned')
            assert not other.claim_start(task_id, 'queued', 99)
            with pytest.raises(ValueError), other.account_action(None):
                pytest.fail('workspace maintenance overlapped an account action')
        with other.account_action(None):
            assert db.next_eligible(99) is None
            with pytest.raises(ValueError), db.account_action('free', require_unassigned=True):
                pytest.fail('account mutation overlapped workspace maintenance')
        with other.account_action('free', require_unassigned=True):
            assert db.assign_available_accounts(8, ['free']) == 0
            with pytest.raises(ValueError):
                db.assign(8, 'free')
        assert db.claim_start(task_id, 'queued', 99)
        with pytest.raises(ValueError), db.account_action(None):
            pytest.fail('workspace maintenance overlapped a claimed launch')
        with db.account_action(None, allow_in_flight=True):
            assert other.workspace_action_active()
            with pytest.raises(ValueError), other.account_action('free', require_unassigned=True):
                pytest.fail('workspace check stop did not exclude another account action')
        assert db.assign_available_accounts(8, ['free']) == 1
    finally:
        other.close()
        db.close()


def test_legacy_in_flight_task_without_pool_is_held_for_owner_review():
    async def run():
        db = Store(':memory:')
        try:
            legacy = enqueue(db, 7, 'old')
            other = enqueue(db, 8, 'other')
            db.conn.execute(
                "UPDATE tasks SET state='reconcile',payload_json=? WHERE task_id=?",
                (json.dumps({'kind':'publish'}), legacy)
            )
            assert db.account_in_use('other')
            assert not db.claim_start(other,'queued',99)
            with pytest.raises(ValueError,match='Неизвестен пул'), db.account_action('other'):
                pytest.fail('unknown legacy scope did not hold the account')

            api = FakeTokora()
            tg = FakeTelegram()
            worker = Worker(db, api, tg, Config('fake','fake',99,dispatch_enabled=True))
            await worker.tick()
            held = db.task(legacy)
            assert held['error_code']=='reconcile_review'
            assert db.next_eligible(99) is None and not api.started
            assert any('Неизвестен пул' in text for _,text in tg.messages)

            assert db.settle_reconciliation_absent(held)
            await worker.tick()
            assert db.task(other)['state']=='running'
        finally:
            db.close()

    asyncio.run(run())


def test_global_check_hold_ignores_stale_or_unknown_status(tmp_path):
    async def run():
        path = str(tmp_path / 'queue.sqlite3')
        db = Store(path)
        task_id = enqueue(db, 7, 'account')
        db.set_account_check_pending(True)
        db.close()
        db = Store(path)
        try:
            api = FakeTokora()
            worker = Worker(db, api, FakeTelegram(), Config('fake', 'fake', 99, dispatch_enabled=True))

            async def status(method, path, **kwargs):
                assert (method, path) == ('GET', '/v1/accounts/check')
                return {'state': 'new-provider-state'}

            api.call = status
            await worker.tick()
            assert db.account_check_pending() and not api.started
            marker = db.account_check_marker()

            async def stale_idle(method, path, **kwargs):
                with db.account_action(None):
                    db.set_account_check_pending(True)
                return {'state': 'idle'}

            api.call = stale_idle
            worker.next_account_check_at = 0
            await worker.tick()
            assert db.account_check_marker() != marker and not api.started

            async def finished(method, path, **kwargs):
                return {'state': 'finished'}

            api.call = finished
            worker.next_account_check_at = 0
            await worker.tick()
            assert not db.account_check_pending()
            assert db.task(task_id)['state'] == 'running'
        finally:
            db.close()

    asyncio.run(run())


def test_definite_rate_limit_retry_rechecks_readiness_before_resending():
    async def run():
        db = Store(':memory:')
        try:
            task_id = enqueue(db, 7, 'account')
            api = FakeTokora()
            api.issue = ApiError('rate_limited', 429, 10)
            worker = Worker(db, api, FakeTelegram(), Config('fake', 'fake', 99, dispatch_enabled=True))
            await worker.tick()
            assert db.task(task_id)['state'] == 'queued'
            assert db.task(task_id)['launch_payload_json'] is None

            api.sessions = {'account': {'accountId': 'account', 'status': 'expired'}}
            api.issue = None
            worker.next_dispatch_at = 0
            await worker.tick()
            assert len(api.started) == 1
            assert db.task(task_id)['error_code'] == 'no_active_accounts'
        finally:
            db.close()

    asyncio.run(run())
