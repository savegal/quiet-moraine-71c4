from __future__ import annotations
from datetime import datetime
import json
import sqlite3
import time
import uuid
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from .accounts import valid_task_folder

OPEN_STATES = ('queued','reconcile','starting','running','paused','stopping')
ACTIVE_STATES = ('starting','running','paused','stopping')
TERMINAL_STATES = ('completed','stopped','failed','canceled')
RECONCILE_HOLD_CODES = ('reconcile_review','reconcile_access_revoked')
MAX_USER_ID = 2**63 - 1
ACCOUNT_ACTION_TTL = 15 * 60
WORKSPACE_ACTION = ''
IN_FLIGHT_STATES = (*ACTIVE_STATES, 'reconcile')
IN_FLIGHT_SQL = "state IN (?,?,?,?,?) OR (state='unknown' AND upstream_id IS NOT NULL)"
UNKNOWN_TASK_POOL = 'Неизвестен пул незавершённой задачи. Открой сверку заказа в панели владельца.'


def now() -> int:
    return int(time.time())


PER_LINK_EXECUTION_SECONDS = 10 * 60
PREPARE_PHASES = frozenset({'prepare', 'preparing', 'init', 'warming_prepare'})
PUBLICATION_TIMER_KINDS = ('publish', 'keyword_reply', 'commentlike')
PUBLICATION_PHASES = frozenset({'publish', 'publishing', 'comments', 'replies',
                                'likes', 'liking', 'commentlike', 'like_comments'})


def snapshot_publication_started(snapshot: dict) -> bool:
    phase = str(snapshot.get('phase') or '').strip().lower()
    return phase in PUBLICATION_PHASES or any(
        type(value) is int and value > 0
        for value in (snapshot.get('attempts'), snapshot.get('published'), snapshot.get('likeAttempts'))
    )


def snapshot_preparing(snapshot: dict | None) -> bool:
    """True while the engine is still preparing accounts before publication attempts begin."""
    if not isinstance(snapshot, dict):
        return False
    phase = str(snapshot.get('phase') or '').strip().lower()
    if phase not in PREPARE_PHASES:
        return False
    return not snapshot_publication_started(snapshot)


def task_target_links_count(task_or_payload: dict) -> int:
    """Count target links for execution-time limits (10 min per link, minimum 1 link)."""
    if not isinstance(task_or_payload, dict):
        return 1
    if any(k in task_or_payload for k in ('assets', 'awemeIds', 'replyLinks', 'likeLinks', 'sourceJob', 'likeSourceJob')):
        payload = task_or_payload
        snapshot = {}
    else:
        try:
            payload = json.loads(task_or_payload.get('payload_json') or '{}')
        except (TypeError, ValueError):
            payload = {}
        try:
            snapshot = json.loads(task_or_payload.get('snapshot_json') or '{}')
        except (TypeError, ValueError):
            snapshot = {}
    if isinstance(payload.get('awemeIds'), list) and payload['awemeIds']:
        return len(payload['awemeIds'])
    if isinstance(payload.get('replyLinks'), list) and payload['replyLinks']:
        if payload.get('sourceJob'):
            return max(1, len({str(item).split(':', 1)[0] for item in payload['replyLinks']}))
        return len(payload['replyLinks'])
    if isinstance(payload.get('likeLinks'), list) and payload['likeLinks']:
        if payload.get('likeSourceJob'):
            return max(1, len({str(item).split(':', 1)[0] for item in payload['likeLinks']}))
        return len(payload['likeLinks'])
    if payload.get('sourceJob') and not payload.get('replySourceMode') and isinstance(payload.get('targetIds'),list):
        return max(1,len(payload['targetIds']))
    awemes = snapshot.get('awemes') if isinstance(snapshot, dict) else None
    aweme_count = len(awemes) if isinstance(awemes, list) and awemes else 0
    try:
        stored_count = int(task_or_payload.get('target_count') or 0)
    except (TypeError, ValueError):
        stored_count = 0
    if aweme_count or stored_count > 0:
        return max(1, aweme_count, stored_count)
    return 1


def task_execution_limit_seconds(task_or_payload: dict) -> int:
    """Execution limit in seconds once publications begin: 10 minutes per target link."""
    return max(1, task_target_links_count(task_or_payload)) * PER_LINK_EXECUTION_SECONDS


def local_day_start(timestamp: int, timezone_name: str = 'Europe/Moscow') -> int:
    """Unix timestamp of midnight in the configured timezone."""
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = ZoneInfo('UTC')
    local_dt = datetime.fromtimestamp(timestamp, tz)
    midnight = local_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def settlement_deadline(snapshot: dict) -> int:
    value=snapshot.get('settleUntil')
    if type(value) is not int or value<=0:
        return 0
    # Tokora uses Unix milliseconds in account and job metadata.
    return (value+999)//1000 if value>=1_000_000_000_000 else value


