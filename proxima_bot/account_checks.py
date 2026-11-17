"""Workspace check coordination and background checks of assigned accounts."""
from __future__ import annotations

import asyncio
import json
import logging
from secrets import token_hex

from .accounts import check_status, session_state
from .api import ApiError, TelegramError, scan_active_tasks
from .bot import safe_api_error
from .db import MAX_USER_ID, OPEN_STATES, now
from .operations import VALID_ID
from .presentation import h, keyboard, number, table_html

log = logging.getLogger(__name__)
POOL_CHECK_COOLDOWN = 15 * 60
RESULT_LABELS = {
    'active': 'Активные сессии',
    'expired': 'Сессия истекла',
    'error': 'Ошибка проверки сессии',
    'unknown': 'Статус неизвестен',
    'missing': 'Аккаунт не найден',
    'busy': 'Пропущено: аккаунт занят или отвязан',
    'unconfirmed': 'API не подтвердил результат',
}


async def read_workspace_account_check(api, db) -> dict:
    marker = db.account_check_marker()
    result = await api.call('GET', '/v1/accounts/check', retries=0)
    state = check_status(result)
    if marker and state == 'idle':
        db.clear_account_check_pending(marker)
    elif state == 'active' and not marker and not db.account_check_marker() and not db.workspace_action_active():
        db.set_account_check_pending(True)
    return result


async def start_workspace_account_check(api, db) -> dict:
    with db.account_action(None):
        current = await api.call('GET', '/v1/accounts/check', retries=0)
        state = check_status(current)
        if state == 'active':
            if not db.account_check_pending():
                db.set_account_check_pending(True)
            return current
        if state != 'idle':
            raise ApiError('invalid_response')
        db.set_account_check_pending(False)
        if await scan_active_tasks(api,first_only=True,retries=0):
            raise ValueError('В кабинете есть действующее задание. Запусти проверку после его завершения.')
        db.set_account_check_pending(True)
        try:
            result = await api.call('POST', '/v1/accounts/check', retries=0)
        except ApiError as exc:
            # A timeout or an upstream 5xx can arrive after Tokora accepted the operation.
            if 400 <= exc.status < 500 and exc.status != 409 and exc.code != 'job_busy':
                db.set_account_check_pending(False)
            raise
        if check_status(result) == 'idle':
            db.set_account_check_pending(False)
        return result


async def stop_workspace_account_check(api, db, *, marker: str | None, started_at: int | None) -> dict:
    with db.account_action(None, allow_in_flight=True):
        current = await api.call('GET','/v1/accounts/check',retries=0)
        state = check_status(current)
        if state == 'idle':
            db.set_account_check_pending(False)
            return current
        if state != 'active':
            raise ApiError('invalid_response')
        if marker != db.account_check_marker() or started_at != current.get('startedAt'):
            raise ValueError('Началась другая проверка. Открой её статус перед остановкой.')
        await api.call('DELETE', '/v1/accounts/check', retries=0)
    return await read_workspace_account_check(api, db)


async def purge_workspace_accounts(api, db, confirm: str) -> int:
    """Only the dedicated server command may remove an entire unassigned pool."""
    if confirm != 'DELETE':
        raise ValueError('Для массовой очистки укажи --confirm DELETE.')
    with db.account_action(None):
        if db.conn.execute('SELECT 1 FROM accounts LIMIT 1').fetchone():
            raise ValueError('Отвяжи аккаунты от участников перед очисткой кабинета.')
        states = ','.join('?' for _ in OPEN_STATES)
        if db.conn.execute(
                f'SELECT 1 FROM tasks WHERE state IN ({states}) OR settle_poll_at>0 LIMIT 1',
                OPEN_STATES).fetchone():
            raise ValueError('В локальной очереди есть незавершённые заказы.')
        if await scan_active_tasks(api,first_only=True,retries=0):
            raise ValueError('В кабинете есть активные задания.')
        scheduled = await api.call('GET','/v1/schedules',params={'tab':'pending','limit':1},retries=0)
        if scheduled.get('items') != [] or scheduled.get('total') != 0:
            raise ValueError('В кабинете есть ожидающие задания или их статус недоступен.')
        posts = await api.call('GET','/v1/posts/queue',retries=0)
        if posts.get('total') != 0 or posts.get('active') != 0 or posts.get('retrying') != 0:
            raise ValueError('Для очистки кабинета очередь публикаций должна быть пустой.')
        check = await api.call('GET','/v1/accounts/check',retries=0)
        if check_status(check) != 'idle':
            raise ValueError('Дождись завершения общей проверки аккаунтов.')
        result = await api.call('POST','/v1/accounts/purge',payload={
            'confirm':'DELETE', 'stopActiveTasks':False},retries=0)
        deleted = result.get('deleted')
        if type(deleted) is not int or deleted < 0:
            raise ApiError('invalid_response')
        return deleted


