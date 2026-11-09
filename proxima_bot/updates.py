"""Durable Telegram intake with independent payment and chat processing."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from .api import TelegramError

log = logging.getLogger(__name__)
LANE_LIMITS = {'checkout': 8, 'payment': 4, 'chat': 8}
OFFSET_IDLE_LIMIT = 7*86400-60


class UpdateInbox:
    def __init__(self, app):
        self.app = app
        self.conn = app.db.conn
        self.changed = asyncio.Event()
        self.active = {lane: set() for lane in LANE_LIMITS}
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS telegram_inbox (
                update_id INTEGER PRIMARY KEY, lane TEXT NOT NULL, actor INTEGER NOT NULL,
                payload TEXT NOT NULL, received REAL NOT NULL, retry_at REAL NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, started INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS telegram_inbox_actor ON telegram_inbox(lane,actor,update_id);
        ''')
        # Ordinary callbacks may have issued a non-idempotent external write before a crash.
        # Leave the last chat action for the operator/user to verify instead of replaying it.
        interrupted = self.conn.execute("DELETE FROM telegram_inbox WHERE lane='chat' AND started=1").rowcount
        if interrupted:
            log.warning('Interrupted chat updates require checking their outcome: %d',interrupted)

    @staticmethod
    def route(update: dict) -> tuple[str, int]:
        checkout = update.get('pre_checkout_query')
        if checkout:
            return 'checkout',int(checkout['from']['id'])
        message = update.get('message') or {}
        if message.get('successful_payment') or message.get('refunded_payment'):
            actor = (message.get('chat') or {}).get('id') or (message.get('from') or {}).get('id') or 0
            return 'payment',int(actor)
        sender = ((update.get('callback_query') or message).get('from') or {}).get('id')
        return 'chat',int(sender or 0)

    def persist(self, updates: list[dict]) -> int:
        offset = max(update['update_id'] for update in updates)+1
        received = time.time()
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            for update in updates:
                lane,actor = self.route(update)
                self.conn.execute('''INSERT OR IGNORE INTO telegram_inbox(update_id,lane,actor,payload,received)
                                     VALUES (?,?,?,?,?)''',
                                  (update['update_id'],lane,actor,json.dumps(update,ensure_ascii=False),received))
            self.app.db.update_offset(offset)
            self.conn.execute("""INSERT INTO metadata(key,value) VALUES ('telegram_offset_at',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",(str(received),))
            self.conn.execute('COMMIT')
        except Exception:
            self.conn.execute('ROLLBACK')
            raise
        self.changed.set()
        return offset

    async def poll(self) -> None:
        offset = self.app.db.get_offset()
        stamp=self.conn.execute("SELECT value FROM metadata WHERE key='telegram_offset_at'").fetchone()
        last_update_at=float(stamp['value']) if stamp else time.time()
        while True:
            try:
                if time.time()-last_update_at>=OFFSET_IDLE_LIMIT:
                    offset=None
                updates = await self.app.tg.updates(offset)
                if updates:
                    offset = self.persist(updates)
                    last_update_at=time.time()
            except TelegramError as exc:
                log.warning('Telegram polling failed: HTTP %d',exc.code)
                await asyncio.sleep(max(3,exc.retry_after))
            except Exception:
                log.exception('Telegram update intake failed')
                await asyncio.sleep(3)

    def ready(self, lane: str, limit: int):
        excluded = self.active[lane]
        placeholders = ','.join('?' for _ in excluded)
        skip = f' AND item.actor NOT IN ({placeholders})' if excluded else ''
        return self.conn.execute('''SELECT item.* FROM telegram_inbox item
            WHERE item.lane=? AND item.started=0 AND item.retry_at<=?'''+skip+'''
            AND NOT EXISTS (SELECT 1 FROM telegram_inbox earlier WHERE earlier.lane=item.lane
                            AND earlier.actor=item.actor AND earlier.update_id<item.update_id)
            ORDER BY item.update_id LIMIT ?''',(lane,time.time(),*excluded,limit)).fetchall()

    async def process(self, row) -> None:
        uid,lane,update_id = row['actor'],row['lane'],row['update_id']
        try:
            if lane=='chat':
                self.conn.execute('UPDATE telegram_inbox SET started=1 WHERE update_id=?',(update_id,))
                await self.app.handle_chat_update(json.loads(row['payload']))
            else:
                await self.app.handle(json.loads(row['payload']))
        except Exception as exc:
            log.warning('Telegram %s update %d failed: %s',lane,update_id,type(exc).__name__)
            retry_payment = lane=='payment'
            retry_checkout = (lane=='checkout' and time.time()-row['received']<8
                              and (not isinstance(exc,TelegramError) or exc.code>=500 or exc.code==429))
            if retry_payment or retry_checkout:
                delay=min(60,2**min(row['attempts'],6)) if retry_payment else 0.5
                if isinstance(exc,TelegramError):
                    delay=max(delay,exc.retry_after)
                self.conn.execute('UPDATE telegram_inbox SET attempts=attempts+1,retry_at=? WHERE update_id=?',
                                  (time.time()+delay,update_id))
                return
        finally:
            self.active[lane].discard(uid)
            self.changed.set()
        self.conn.execute('DELETE FROM telegram_inbox WHERE update_id=?',(update_id,))

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self.poll())
            while True:
                self.changed.clear()
                for lane,capacity in LANE_LIMITS.items():
                    available = capacity-len(self.active[lane])
                    if available:
                        for row in self.ready(lane,available):
                            self.active[lane].add(row['actor'])
                            tasks.create_task(self.process(row))
                try:
                    await asyncio.wait_for(self.changed.wait(),timeout=0.5)
                except TimeoutError:
                    pass
