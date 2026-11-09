import asyncio
from contextlib import suppress
import sqlite3

import pytest

from proxima_bot.advanced_db import ExtendedStore
from proxima_bot.bot import BotApp
from proxima_bot.config import Config
from proxima_bot.updates import UpdateInbox


OWNER = 99
MEMBER = 7
DAY = 86400


class TelegramStub:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.offsets = []
        self.checkouts = []
        self.checkout_answered = asyncio.Event()
        self.messages = []

    async def updates(self, offset):
        self.offsets.append(offset)
        return await self.incoming.get()

    async def precheckout(self, query_id, ok, reason):
        self.checkouts.append((query_id, ok, reason))
        self.checkout_answered.set()

    async def send(self, uid, text, markup=None):
        self.messages.append((uid, text, markup))


def app_for(db, tg=None):
    return BotApp(Config('fake', 'fake', OWNER), db, tg or TelegramStub(), object())


def checkout(update_id, payload, query_id='checkout-one', *, stars=100, uid=MEMBER):
    return {
        'update_id': update_id,
        'pre_checkout_query': {
            'id': query_id,
            'from': {'id': uid},
            'currency': 'XTR',
            'total_amount': stars,
            'invoice_payload': payload,
        },
    }


def receipt(update_id, payload, charge_id='charge-one', *, stars=100, refunded=False, at=None):
    field = 'refunded_payment' if refunded else 'successful_payment'
    update = {
        'update_id': update_id,
        'message': {
            'chat': {'id': MEMBER, 'type': 'private'},
            field: {
                'currency': 'XTR',
                'total_amount': stars,
                'invoice_payload': payload,
                'telegram_payment_charge_id': charge_id,
            },
        },
    }
    if at is not None:
        update['message']['date'] = at
    return update


def test_checkout_is_answered_while_same_chat_is_busy():
    async def run():
        db = ExtendedStore(':memory:')
        try:
            app = app_for(db)
            payload = db.create_invoice(MEMBER, 100, days=1)
            inbox = UpdateInbox(app)
            chat_started = asyncio.Event()
            release_chat = asyncio.Event()

            async def slow_home(uid):
                assert uid == MEMBER
                chat_started.set()
                await release_chat.wait()

            app.show_home = slow_home
            app.tg.incoming.put_nowait([{
                'update_id': 10,
                'message': {'chat': {'id': MEMBER, 'type': 'private'},
                            'from': {'id': MEMBER}, 'text': '/start'},
            }])
            runner = asyncio.create_task(inbox.run())
            try:
                await asyncio.wait_for(chat_started.wait(), timeout=2)
                app.tg.incoming.put_nowait([checkout(11, payload)])
                await asyncio.wait_for(app.tg.checkout_answered.wait(), timeout=2)
                assert app.tg.checkouts[0][:2] == ('checkout-one', True)
                assert not release_chat.is_set()
                assert app.tg.offsets[:2] == [None, 11]
                assert db.get_offset() == 12
            finally:
                runner.cancel()
                with suppress(asyncio.CancelledError):
                    await runner
        finally:
            db.close()

    asyncio.run(run())


def test_intake_and_offset_rollback_together(monkeypatch):
    db = ExtendedStore(':memory:')
    try:
        db.update_offset(11)
        app = app_for(db)
        inbox = UpdateInbox(app)

        def fail_offset(value):
            raise sqlite3.OperationalError('simulated offset write failure')

        monkeypatch.setattr(db, 'update_offset', fail_offset)
        with pytest.raises(sqlite3.OperationalError):
            inbox.persist([receipt(11, 'invoice')])
        assert db.get_offset() == 11
        assert inbox.ready('payment', 1) == []
    finally:
        db.close()


def test_payment_survives_ledger_failure_and_restart(tmp_path, monkeypatch):
    async def run():
        path = str(tmp_path / 'telegram.sqlite3')
        db = ExtendedStore(path)
        try:
            app = app_for(db)
            payload = db.create_invoice(MEMBER, 100, days=2)
            update = receipt(11, payload)
            inbox = UpdateInbox(app)

            def fail_ledger(*args):
                raise sqlite3.OperationalError('simulated ledger write failure')

            monkeypatch.setattr(db, 'record_payment_access', fail_ledger)
            assert inbox.persist([update, update]) == 12
            rows = inbox.ready('payment', 4)
            assert len(rows) == 1
            await inbox.process(rows[0])
            assert db.get_offset() == 12
            assert not db.charge_recorded(MEMBER, payload, 'charge-one')
            assert db.user(MEMBER)['expires_at'] == 0
            pending = db.conn.execute(
                'SELECT attempts FROM telegram_inbox WHERE update_id=11'
            ).fetchone()
            assert pending['attempts'] == 1
        finally:
            db.close()

        db = ExtendedStore(path)
        try:
            app = app_for(db)
            inbox = UpdateInbox(app)
            assert db.get_offset() == 12
            db.conn.execute('UPDATE telegram_inbox SET retry_at=0 WHERE update_id=11')
            row = inbox.ready('payment', 1)[0]
            await inbox.process(row)
            assert db.charge_recorded(MEMBER, payload, 'charge-one')
            assert db.conn.execute('SELECT COUNT(*) FROM telegram_inbox').fetchone()[0] == 0
            expiry = db.user(MEMBER)['expires_at']
            assert expiry > 0
            inbox.persist([receipt(12, payload)])
            await inbox.process(inbox.ready('payment', 1)[0])
            assert db.user(MEMBER)['expires_at'] == expiry
            assert db.dashboard()['payments'] == 1
            assert [row['action'] for row in db.subscriptions(MEMBER)] == ['stars']
        finally:
            db.close()

    asyncio.run(run())


