import asyncio
from contextlib import suppress
import sqlite3

import pytest

from proxima_bot.advanced_db import ExtendedStore
from proxima_bot.bot import BotApp
from proxima_bot.config import Config
from proxima_bot.updates import UpdateInbox


class TelegramStub:
    def __init__(self):
        self.incoming = asyncio.Queue()

    async def updates(self, offset):
        return await self.incoming.get()


def app_for(db):
    return BotApp(Config('fake', 'fake', 99), db, TelegramStub(), object())


def message(update_id, uid=7):
    return {'update_id': update_id, 'message': {'chat': {'type': 'private'}, 'from': {'id': uid}, 'text': '/start'}}


def test_updates_are_ordered_per_actor_without_blocking_other_actors():
    async def run():
        db = ExtendedStore(':memory:')
        app = app_for(db)
        inbox = UpdateInbox(app)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        other_processed = asyncio.Event()
        next_processed = asyncio.Event()

        async def handle(update):
            if update['update_id'] == 10:
                first_started.set()
                await release_first.wait()
            elif update['update_id'] == 11:
                next_processed.set()
            else:
                other_processed.set()

        app.handle_chat_update = handle
        app.tg.incoming.put_nowait([message(10), message(11), message(12, 8)])
        runner = asyncio.create_task(inbox.run())
        try:
            await asyncio.wait_for(first_started.wait(), timeout=2)
            await asyncio.wait_for(other_processed.wait(), timeout=2)
            assert not next_processed.is_set()
            assert db.get_offset() == 13
            release_first.set()
            await asyncio.wait_for(next_processed.wait(), timeout=2)
        finally:
            runner.cancel()
            with suppress(asyncio.CancelledError):
                await runner
            db.close()

    asyncio.run(run())


def test_intake_and_offset_rollback_together(monkeypatch):
    db = ExtendedStore(':memory:')
    try:
        db.update_offset(11)
        inbox = UpdateInbox(app_for(db))

        def fail_offset(value):
            raise sqlite3.OperationalError('simulated offset write failure')

        monkeypatch.setattr(db, 'update_offset', fail_offset)
        with pytest.raises(sqlite3.OperationalError):
            inbox.persist([message(11)])
        assert db.get_offset() == 11
        assert inbox.ready(1) == []
    finally:
        db.close()


def test_unstarted_chat_update_survives_restart(tmp_path):
    db = ExtendedStore(str(tmp_path / 'bot.db'))
    inbox = UpdateInbox(app_for(db))
    inbox.persist([message(10), message(11)])
    db.conn.execute('UPDATE telegram_inbox SET started=1 WHERE update_id=10')
    db.close()

    db = ExtendedStore(str(tmp_path / 'bot.db'))
    try:
        inbox = UpdateInbox(app_for(db))
        assert [row['update_id'] for row in inbox.ready(2)] == [11]
        assert db.get_offset() == 12
    finally:
        db.close()