def task_updates_live(task: dict) -> bool:
    return task['state'] in OPEN_STATES or bool(task.get('settle_poll_at'))


def remote_task_name(request_id: str, display_name: str) -> str:
    """A unique remote label while retaining the full local display name."""
    marker = '[PX-' + request_id.replace('-', '') + ']'
    return f'{marker} {display_name}'[:60].rstrip()


def payload_account_ids(payload: dict) -> list[str]:
    assets = payload.get('assets') if isinstance(payload, dict) else None
    ids = assets.get('accountIds') if isinstance(assets, dict) else None
    if not isinstance(ids, list) or any(not isinstance(aid, str) or not aid for aid in ids):
        raise ValueError('Для задания нужны явные ID аккаунтов.')
    return list(dict.fromkeys(ids))


class Store:
    """Single-worker WAL database. Atomic changes; one bot process only."""
    def __init__(self, location: str, *, timezone_name: str = 'Europe/Moscow'):
        self.timezone_name = timezone_name
        if location != ':memory:':
            Path(location).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(location, isolation_level=None, timeout=20)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA busy_timeout=20000')
        self.conn.executescript('''
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, first_seen INTEGER NOT NULL,
            expires_at INTEGER NOT NULL DEFAULT 0, blocked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS accounts (
            account_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(user_id)
        );
        CREATE INDEX IF NOT EXISTS ix_accounts_user ON accounts(user_id);
        CREATE TABLE IF NOT EXISTS account_actions (
            account_id TEXT PRIMARY KEY, token TEXT NOT NULL, expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS drafts (
            user_id INTEGER PRIMARY KEY, mode TEXT NOT NULL, step TEXT NOT NULL,
            data_json TEXT NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS imported_owner_access (account_id TEXT NOT NULL,target_id INTEGER NOT NULL,owner_id INTEGER NOT NULL,PRIMARY KEY(account_id,owner_id));
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
            display_name TEXT NOT NULL, kind TEXT NOT NULL,
            request_id TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL,
            upstream_id TEXT UNIQUE, state TEXT NOT NULL,
            error_code TEXT NOT NULL DEFAULT '',
            snapshot_json TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_tasks_queued ON tasks(state, created_at);
        CREATE INDEX IF NOT EXISTS ix_tasks_owner ON tasks(user_id, created_at);
        CREATE TABLE IF NOT EXISTS followups (
            source_task_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            child_task_id TEXT NOT NULL,
            PRIMARY KEY(source_task_id,mode)
        );
        ''')
        fields = {row[1] for row in self.conn.execute('PRAGMA table_info(tasks)')}
        if 'execution_started_at' not in fields:
            self.conn.execute('ALTER TABLE tasks ADD COLUMN execution_started_at INTEGER NOT NULL DEFAULT 0')
        if 'target_retry_at' not in fields:
            self.conn.execute('ALTER TABLE tasks ADD COLUMN target_retry_at INTEGER NOT NULL DEFAULT 0')
        if 'target_resolution_attempts' not in fields:
            self.conn.execute('ALTER TABLE tasks ADD COLUMN target_resolution_attempts INTEGER NOT NULL DEFAULT 0')
        if 'fire_at' not in fields:
            self.conn.execute('ALTER TABLE tasks ADD COLUMN fire_at INTEGER NOT NULL DEFAULT 0')
        if 'hidden' not in fields:
            self.conn.execute('ALTER TABLE tasks ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0')
        if 'reconcile_attempts' not in fields:
            self.conn.execute('ALTER TABLE tasks ADD COLUMN reconcile_attempts INTEGER NOT NULL DEFAULT 0')
        if 'preflight_json' not in fields:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN preflight_json TEXT NOT NULL DEFAULT '{}'")
        if 'launch_payload_json' not in fields:
            self.conn.execute('ALTER TABLE tasks ADD COLUMN launch_payload_json TEXT')
        if 'settle_poll_at' not in fields:
            self.conn.execute('ALTER TABLE tasks ADD COLUMN settle_poll_at INTEGER NOT NULL DEFAULT 0')
            for row in self.conn.execute('SELECT task_id, updated_at, snapshot_json FROM tasks WHERE state IN (?,?,?,?) '
                                         "AND upstream_id IS NOT NULL AND snapshot_json LIKE '%\"settleUntil\"%'",
                                         TERMINAL_STATES).fetchall():
                if settlement_deadline(json.loads(row['snapshot_json']))>row['updated_at']:
                    self.conn.execute('UPDATE tasks SET settle_poll_at=? WHERE task_id=?',(now(),row['task_id']))
        if 'target_count' not in fields:
            self.conn.execute('ALTER TABLE tasks ADD COLUMN target_count INTEGER NOT NULL DEFAULT 0')
        self.conn.execute('CREATE INDEX IF NOT EXISTS ix_tasks_settling ON tasks(settle_poll_at) WHERE settle_poll_at>0')

    def close(self) -> None:
        self.conn.close()

    def update_offset(self, value:int)->None:
        self.conn.execute('INSERT INTO metadata(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('telegram_offset',str(value)))

    def get_offset(self)->int|None:
        row=self.conn.execute('SELECT value FROM metadata WHERE key=?',('telegram_offset',)).fetchone()
        return int(row['value']) if row else None

    def register(self, user_id: int) -> None:
        if not 0<user_id<=MAX_USER_ID:raise ValueError('Некорректный Telegram ID.')
        self.conn.execute('INSERT OR IGNORE INTO users(user_id,first_seen) VALUES (?,?)', (user_id,now()))

    def user(self, uid: int) -> dict | None:
        row = self.conn.execute('SELECT * FROM users WHERE user_id=?',(uid,)).fetchone()
        return dict(row) if row else None

    def is_active(self, uid: int, owner_id: int) -> bool:
        if uid == owner_id:
            return True
        r=self.user(uid)
        return bool(r and not r['blocked'] and r['expires_at']>now())

    def grant(self, uid: int, days: int) -> int:
        self.register(uid)
        prev=self.user(uid)['expires_at']
        expiry=max(now(),prev)+days*86400
        self.conn.execute('UPDATE users SET expires_at=?,blocked=0 WHERE user_id=?',(expiry,uid))
        return expiry

    def block(self, uid: int) -> None:
        self.register(uid)
        self.conn.execute('UPDATE users SET blocked=1 WHERE user_id=?',(uid,))

    @contextmanager
    def _write_transaction(self):
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.conn.execute('COMMIT')
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute('ROLLBACK')
            raise

    def _task_accounts(self, *, include_queued: bool = False, exclude_task: str = '') -> tuple[set[str], bool]:
        conditions = IN_FLIGHT_SQL
        if include_queued:
            conditions += " OR state='queued'"
        query = f'SELECT state,payload_json,launch_payload_json FROM tasks WHERE ({conditions}) AND task_id<>?'
        accounts = set()
        unknown_pool = False
        for row in self.conn.execute(query, (*IN_FLIGHT_STATES, exclude_task)):
            sent = row['launch_payload_json'] if row['state'] != 'queued' else None
            try:
                ids = payload_account_ids(json.loads(sent or row['payload_json']))
            except (TypeError, ValueError):
                ids = []
            if not ids and row['state'] != 'queued':
                unknown_pool = True
            accounts.update(ids)
        return accounts, unknown_pool

    def _leased_accounts(self) -> set[str]:
        return {row['account_id'] for row in self.conn.execute(
            'SELECT account_id FROM account_actions WHERE expires_at>?', (now(),))}

    def workspace_action_active(self) -> bool:
        return self.conn.execute(
            'SELECT 1 FROM account_actions WHERE account_id=? AND expires_at>?',
            (WORKSPACE_ACTION, now())
        ).fetchone() is not None

    def account_actions_pending(self, ids: list[str]) -> bool:
        return bool(self._leased_accounts().intersection((*ids, WORKSPACE_ACTION)))

    def account_action_versions(self, ids: Iterable[str]) -> dict[str, str]:
        versions = dict.fromkeys((*ids, WORKSPACE_ACTION), '')
        for row in self.conn.execute('SELECT account_id,token FROM account_actions'):
            if row['account_id'] in versions:
                versions[row['account_id']] = row['token']
        return versions

    def account_in_use(self, aid: str) -> bool:
        """Check the sent or uncertain launch pool and current maintenance leases."""
        used, unknown_pool = self._task_accounts()
        return unknown_pool or aid in used or self.account_actions_pending([aid])

    @contextmanager
    def account_action(self, aid: str | None, *, user_id: int | None = None,
                       require_unassigned: bool = False, allow_in_flight: bool = False):
        """Reserve an account, or the workspace, without holding a transaction over HTTP."""
        token = uuid.uuid4().hex
        account = WORKSPACE_ACTION if aid is None else aid
        with self._write_transaction():
            leased = self._leased_accounts()
            if aid is None:
                if allow_in_flight:
                    active = None
                elif user_id is not None:
                    active = self.conn.execute(
                        'SELECT 1 FROM tasks WHERE user_id=? AND ' + IN_FLIGHT_SQL + ' LIMIT 1',
                        (user_id, *IN_FLIGHT_STATES),
                    ).fetchone()
                else:
                    active = self.conn.execute(
                        'SELECT 1 FROM tasks WHERE ' + IN_FLIGHT_SQL + ' LIMIT 1', IN_FLIGHT_STATES
                    ).fetchone()
                if leased or active:
                    raise ValueError('Дождись завершения задач и проверок в кабинете.')
            else:
                if not aid or allow_in_flight or (user_id is not None and require_unassigned):
                    raise ValueError('Некорректные параметры аккаунта.')
                if self.account_check_pending():
                    raise ValueError('Дождись завершения общей проверки аккаунтов.')
                used, unknown_pool = self._task_accounts()
                if unknown_pool:
                    raise ValueError(UNKNOWN_TASK_POOL)
                if WORKSPACE_ACTION in leased or aid in leased or aid in used:
                    raise ValueError('Аккаунт занят задачей или проверкой.')
                owner = self.conn.execute('SELECT user_id FROM accounts WHERE account_id=?', (aid,)).fetchone()
                if require_unassigned and owner is not None:
                    raise ValueError('Сначала отвяжи аккаунт от участника.')
                if user_id is not None and (owner is None or owner['user_id'] != user_id):
                    raise ValueError('Аккаунт больше не назначен этому участнику.')
            self.conn.execute('INSERT OR REPLACE INTO account_actions VALUES (?,?,?)',
                              (account, token, now() + ACCOUNT_ACTION_TTL))
        try:
            yield
        finally:
            # Keep the token so preflight can detect a mutation completed during its GET.
            self.conn.execute('UPDATE account_actions SET expires_at=0 WHERE account_id=? AND token=?',
                              (account, token))

    def account_check_marker(self) -> str | None:
        row = self.conn.execute("SELECT value FROM metadata WHERE key='account_check_pending'").fetchone()
        return row['value'] if row else None

    def account_check_pending(self) -> bool:
        return self.account_check_marker() is not None

    def set_account_check_pending(self, pending: bool) -> None:
        if pending:
            self.conn.execute("INSERT OR REPLACE INTO metadata VALUES ('account_check_pending',?)",
                              (uuid.uuid4().hex,))
        else:
            self.conn.execute("DELETE FROM metadata WHERE key='account_check_pending'")

    def dispatch_paused(self) -> bool:
        row = self.conn.execute("SELECT value FROM metadata WHERE key='dispatch_paused'").fetchone()
        return bool(row and row['value'] == '1')

    def set_dispatch_paused(self, paused: bool) -> None:
        if paused:
            self.conn.execute("INSERT OR REPLACE INTO metadata VALUES ('dispatch_paused','1')")
        else:
            self.conn.execute("DELETE FROM metadata WHERE key='dispatch_paused'")

    def clear_account_check_pending(self, marker: str) -> bool:
        return self.conn.execute('''
            DELETE FROM metadata WHERE key='account_check_pending' AND value=?
              AND NOT EXISTS (SELECT 1 FROM account_actions WHERE account_id=? AND expires_at>?)
        ''', (marker, WORKSPACE_ACTION, now())).rowcount == 1

    def assign(self, uid: int, account_id: str, *, allow_transfer:bool=True) -> bool:
        with self._write_transaction():
            row=self.conn.execute('SELECT user_id FROM accounts WHERE account_id=?',(account_id,)).fetchone()
            if row and row['user_id'] == uid:
                return True
            if row and not allow_transfer:
                return False
            used, unknown_pool = self._task_accounts(include_queued=True)
            if unknown_pool:
                raise ValueError(UNKNOWN_TASK_POOL)
            if self.account_actions_pending([account_id]) or account_id in used:
                if not allow_transfer:
                    return False
                raise ValueError('Аккаунт занят заданием или проверкой.')
            self.register(uid)
            # Clear any stale targeted-import owner access before assigning/transferring.
            self.conn.execute('DELETE FROM imported_owner_access WHERE account_id=?',(account_id,))
            self.conn.execute('INSERT INTO accounts(account_id,user_id) VALUES (?,?) ON CONFLICT(account_id) DO UPDATE SET user_id=excluded.user_id',(account_id,uid))
            return True

    def unassign(self, uid: int, account_id: str) -> bool:
        with self._write_transaction():
            assigned = self.conn.execute('SELECT 1 FROM accounts WHERE account_id=? AND user_id=?',
                                         (account_id, uid)).fetchone()
            owner_grant = None
            if not assigned:
                owner_grant = self.conn.execute(
                    'SELECT 1 FROM imported_owner_access WHERE account_id=? AND owner_id=?',
                    (account_id, uid),
                ).fetchone()
                if not owner_grant:
                    return False
            used, unknown_pool = self._task_accounts(include_queued=True)
            if unknown_pool:
                raise ValueError(UNKNOWN_TASK_POOL)
            if self.account_actions_pending([account_id]) or account_id in used:
                raise ValueError('Аккаунт занят заданием или проверкой.')
            if assigned:
                self.conn.execute('DELETE FROM imported_owner_access WHERE account_id=?', (account_id,))
                return self.conn.execute('DELETE FROM accounts WHERE account_id=? AND user_id=?',
                                         (account_id, uid)).rowcount == 1
            return self.conn.execute('DELETE FROM imported_owner_access WHERE account_id=? AND owner_id=?',
                                     (account_id, uid)).rowcount == 1

    def account_ids(self, uid: int) -> list[str]:
        return [r['account_id'] for r in self.conn.execute('SELECT account_id FROM accounts WHERE user_id=? UNION SELECT a.account_id FROM accounts a JOIN imported_owner_access g ON a.account_id=g.account_id AND a.user_id=g.target_id WHERE g.owner_id=? ORDER BY account_id',(uid,uid))]

    def account_assignee(self, account_id:str) -> int|None:
        row = self.conn.execute('SELECT user_id FROM accounts WHERE account_id=?',(account_id,)).fetchone()
        return row['user_id'] if row else None

    def has_other_account_assignees(self, uid:int) -> bool:
        return self.conn.execute('SELECT 1 FROM accounts WHERE user_id<>? LIMIT 1',(uid,)).fetchone() is not None

    def eligible_task_accounts(self, uid:int, ids:list[str], owner_id:int|None) -> list[str]:
        if uid == owner_id:
            return list(ids)
        assigned = set(self.account_ids(uid))
        return [aid for aid in ids if aid in assigned]

    def save_draft(self, uid:int, mode:str, step:str, data:dict) -> None:
        previous=self.draft(uid)
        if mode in ('local_template','template_rename') and previous:
            if previous['mode'] in ('publish','reply_links','fetch','commentlike','keyword_reply','feedwarm','usernames','source_reply','media_order'):
                data={**data,'_return_draft':{key:previous[key] for key in ('mode','step','data')}}
            elif previous['data'].get('_return_draft') and '_return_draft' not in data:
                data={**data,'_return_draft':previous['data']['_return_draft']}
        self.conn.execute('INSERT OR REPLACE INTO drafts VALUES (?,?,?,?,?)',(uid,mode,step,json.dumps(data,ensure_ascii=False),now()))

    def draft(self, uid:int) -> dict | None:
        r=self.conn.execute('SELECT * FROM drafts WHERE user_id=?',(uid,)).fetchone()
        return {**dict(r),'data':json.loads(r['data_json'])} if r else None

    def clear_draft(self, uid:int) -> None:
        current=self.draft(uid)
        backup=current['data'].get('_return_draft') if current else None
        self.conn.execute('DELETE FROM drafts WHERE user_id=?',(uid,))
        if backup:
            self.save_draft(uid,backup['mode'],backup['step'],backup['data'])

    def queue_task(self, uid:int, name:str, kind:str, payload:dict, *, followup_source:str|None=None, followup_mode:str|None=None, max_daily:int=100, max_open:int=3, fire_at:int=0, draft_token:str|None=None, target_count:int=0) -> str:
        timestamp = now()
        if fire_at and not timestamp + 60 <= fire_at <= timestamp + 365 * 86400:
            raise ValueError('Укажи время запуска от 1 минуты до 365 дней вперёд.')
        task_id=uuid.uuid4().hex[:8]
        request_id=str(uuid.uuid4())
        payload={**payload,'name':remote_task_name(request_id,name),'kind':kind,'requestId':request_id}
        resolved_targets = max(0, int(target_count or 0))
        # BEGIN IMMEDIATE ensures account ownership and queue insert are consistent.
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            if draft_token is not None:
                draft=self.draft(uid)
                if not draft or draft['step']!='review' or draft['data'].get('wizard_token')!=draft_token:
                    raise ValueError('Подтверждение устарело. Открой актуальный черновик.')
            if max_daily>0:
                day_start=local_day_start(now(), getattr(self, 'timezone_name', 'Europe/Moscow'))
                daily=self.conn.execute('SELECT COUNT(*) FROM tasks WHERE user_id=? AND created_at>=?',
                    (uid,day_start)).fetchone()[0]
                if daily>=max_daily:raise ValueError('Достигнут дневной лимит заданий.')
            if max_open>0:
                opened=self.conn.execute("SELECT COUNT(*) FROM tasks WHERE user_id=? AND state IN (?,?,?,?,?,?)",(uid,*OPEN_STATES)).fetchone()[0]
                if opened>=max_open:raise ValueError('Превышено количество одновременно ожидающих или идущих заданий.')
            ids=payload_account_ids(payload)
            if not ids or ids != self.eligible_task_accounts(uid,ids,getattr(self,'owner_id',None)):
                raise ValueError('Назначенные аккаунты изменились. Проверь заказ и подтверди его снова.')
            if followup_source and not resolved_targets:
                source_row=self.conn.execute('SELECT * FROM tasks WHERE task_id=?',(followup_source,)).fetchone()
                if source_row:
                    resolved_targets=task_target_links_count(dict(source_row))
            self.register(uid)
            self.conn.execute('INSERT INTO tasks(task_id,user_id,display_name,kind,request_id,payload_json,upstream_id,state,created_at,updated_at,fire_at,target_count) VALUES (?,?,?,?,?,?,NULL,?,?,?,?,?)',
                (task_id,uid,name,kind,request_id,json.dumps(payload,ensure_ascii=False),'queued',timestamp,timestamp,fire_at,resolved_targets))
            if followup_source:
                if followup_mode not in ('reply','like'):
                    raise ValueError('Unsupported followup')
                prev=self.conn.execute('SELECT child_task_id FROM followups WHERE source_task_id=? AND mode=?',
                    (followup_source,followup_mode)).fetchone()
                if prev:
                    last=self.conn.execute('SELECT state FROM tasks WHERE task_id=?',(prev['child_task_id'],)).fetchone()
                    if last and last['state'] not in (*TERMINAL_STATES,'unknown'):
                        raise sqlite3.IntegrityError('Active followup already exists')
                    self.conn.execute('UPDATE followups SET child_task_id=? WHERE source_task_id=? AND mode=?',
                        (task_id,followup_source,followup_mode))
                else:
                    self.conn.execute('INSERT INTO followups VALUES (?,?,?)',
                        (followup_source,followup_mode,task_id))
            if draft_token is not None:
                self.clear_draft(uid)
            self.conn.execute('COMMIT')
        except Exception:
            self.conn.execute('ROLLBACK')
            raise
        return task_id

    def task(self, task_id:str, uid:int|None=None) -> dict | None:
        r=self.conn.execute('SELECT * FROM tasks WHERE task_id=?'+(' AND user_id=?' if uid is not None else ''),
                            (task_id,uid) if uid is not None else (task_id,)).fetchone()
        return dict(r) if r else None

    def task_by_upstream(self, upstream_id: str, uid: int) -> dict | None:
        row = self.conn.execute('SELECT * FROM tasks WHERE upstream_id=? AND user_id=?', (upstream_id, uid)).fetchone()
        return dict(row) if row else None

    def recent_tasks(self, uid:int, limit:int=10, offset:int=0, *, include_hidden:bool=False) -> list[dict]:
        visibility = '' if include_hidden else ' AND hidden=0'
        return [dict(r) for r in self.conn.execute(
            'SELECT * FROM tasks WHERE user_id=?'+visibility+' ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?',
            (uid,limit,offset))]

    def task_counts(self, uid:int) -> dict[str,int]:
        return dict(self.conn.execute('SELECT state,COUNT(*) FROM tasks WHERE user_id=? GROUP BY state',(uid,)))

    def hide_task(self, uid:int, task_id:str) -> bool:
        return self.conn.execute(
            'UPDATE tasks SET hidden=1 WHERE user_id=? AND task_id=? AND hidden=0 AND state IN (?,?,?,?)',
            (uid,task_id,*TERMINAL_STATES)).rowcount == 1

    def pending(self) -> dict | None:
        r=self.conn.execute('SELECT * FROM tasks WHERE state=? ORDER BY created_at,rowid LIMIT 1',('queued',)).fetchone()
        return dict(r) if r else None

    def running(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM tasks WHERE state IN (?,?,?,?)",ACTIVE_STATES)]

    def due_settlements(self, timestamp:int, limit:int=5) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            'SELECT * FROM tasks WHERE settle_poll_at>0 AND settle_poll_at<=? ORDER BY settle_poll_at,rowid LIMIT ?',
            (timestamp,limit))]

    def defer_settlement(self, job:dict, poll_at:int) -> None:
        self.conn.execute('UPDATE tasks SET settle_poll_at=? WHERE task_id=? AND settle_poll_at=? AND snapshot_json=?',
                          (poll_at,job['task_id'],job['settle_poll_at'],job['snapshot_json']))

    def record_target_resolution_failure(self, task_id: str, attempts: int, retry_at: int) -> None:
        state, error = ('queued', 'short_link_retry') if retry_at else ('failed', 'short_link_unresolved')
        self.conn.execute('''UPDATE tasks SET state=?, error_code=?, target_resolution_attempts=?,
                          target_retry_at=?, updated_at=? WHERE task_id=? AND state='queued'
                          AND upstream_id IS NULL AND launch_payload_json IS NULL''',
                          (state, error, attempts, retry_at, now(), task_id))

    def defer_launch(self, task_id:str, state:str, code:str, retry_at:int, preflight:dict) -> None:
        self.conn.execute('''UPDATE tasks SET error_code=?, target_retry_at=?, preflight_json=?, updated_at=?
                             WHERE task_id=? AND state=? AND upstream_id IS NULL AND error_code NOT IN (?,?)''',
                          (code,retry_at,json.dumps(preflight,ensure_ascii=False),now(),task_id,state,*RECONCILE_HOLD_CODES))

    def unresolved_tasks(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM tasks WHERE state='reconcile' ORDER BY updated_at, rowid"
        )]

    def claim_start(self, task_id: str, expected_state: str, owner_id: int, *, launch_payload:dict|None=None,
                    account_versions:dict[str,str]|None=None, max_active:int=1,
                    replay_scope: list[str] | None = None) -> bool:
        if expected_state not in ('queued','reconcile'):
            return False
        with self._write_transaction():
            task = self.task(task_id)
            if not task or task['state'] != expected_state or self.dispatch_paused() or self.account_check_pending():
                return False
            if account_versions is not None and self.account_action_versions(account_versions) != account_versions:
                return False
            try:
                saved_payload = json.loads(task['launch_payload_json'] or task['payload_json'])
            except (TypeError, ValueError):
                return False
            if launch_payload is None:
                launch_payload = saved_payload
            elif (task['launch_payload_json'] or expected_state=='reconcile') and launch_payload != saved_payload:
                return False
            if launch_payload.get('usePhoto') and task['user_id']!=owner_id:
                can_use_photos=getattr(self,'can_use_photos',None)
                if can_use_photos is None or not can_use_photos(task['user_id']):
                    return False
            try:
                saved_ids = set(payload_account_ids(saved_payload))
                launch_ids = set(payload_account_ids(launch_payload))
            except ValueError:
                return False
            reservation = launch_payload
            if replay_scope is not None:
                if (expected_state != 'reconcile' or task['user_id'] != owner_id
                        or not valid_task_folder(launch_payload.get('accountFolder'), allow_workspace=True)
                        or account_versions is None or not set(replay_scope) <= account_versions.keys()):
                    return False
                # The wire request ignores accountIds. Reserve both the original pool and any
                # accounts a replay can now reach if the first request never created a task.
                launch_ids.update(replay_scope)
                reservation = {**launch_payload, 'assets': {**launch_payload['assets'], 'accountIds': sorted(launch_ids)}}
            used, unknown_pool = self._task_accounts(exclude_task=task_id)
            owner_cabinet_scope = (
                task['user_id'] == owner_id
                and expected_state == 'queued'
                and not task['launch_payload_json']
                and valid_task_folder(saved_payload.get('accountFolder'), allow_workspace=True)
                and launch_payload.get('accountFolder') == saved_payload.get('accountFolder')
            )
            if (unknown_pool or not launch_ids
                    or (not owner_cabinet_scope and replay_scope is None and (not launch_ids <= saved_ids
                        or saved_ids != set(self.eligible_task_accounts(task['user_id'],list(saved_ids),owner_id))))
                    or launch_ids != set(self.eligible_task_accounts(task['user_id'],list(launch_ids),owner_id))
                    or launch_ids.intersection(used)
                    or self.account_actions_pending(list(launch_ids))):
                return False
            timestamp = now()
            cursor = self.conn.execute('''
                UPDATE tasks
                   SET state='starting', updated_at=?,
                       launch_payload_json=?,
                       reconcile_attempts=reconcile_attempts+CASE WHEN state='reconcile' THEN 1 ELSE 0 END
                 WHERE task_id=? AND state=? AND fire_at<=? AND target_retry_at<=? AND upstream_id IS NULL
                   AND error_code NOT IN (?,?)
                   AND (user_id=? OR user_id IN (SELECT user_id FROM users WHERE blocked=0 AND expires_at>?))
                   AND (SELECT COUNT(*) FROM tasks WHERE state IN (?,?,?,?)) < ?
            ''',(timestamp,json.dumps(reservation,ensure_ascii=False),task_id,expected_state,timestamp,timestamp,
                 *RECONCILE_HOLD_CODES,owner_id,timestamp,*ACTIVE_STATES,max_active))
            return cursor.rowcount == 1

    def hold_reconciliation(self, task_id: str) -> dict | None:
        # Holding an ambiguous launch stops POST retries; read-only matching continues.
        self.conn.execute(
            "UPDATE tasks SET error_code='reconcile_review', updated_at=? "
            "WHERE task_id=? AND state='reconcile' AND error_code!='reconcile_review'",
            (now(),task_id)
        )
        task = self.task(task_id)
        return task if task and task['state']=='reconcile' else None

    def settle_reconciliation_absent(self, held_task: dict) -> bool:
        if held_task['state']!='reconcile' or held_task['error_code']!='reconcile_review':
            return False
        cursor = self.conn.execute('''
            UPDATE tasks SET state='canceled', error_code='upstream_absence_confirmed', updated_at=?
            WHERE task_id=? AND state='reconcile' AND error_code='reconcile_review'
              AND updated_at=? AND reconcile_attempts=? AND upstream_id IS ?
        ''',(now(),held_task['task_id'],held_task['updated_at'],held_task['reconcile_attempts'],held_task['upstream_id']))
        return cursor.rowcount == 1

    def transition(self, task_id:str, state:str, *, upstream_id:str|None=None, error_code:str|None=None,
                   snapshot:dict|None=None, preflight:dict|None=None, expected_state:str|None=None,
                   expected_snapshot:str|None=None, observed_at:int|None=None,
                   clear_launch_payload:bool=False) -> bool:
        sets=['state=?','updated_at=?']; args:list[Any]=[state,now()]
        if state in ('running','paused','stopping') and (upstream_id is not None or snapshot is not None):
            kinds = ','.join('?' for _ in PUBLICATION_TIMER_KINDS)
            sets.append(f'''execution_started_at=CASE
                WHEN execution_started_at=0 AND (
                    (kind IN ({kinds}) AND ?)
                    OR (kind NOT IN ({kinds}) AND ?)
                ) THEN ? ELSE execution_started_at END''')
            args.extend((*PUBLICATION_TIMER_KINDS, snapshot_publication_started(snapshot or {}),
                         *PUBLICATION_TIMER_KINDS, not snapshot_preparing(snapshot), now()))
        if upstream_id is not None:
            sets.append('upstream_id=?');args.append(upstream_id)
        if error_code is not None:
            sets.append('error_code=?');args.append(error_code)
        if snapshot is not None:
            sets.append('snapshot_json=?');args.append(json.dumps(snapshot,ensure_ascii=False))
            observed=now() if observed_at is None else observed_at
            deadline=settlement_deadline(snapshot)
            sets.append('settle_poll_at=?')
            args.append(min(deadline,observed+5) if state in TERMINAL_STATES and deadline>observed else 0)
        if preflight is not None:
            sets.append('preflight_json=?');args.append(json.dumps(preflight,ensure_ascii=False))
        if clear_launch_payload:
            sets.append('launch_payload_json=NULL')
        args.append(task_id)
        where = ' WHERE task_id=?'
        if expected_state is not None:
            where += ' AND state=?'
            args.append(expected_state)
        if expected_snapshot is not None:
            where += ' AND snapshot_json=?'
            args.append(expected_snapshot)
        return self.conn.execute('UPDATE tasks SET '+','.join(sets)+where,args).rowcount == 1

    def recover(self) -> None:
        # Preserve request IDs across restarts for idempotent Tokora POST /tasks retries.
        self.conn.execute("UPDATE tasks SET state='reconcile' WHERE state='starting' AND upstream_id IS NULL")
        # Older versions stopped tracking a task after a single not-found response.
        self.conn.execute("UPDATE tasks SET state='reconcile' WHERE state='unknown' AND upstream_id IS NOT NULL")
        # Older versions froze even definitely rejected launches. A queued retry
        # must use fresh account statuses while uncertain launches keep their exact payload.
        self.conn.execute("UPDATE tasks SET launch_payload_json=NULL WHERE state='queued' AND upstream_id IS NULL "
                          "AND launch_payload_json IS NOT NULL")

    def cancel_queued(self, task_id:str,uid:int)->bool:
        cur=self.conn.execute("UPDATE tasks SET state='canceled',updated_at=? WHERE task_id=? AND user_id=? AND state='queued'",(now(),task_id,uid))
        return bool(cur.rowcount)

    def counts(self) -> dict:
        return {
            'users':self.conn.execute('SELECT COUNT(*) FROM users').fetchone()[0],
            'active':self.conn.execute('SELECT COUNT(*) FROM users WHERE expires_at>? AND blocked=0',(now(),)).fetchone()[0],
            'queued':self.conn.execute("SELECT COUNT(*) FROM tasks WHERE state='queued'").fetchone()[0],
            'running':self.conn.execute("SELECT COUNT(*) FROM tasks WHERE state IN (?,?,?,?)",ACTIVE_STATES).fetchone()[0],
        }

    def next_eligible(self, owner_id:int, *, blocked_accounts:Iterable[str]=()) -> dict | None:
        leased = self._leased_accounts()
        if WORKSPACE_ACTION in leased or self.account_check_pending():
            return None
        unavailable = leased.union(blocked_accounts)
        timestamp = now()
        candidates = self.conn.execute('''SELECT t.* FROM tasks t
          JOIN users u ON u.user_id=t.user_id
          WHERE (t.state='queued' OR
                (t.state='reconcile' AND t.upstream_id IS NULL AND t.error_code NOT IN (?,?)))
          AND t.fire_at <= ? AND t.target_retry_at <= ?
          AND (u.user_id=? OR (u.blocked=0 AND u.expires_at>?))
          ORDER BY CASE t.state WHEN 'reconcile' THEN 0 ELSE 1 END,t.created_at,t.rowid
        ''', (*RECONCILE_HOLD_CODES,timestamp,timestamp,owner_id,timestamp))
        in_flight = self._task_accounts()
        for row in candidates:
            try:
                payload = json.loads(row['launch_payload_json'] or row['payload_json'])
                ids = set(payload_account_ids(payload))
            except (TypeError, ValueError):
                return dict(row)  # Let the worker surface the invalid order.
            if not ids:
                return dict(row)
            busy, unknown_pool = self._task_accounts(exclude_task=row['task_id']) if row['state']=='reconcile' else in_flight
            if not unknown_pool and not ids.intersection(unavailable | busy):
                return dict(row)
        return None