def test_invoice_approval_reserves_one_checkout_id():
    async def run():
        db = ExtendedStore(':memory:')
        try:
            app = app_for(db)
            payload = db.create_invoice(MEMBER, 100, days=1)
            await app.handle(checkout(1, payload, 'wrong-price', stars=99))
            await app.handle(checkout(2, payload, 'wrong-user', uid=MEMBER + 1))
            await app.handle(checkout(3, payload, 'original'))
            await app.handle(checkout(3, payload, 'original'))
            await app.handle(checkout(4, payload, 'competing'))
            assert [(query, ok) for query, ok, _ in app.tg.checkouts] == [
                ('wrong-price', False), ('wrong-user', False),
                ('original', True), ('original', True), ('competing', False),
            ]
        finally:
            db.close()

    asyncio.run(run())


def test_refund_removes_only_refunded_term_and_is_idempotent():
    async def run():
        db = ExtendedStore(':memory:')
        try:
            app = app_for(db)
            initial = db.modify_access(MEMBER, OWNER, 'extend', days=4)
            first = db.create_invoice(MEMBER, 100, days=3)
            await app.handle(receipt(1, first, 'first-charge'))
            db.modify_access(MEMBER, OWNER, 'extend', days=2)
            second = db.create_invoice(MEMBER, 70, days=1)
            await app.handle(receipt(2, second, 'second-charge', stars=70))
            assert db.user(MEMBER)['expires_at'] == initial + 6 * DAY
            db.modify_access(MEMBER, OWNER, 'block')
            await app.handle(receipt(3, first, 'first-charge', refunded=True))
            await app.handle(receipt(4, first, 'first-charge', refunded=True))
            await app.handle(receipt(5, first, 'first-charge'))
            assert db.user(MEMBER)['expires_at'] == initial + 3 * DAY
            assert db.user(MEMBER)['blocked'] == 1
            assert db.dashboard()['stars_paid'] == 70
            assert db.dashboard()['payments'] == 1
            events = db.subscriptions(MEMBER)
            assert sum(event['action'] == 'refund' for event in events) == 1
        finally:
            db.close()

    asyncio.run(run())


@pytest.mark.parametrize(('action', 'days'), [('replace', 9), ('revoke', 0)])
def test_refund_does_not_undo_a_later_manual_reset(action, days):
    async def run():
        db = ExtendedStore(':memory:')
        try:
            app = app_for(db)
            payload = db.create_invoice(MEMBER, 100, days=3)
            await app.handle(receipt(1, payload))
            reset_expiry = db.modify_access(MEMBER, OWNER, action, days=days)
            await app.handle(receipt(2, payload, refunded=True))
            assert db.user(MEMBER)['expires_at'] == reset_expiry
            assert db.dashboard()['stars_paid'] == 0
            assert db.payments()[0]['refunded_at'] > 0
        finally:
            db.close()

    asyncio.run(run())


def test_receipt_delivered_after_refund_is_not_credited():
    async def run():
        db = ExtendedStore(':memory:')
        try:
            app = app_for(db)
            initial = db.modify_access(MEMBER, OWNER, 'extend', days=2)
            payload = db.create_invoice(MEMBER, 100, days=3)
            await app.handle(receipt(1, payload, refunded=True))
            assert db.conn.execute('SELECT COUNT(*) FROM payment_reviews').fetchone()[0] == 1
            await app.handle(receipt(2, payload))
            await app.handle(receipt(3, payload))
            assert db.user(MEMBER)['expires_at'] == initial
            assert db.payments()[0]['refunded_at'] > 0
            assert db.dashboard()['stars_paid'] == 0
            assert db.conn.execute('SELECT COUNT(*) FROM payment_reviews').fetchone()[0] == 0
        finally:
            db.close()

    asyncio.run(run())


def test_late_receipt_preserves_manual_revoke(monkeypatch):
    async def run():
        paid_at = 1_800_000_000
        monkeypatch.setattr('proxima_bot.db.now', lambda: paid_at)
        monkeypatch.setattr('proxima_bot.advanced_db.now', lambda: paid_at + 60)
        db = ExtendedStore(':memory:')
        try:
            app = app_for(db)
            payload = db.create_invoice(MEMBER, 100, days=3)
            db.modify_access(MEMBER, OWNER, 'revoke')
            await app.handle(receipt(1, payload, at=paid_at))
            assert db.user(MEMBER)['expires_at'] == 0
            assert db.charge_recorded(MEMBER, payload, 'charge-one')
            assert db.dashboard()['stars_paid'] == 100
        finally:
            db.close()

    asyncio.run(run())


def test_refund_replays_remaining_payment_from_original_timestamp(monkeypatch):
    async def run():
        clock = [1_800_000_000]
        monkeypatch.setattr('proxima_bot.db.now', lambda: clock[0])
        monkeypatch.setattr('proxima_bot.advanced_db.now', lambda: clock[0])
        db = ExtendedStore(':memory:')
        try:
            app = app_for(db)
            first = db.create_invoice(MEMBER, 100, days=3)
            paid_at = clock[0]
            clock[0] += 2 * DAY
            await app.handle(receipt(1, first, 'first-charge', at=paid_at))
            remaining_expiry = clock[0] + DAY
            assert db.user(MEMBER)['expires_at'] == remaining_expiry
            second = db.create_invoice(MEMBER, 70, days=2)
            await app.handle(receipt(2, second, 'second-charge', stars=70, at=clock[0]))
            assert db.user(MEMBER)['expires_at'] == remaining_expiry + 2 * DAY
            await app.handle(receipt(3, second, 'second-charge', stars=70, refunded=True))
            assert db.user(MEMBER)['expires_at'] == remaining_expiry
            assert db.dashboard()['stars_paid'] == 100
        finally:
            db.close()

    asyncio.run(run())