class PoolChecks:
    """Run only documented single-account checks and persist safe progress."""

    def __init__(self, app):
        self.app = app
        self.db = app.db
        self.task: asyncio.Task | None = None
        self.running_id: str | None = None
        self.stop_requested = asyncio.Event()
        self.db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS pool_checks (
                check_id TEXT PRIMARY KEY, actor INTEGER NOT NULL, target INTEGER NOT NULL,
                state TEXT NOT NULL, total INTEGER NOT NULL, counts_json TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_pool_checks_target ON pool_checks(target,created_at);
            UPDATE pool_checks SET state='interrupted' WHERE state='running';
        """)

    async def close(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def allowed(self, uid: int, target: int) -> bool:
        return (self.app.owner(uid) or (uid == target and self.db.is_active(uid, self.app.cfg.owner_id)))

    def get(self, uid: int, check_id: str | None = None, *, target: int | None = None) -> dict | None:
        if check_id is not None:
            row = self.db.conn.execute('SELECT * FROM pool_checks WHERE check_id=?', (check_id,)).fetchone()
        else:
            row = self.db.conn.execute('SELECT * FROM pool_checks WHERE target=? ORDER BY rowid DESC LIMIT 1',
                                       (target,)).fetchone()
        if row is None or not self.allowed(uid, row['target']):
            return None
        return {**dict(row), 'counts':json.loads(row['counts_json'])}

    def back(self, uid: int, target: int) -> tuple[str,str,None]:
        route = f'adm:uaccounts:{target}' if self.app.owner(uid) else 'accounts'
        return ('↶ Аккаунты',route,None)

    async def open(self, uid: int, target: int):
        if not self.allowed(uid, target):
            return await self.app.access(uid)
        ids = self.db.account_ids(target)
        if not ids:
            return await self.app.say(uid,'В этом пуле пока нет аккаунтов.',keyboard([self.back(uid,target)]))
        last = self.get(uid, target=target)
        if last and last['state'] == 'running':
            return await self.show(uid, last['check_id'])
        token = token_hex(6)
        self.db.save_draft(uid, 'pool_check', 'confirm', {'token':token, 'target':target, 'ids':ids})
        caption = 'Проверим текущие сессии. Занятые аккаунты пропустим.'
        rich,plain = table_html('✓ Проверка сессий пула', [('Аккаунтов',number(len(ids)))], caption)
        history = [('▤ Последний результат','pc:view:'+last['check_id'],None)] if last else []
        await self.app.tg.rich(uid,rich,plain,keyboard(
            [('✓ Начать проверку','pc:run:'+token,'success')], history, [self.back(uid,target)]))

    async def show(self, uid: int, check_id: str):
        job = self.get(uid, check_id)
        if job:
            return await self.app.live_cards.open_pool_check(uid, job)

    def render(self, uid: int, job: dict):
        counts = job['counts']
        state = job['state']
        rows = [
            ('Статус', {'running':'В работе','completed':'Завершена','stopped':'Остановлена',
                        'interrupted':'Прервана','failed':'Часть результатов не получена'}[state]),
            ('Обработано',f'{number(sum(counts.values()))} / {number(job["total"])}'),
        ]
        if self.app.owner(uid):
            rows.insert(0,('Telegram ID',job['target']))
        rows.extend((label,number(counts.get(key))) for key,label in RESULT_LABELS.items()
                    if counts.get(key) or key == 'active')
        caption = job['note']
        if state == 'interrupted':
            caption = 'Бот перезапустился. Результаты уже проверенных сессий доступны в списке аккаунтов.'
        if state == 'completed':
            caption = 'Это проверка сессий. Доступность публикаций может отличаться.'
        rich,plain = table_html('✓ Проверка сессий пула', rows, caption)
        controls = []
        if state == 'running':
            controls = [('↻ Обновить','pc:view:'+job['check_id'],None),
                        ('■ Остановить','pc:stop:'+job['check_id'],'danger')]
        return rich,plain,keyboard(controls,[self.back(uid,job['target'])])

    async def callback(self, uid: int, data: str) -> bool:
        if data == 'check_pool':
            await self.open(uid, uid)
            return True
        if data.startswith('check_pool:'):
            target = data[11:]
            if self.app.owner(uid) and target.isascii() and target.isdecimal() and len(target) <= 19:
                if 0 < int(target) <= MAX_USER_ID:
                    await self.open(uid, int(target))
            return True
        if not data.startswith('pc:'):
            return False
        action,_,value = data[3:].partition(':')
        if action in ('view','stop'):
            job = self.get(uid, value)
            if job:
                if action == 'stop' and value == self.running_id:
                    self.stop_requested.set()
                await self.show(uid, value)
        elif action == 'run':
            await self.start(uid, value)
        return True

    async def start(self, uid: int, token: str):
        draft = self.db.draft(uid)
        if not draft or draft['mode'] != 'pool_check' or draft['data']['token'] != token:
            return
        if now()-draft['updated_at'] > 600:
            self.db.clear_draft(uid)
            return await self.app.say(uid,'Подтверждение устарело. Открой проверку пула снова.')
        target, ids = draft['data']['target'], draft['data']['ids']
        if not self.allowed(uid, target):
            return await self.app.access(uid)
        if self.db.account_ids(target) != ids:
            return await self.open(uid, target)
        last = self.get(uid, target=target)
        if last and not self.app.owner(uid) and now()-last['created_at'] < POOL_CHECK_COOLDOWN:
            return await self.app.say(uid,'Повторная проверка пула доступна через 15 минут после предыдущего запуска.',
                                      keyboard([('▤ Последний результат','pc:view:'+last['check_id'],None)]))
        if self.running_id or self.db.account_check_marker() or self.app.account_imports.running_id:
            return await self.app.say(uid,'Сейчас уже идёт проверка аккаунтов. Попробуй позже.',
                                      keyboard([self.back(uid,target)]))
        if not ids or any(not VALID_ID.fullmatch(aid) for aid in ids):
            return await self.app.say(uid,'В пуле нет подходящих аккаунтов для проверки.')
        check_id = token_hex(6)
        self.db.clear_draft(uid)
        self.db.conn.execute("INSERT INTO pool_checks VALUES (?,?,?,'running',?,'{}','',?)",
                             (check_id,uid,target,len(ids),now()))
        self.running_id = check_id
        self.stop_requested.clear()
        self.task = asyncio.create_task(self.run(uid, target, check_id, ids))
        return await self.show(uid, check_id)

    def save(self, check_id: str, counts: dict, state: str = 'running', note: str = ''):
        self.db.conn.execute('UPDATE pool_checks SET counts_json=?,state=?,note=? WHERE check_id=?',
                             (json.dumps(counts),state,note,check_id))

    async def check_one(self, uid: int, target: int, aid: str) -> str | None:
        for attempt in range(2):
            if self.stop_requested.is_set() or not self.allowed(uid, target):
                return None
            try:
                with self.db.account_action(aid, user_id=target):
                    account = await self.app.api.call('POST', f'/v1/accounts/{aid}/check', retries=0, background=True)
            except ValueError:
                return 'busy'
            except ApiError as exc:
                if exc.status == 404:
                    return 'missing'
                if exc.status != 429 or attempt or exc.retry_after > 60:
                    raise
                try:
                    await asyncio.wait_for(self.stop_requested.wait(), timeout=max(1, exc.retry_after))
                except TimeoutError:
                    continue
                return None
            if account.get('accountId') != aid:
                raise ApiError('invalid_response')
            return session_state(account)

    async def run(self, uid: int, target: int, check_id: str, ids: list[str]):
        counts: dict[str,int] = {}
        state, note = 'interrupted', ''
        try:
            for aid in ids:
                try:
                    outcome = await self.check_one(uid, target, aid)
                except ApiError as exc:
                    counts['unconfirmed'] = counts.get('unconfirmed',0)+1
                    if exc.status in (0,401,402,403,429) or exc.status >= 500:
                        state, note = 'failed', safe_api_error(exc.code)
                        break
                    self.save(check_id, counts)
                    continue
                if outcome is None:
                    state, note = 'stopped', 'Проверка остановлена. Следующие аккаунты не запускались.'
                    break
                counts[outcome] = counts.get(outcome,0)+1
                self.save(check_id, counts)
            else:
                state = 'failed' if counts.get('unconfirmed') else 'completed'
        except Exception as exc:
            log.warning('Account pool check failed: %s',type(exc).__name__)
            state, note = 'failed', 'Проверка прервалась. Открой список аккаунтов для текущих статусов.'
        finally:
            self.save(check_id, counts, state, note)
            self.running_id = None
