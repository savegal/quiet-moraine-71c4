"""Bot-wide entry approval, independent of cabinet membership and subscriptions."""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from .advanced_db import ExtendedStore
from .api import Telegram, TelegramError
from .db import MAX_USER_ID, now
from .presentation import h, keyboard, pagination


log = logging.getLogger(__name__)
ENTRY_ROUTE = 'entry:'
ENTRY_LIST = ENTRY_ROUTE+'list:pending:0'
ENTRY_OPEN = ENTRY_ROUTE+'open'
ENTRY_PANEL = ENTRY_ROUTE+'panel'
MIGRATION = 'bot_entry_migration_v1'


class BotAdmission:
    def __init__(self, db: ExtendedStore, tg: Telegram, owner_id: int):
        self.db = db
        self.tg = tg
        self.owner_id = owner_id
        db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS bot_admissions (
                user_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL CHECK(state IN ('pending','approved','rejected')),
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                requested_at INTEGER NOT NULL,
                decided_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS ix_bot_admissions_state
                ON bot_admissions(state, requested_at, user_id);
        """)

    def migrate_members(self, cabinets: Iterable[ExtendedStore]) -> None:
        with self.db._write_transaction():
            if self.db.conn.execute('SELECT 1 FROM metadata WHERE key=?',(MIGRATION,)).fetchone():
                return
            insert = """INSERT OR IGNORE INTO bot_admissions(user_id,state,requested_at,decided_at)
                        VALUES (?,'approved',?,?)"""
            timestamp = now()
            for cabinet in cabinets:
                members = cabinet.conn.execute("""
                    SELECT user_id FROM users WHERE expires_at>0
                    UNION SELECT user_id FROM subscription_events WHERE previous_expires>0 OR new_expires>0
                    UNION SELECT user_id FROM accounts
                    UNION SELECT user_id FROM tasks
                """)
                self.db.conn.executemany(insert, ((row[0],timestamp,timestamp) for row in members))
            explicit = self.db.conn.execute('SELECT DISTINCT user_id FROM tokora_workspace_members')
            self.db.conn.executemany(insert, ((row[0],timestamp,timestamp) for row in explicit))
            self.db.conn.execute('INSERT INTO metadata(key,value) VALUES (?,?)',(MIGRATION,'1'))

    def state(self, uid: int) -> str:
        row = self.db.conn.execute('SELECT state FROM bot_admissions WHERE user_id=?',(uid,)).fetchone()
        return row['state'] if row else ''

    def can_enter(self, uid: int) -> bool:
        return uid==self.owner_id or self.state(uid)=='approved'

    def pending_count(self) -> int:
        return self.db.conn.execute("SELECT COUNT(*) FROM bot_admissions WHERE state='pending'").fetchone()[0]

    async def status(self, uid: int) -> None:
        state = self.state(uid)
        if uid==self.owner_id or state=='approved':
            text = '<b>Вход в бот разрешён</b>\n\nДля создания заказов нужна активная подписка.'
            rows = [('▷ Открыть бот',ENTRY_OPEN,'success')]
        elif state=='pending':
            text = '<b>Заявка на вход отправлена</b>\n\nБот сообщит здесь, когда владелец примет решение.'
            rows = [('↻ Проверить статус','entry:status',None)]
        elif state=='rejected':
            text = '<b>Заявка на вход отклонена</b>'
            rows = [('↻ Проверить статус','entry:status',None)]
        else:
            text = '<b>Запрос на вход в Proxima Soft</b>\n\nОтправь заявку владельцу, чтобы открыть меню бота.'
            rows = [('↑ Отправить заявку','entry:request','primary')]
        await self.tg.send(uid,text,keyboard(rows))

    @staticmethod
    def label(row) -> str:
        return ' '.join(filter(None,(row['first_name'],row['last_name']))) or (
            '@'+row['username'] if row['username'] else str(row['user_id']))

    async def request(self, uid: int, profile: dict) -> None:
        if uid==self.owner_id:return await self.status(uid)
        names = tuple(' '.join(str(profile.get(key) or '').split())[:64]
                      for key in ('first_name','last_name','username'))
        with self.db._write_transaction():
            added = self.db.conn.execute("""
                INSERT OR IGNORE INTO bot_admissions(user_id,state,first_name,last_name,username,requested_at)
                VALUES (?,'pending',?,?,?,?)
            """,(uid,*names,now())).rowcount
            if added:self.db.log(uid,'bot_entry_requested')
        if added:
            try:
                await self.tg.send(self.owner_id,self.request_card(uid),keyboard(
                    [('▤ Рассмотреть заявку',f'entry:view:{uid}', 'primary')]))
            except TelegramError as exc:
                log.warning('Entry request notification failed: HTTP %d',exc.code)
        await self.status(uid)

    def request_card(self, target: int) -> str:
        row = self.db.conn.execute('SELECT * FROM bot_admissions WHERE user_id=?',(target,)).fetchone()
        state = {'pending':'На рассмотрении','rejected':'Отклонена','approved':'Одобрена'}[row['state']]
        profile = h(self.label(row))
        if (row['first_name'] or row['last_name']) and row['username']:
            profile += ' · @'+h(row['username'])
        return f'<b>Заявка на вход · {state}</b>\n\n{profile}\nTelegram ID · <code>{target}</code>'

    async def requests(self, state: str, page: int) -> None:
        size = 10
        rows = self.db.conn.execute("""
            SELECT * FROM bot_admissions WHERE state=?
            ORDER BY requested_at, user_id LIMIT ? OFFSET ?
        """,(state,size+1,page*size)).fetchall()
        tabs = [('На рассмотрении',ENTRY_LIST,'primary' if state=='pending' else None),
                ('Отклонённые','entry:list:rejected:0','primary' if state=='rejected' else None)]
        items = [[(self.label(row)[:40]+f' · {row["user_id"]}',f'entry:view:{row["user_id"]}',None)]
                 for row in rows[:size]]
        caption = 'Выбери заявку.' if rows else 'На этой странице заявок нет.'
        await self.tg.send(self.owner_id,'<b>▤ Заявки на вход</b>\n\n'+caption,keyboard(
            tabs,*items,pagination(f'entry:list:{state}:',page,len(rows)>size),[('↶ Панель',ENTRY_PANEL,None)]))

    async def view(self, target: int) -> None:
        state = self.state(target)
        if not state:return await self.requests('pending',0)
        decisions = [('✓ Разрешить вход',f'entry:allow:{target}:{state}','success')] if state!='approved' else []
        if state=='pending':decisions.append(('× Отклонить',f'entry:deny:{target}:pending','danger'))
        await self.tg.send(self.owner_id,self.request_card(target),keyboard(
            decisions,[('↶ Заявки',f'entry:list:{state if state!="approved" else "pending"}:0',None)]))

    async def decide(self, target: int, expected: str, allow: bool) -> None:
        if target==self.owner_id:return
        state = 'approved' if allow else 'rejected'
        with self.db._write_transaction():
            changed = self.db.conn.execute("""
                UPDATE bot_admissions SET state=?,decided_at=? WHERE user_id=? AND state=?
            """,(state,now(),target,expected)).rowcount
            if changed:self.db.log(self.owner_id,'bot_entry_'+state,str(target))
        if changed:
            try:
                await self.status(target)
            except TelegramError as exc:
                log.warning('Entry decision notification failed: HTTP %d',exc.code)
        await self.view(target)

    async def callback(self, uid: int, data: str, profile: dict) -> None:
        if data=='entry:request':return await self.request(uid,profile)
        if data in ('entry:status', ENTRY_OPEN):return await self.status(uid)
        if uid!=self.owner_id:return
        if match := re.fullmatch(r'entry:list:(pending|rejected):([0-9]{1,5})',data):
            return await self.requests(match[1],int(match[2]))
        if match := re.fullmatch(r'entry:view:([0-9]{1,19})',data):
            if 0<int(match[1])<=MAX_USER_ID:return await self.view(int(match[1]))
        if match := re.fullmatch(r'entry:(allow|deny):([0-9]{1,19}):(pending|rejected)',data):
            target = int(match[2])
            if 0<target<=MAX_USER_ID and (match[1]=='allow' or match[3]=='pending'):
                await self.decide(target,match[3],match[1]=='allow')
