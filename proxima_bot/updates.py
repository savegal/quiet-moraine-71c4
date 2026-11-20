"""Durable Telegram intake with ordered processing per chat actor."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from .api import TelegramError

log = logging.getLogger(__name__)
CHAT_CONCURRENCY = 8
OFFSET_IDLE_LIMIT = 7*86400-60
TOKEN_TEXT = re.compile(r'tk_[A-Za-z0-9_-]{12,}')


class UpdateInbox:
    def __init__(self, app):
        self.app = app
        self.conn = app.db.conn
        self.changed = asyncio.Event()
        self.active: set[int] = set()
        self.private_updates: dict[int, dict] = {}
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS telegram_inbox (
                update_id INTEGER PRIMARY KEY, lane TEXT NOT NULL, actor INTEGER NOT NULL,
                payload TEXT NOT NULL, received REAL NOT NULL, started INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS telegram_inbox_actor ON telegram_inbox(lane,actor,update_id);
        ''')
        # Ordinary callbacks may have issued a non-idempotent external write before a crash.
        # Leave the last chat action for the operator/user to verify instead of replaying it.
        interrupted = self.conn.execute("DELETE FROM telegram_inbox WHERE lane='chat' AND started=1").rowcount
        if interrupted:
            log.warning('Interrupted chat updates require checking their outcome: %d',interrupted)

    @staticmethod
    def actor(update: dict) -> int:
        message = update.get('message') or {}
        sender = ((update.get('callback_query') or message).get('from') or {}).get('id')
        return int(sender or 0)

    def persist(self, updates: list[dict]) -> int:
        offset = max(update['update_id'] for update in updates)+1
        received = time.time()
        credential_command = getattr(self.app, 'credential_command', '')
        credential_commands = getattr(self.app, 'credential_commands', (credential_command,))
        private = {}
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            for update in updates:
                actor = self.actor(update)
                if not actor:continue
                message = update.get('message') or {}
                text = message.get('text') or ''
                words = text.split(maxsplit=1)
                is_owner = credential_command and actor == self.app.cfg.owner_id
                is_credential = credential_command and words and (
                    words[0].split('@', 1)[0].lower() in credential_commands
                    or (is_owner and TOKEN_TEXT.search(text)))
                # Keep the owner's submission in memory. The durable copy is safe to replay after a crash.
                safe = update
                if is_credential:
                    safe = {**update, 'message': {**message, 'text': credential_command}}
                payload = json.dumps(safe, ensure_ascii=False)
                if is_credential or is_owner:
                    payload = TOKEN_TEXT.sub('[ключ скрыт]', payload)
                result = self.conn.execute('''INSERT OR IGNORE INTO telegram_inbox(update_id,lane,actor,payload,received)
                                     VALUES (?,'chat',?,?,?)''',
                                  (update['update_id'],actor,payload,received))
                if (result.rowcount and is_credential and is_owner
                        and (message.get('chat') or {}).get('type') == 'private'):
                    private[update['update_id']] = update
            self.app.db.update_offset(offset)
            self.conn.execute("""INSERT INTO metadata(key,value) VALUES ('telegram_offset_at',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",(str(received),))
            self.conn.execute('COMMIT')
        except Exception:
            self.conn.execute('ROLLBACK')
            raise
        self.private_updates.update(private)
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

    def ready(self, limit: int):
        excluded = self.active
        placeholders = ','.join('?' for _ in excluded)
        skip = f' AND item.actor NOT IN ({placeholders})' if excluded else ''
        return self.conn.execute('''SELECT item.* FROM telegram_inbox item
            WHERE item.lane='chat' AND item.started=0'''+skip+'''
            AND NOT EXISTS (SELECT 1 FROM telegram_inbox earlier WHERE earlier.lane=item.lane
                            AND earlier.actor=item.actor AND earlier.update_id<item.update_id)
            ORDER BY item.update_id LIMIT ?''',(*excluded,limit)).fetchall()

    async def process(self, row) -> None:
        uid,update_id = row['actor'],row['update_id']
        try:
            self.conn.execute('UPDATE telegram_inbox SET started=1 WHERE update_id=?',(update_id,))
            update = self.private_updates.pop(update_id, None)
            await self.app.handle_chat_update(update if update is not None else json.loads(row['payload']))
        except Exception as exc:
            log.warning('Telegram chat update %d failed: %s',update_id,type(exc).__name__)
        finally:
            self.active.discard(uid)
            self.changed.set()
        self.conn.execute('DELETE FROM telegram_inbox WHERE update_id=?',(update_id,))

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self.poll())
            while True:
                self.changed.clear()
                available = CHAT_CONCURRENCY-len(self.active)
                if available:
                    for row in self.ready(available):
                        self.active.add(row['actor'])
                        tasks.create_task(self.process(row))
                try:
                    await asyncio.wait_for(self.changed.wait(),timeout=0.5)
                except TimeoutError:
                    pass
