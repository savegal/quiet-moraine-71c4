from __future__ import annotations
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

OPEN_STATES = ('queued','reconcile','starting','running','paused','stopping')
ACTIVE_STATES = ('starting','running','paused','stopping')
TERMINAL_STATES = ('completed','stopped','failed','canceled')
RECONCILE_HOLD_CODES = ('reconcile_review','reconcile_access_revoked')
MAX_USER_ID = 2**63 - 1


def now() -> int:
    return int(time.time())


def remote_task_name(request_id: str, display_name: str) -> str:
    """A unique remote label while retaining the full local display name."""
    marker = '[PX-' + request_id.replace('-', '') + ']'
    return f'{marker} {display_name}'[:60].rstrip()


class Store:
    """Single-worker WAL database. Atomic changes; one bot process only."""
    def __init__(self, location: str):
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
        CREATE TABLE IF NOT EXISTS drafts (
            user_id INTEGER PRIMARY KEY, mode TEXT NOT NULL, step TEXT NOT NULL,
            data_json TEXT NOT NULL, updated_at INTEGER NOT NULL
        );
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
        CREATE TABLE IF NOT EXISTS invoices (
            payload TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
            stars INTEGER NOT NULL, created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL, paid INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS charges (
            charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
            invoice_payload TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payment_reviews (
            kind TEXT NOT NULL, charge_id TEXT NOT NULL, user_id INTEGER NOT NULL,
            invoice_payload TEXT NOT NULL, stars INTEGER NOT NULL, created_at INTEGER NOT NULL,
            PRIMARY KEY(kind,charge_id)
        );
        ''')
        fields = {row[1] for row in self.conn.execute('PRAGMA table_info(tasks)')}
        if 'fire_at' not in fields:
            self.conn.execute('ALTER TABLE tasks ADD COLUMN fire_at INTEGER NOT NULL DEFAULT 0')
        if 'hidden' not in fields:
            self.conn.execute('ALTER TABLE tasks ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0')
        if 'reconcile_attempts' not in fields:
            self.conn.execute('ALTER TABLE tasks ADD COLUMN reconcile_attempts INTEGER NOT NULL DEFAULT 0')
        fields = {row[1] for row in self.conn.execute('PRAGMA table_info(invoices)')}
        if 'days' not in fields:
            self.conn.execute('ALTER TABLE invoices ADD COLUMN days INTEGER NOT NULL DEFAULT 0')
        if 'checkout_id' not in fields:
            self.conn.execute("ALTER TABLE invoices ADD COLUMN checkout_id TEXT NOT NULL DEFAULT ''")
        fields = {row[1] for row in self.conn.execute('PRAGMA table_info(charges)')}
        if 'refunded_at' not in fields:
            self.conn.execute('ALTER TABLE charges ADD COLUMN refunded_at INTEGER NOT NULL DEFAULT 0')
        if 'grant_event_id' not in fields:
            self.conn.execute('ALTER TABLE charges ADD COLUMN grant_event_id INTEGER NOT NULL DEFAULT 0')
        self.conn.execute('CREATE INDEX IF NOT EXISTS charges_grant_event ON charges(grant_event_id)')

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

    def assign(self, uid: int, account_id: str) -> None:
        self.register(uid)
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            row=self.conn.execute('SELECT user_id FROM accounts WHERE account_id=?',(account_id,)).fetchone()
            if row and row['user_id'] != uid:
                jobs=self.conn.execute("SELECT payload_json FROM tasks WHERE user_id=? AND state IN (?,?,?,?,?,?)",(row['user_id'],*OPEN_STATES)).fetchall()
                if any(account_id in json.loads(t['payload_json']).get('assets',{}).get('accountIds',[]) for t in jobs):
                    raise ValueError('Account participates in a pending or active task')
            self.conn.execute('INSERT INTO accounts(account_id,user_id) VALUES (?,?) ON CONFLICT(account_id) DO UPDATE SET user_id=excluded.user_id',(account_id,uid))
            self.conn.execute('COMMIT')
        except Exception:
            self.conn.execute('ROLLBACK');raise

    def unassign(self, uid: int, account_id: str) -> bool:
        jobs=self.conn.execute("SELECT payload_json FROM tasks WHERE user_id=? AND state IN (?,?,?,?,?,?)",(uid,*OPEN_STATES)).fetchall()
        if any(account_id in json.loads(t['payload_json']).get('assets',{}).get('accountIds',[]) for t in jobs):
            raise ValueError('Account participates in a pending or active task')
        cur=self.conn.execute('DELETE FROM accounts WHERE account_id=? AND user_id=?',(account_id,uid))
        return bool(cur.rowcount)

    def account_ids(self, uid: int) -> list[str]:
        return [r['account_id'] for r in self.conn.execute('SELECT account_id FROM accounts WHERE user_id=? ORDER BY account_id',(uid,))]

    def save_draft(self, uid:int, mode:str, step:str, data:dict) -> None:
        self.conn.execute('INSERT OR REPLACE INTO drafts VALUES (?,?,?,?,?)',(uid,mode,step,json.dumps(data,ensure_ascii=False),now()))

    def draft(self, uid:int) -> dict | None:
        r=self.conn.execute('SELECT * FROM drafts WHERE user_id=?',(uid,)).fetchone()
        return {**dict(r),'data':json.loads(r['data_json'])} if r else None

    def clear_draft(self, uid:int) -> None:
        self.conn.execute('DELETE FROM drafts WHERE user_id=?',(uid,))

    def queue_task(self, uid:int, name:str, kind:str, payload:dict, *, followup_source:str|None=None, followup_mode:str|None=None, max_daily:int=20, max_open:int=3, fire_at:int=0, draft_token:str|None=None) -> str:
        timestamp = now()
        if fire_at and not timestamp + 60 <= fire_at <= timestamp + 365 * 86400:
            raise ValueError('Укажи время запуска от 1 минуты до 365 дней вперёд.')
        task_id=uuid.uuid4().hex[:8]
        request_id=str(uuid.uuid4())
        payload={**payload,'name':remote_task_name(request_id,name),'kind':kind,'requestId':request_id}
        # BEGIN IMMEDIATE ensures account ownership and queue insert are consistent.
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            if draft_token is not None:
                draft=self.draft(uid)
                if not draft or draft['step']!='review' or draft['data'].get('wizard_token')!=draft_token:
                    raise ValueError('Подтверждение устарело. Открой актуальный черновик.')
            if max_daily>0:
                day_start=(now()//86400)*86400
                daily=self.conn.execute('SELECT COUNT(*) FROM tasks WHERE user_id=? AND created_at>=?',
                    (uid,day_start)).fetchone()[0]
                if daily>=max_daily:raise ValueError('Достигнут дневной лимит заданий.')
            if max_open>0:
                opened=self.conn.execute("SELECT COUNT(*) FROM tasks WHERE user_id=? AND state IN (?,?,?,?,?,?)",(uid,*OPEN_STATES)).fetchone()[0]
                if opened>=max_open:raise ValueError('Превышено количество одновременно ожидающих или идущих заданий.')
            ids=payload.get('assets',{}).get('accountIds',[])
            if not ids or set(ids)-set(self.account_ids(uid)):
                raise ValueError('Назначенные аккаунты изменились. Проверь заказ и подтверди его снова.')
            self.conn.execute('INSERT INTO tasks(task_id,user_id,display_name,kind,request_id,payload_json,upstream_id,state,created_at,updated_at,fire_at) VALUES (?,?,?,?,?,?,NULL,?,?,?,?)',
                (task_id,uid,name,kind,request_id,json.dumps(payload,ensure_ascii=False),'queued',timestamp,timestamp,fire_at))
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

    def unresolved_tasks(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM tasks WHERE state='reconcile' ORDER BY updated_at, rowid"
        )]

    def claim_start(self, task_id: str, expected_state: str, owner_id: int) -> bool:
        if expected_state not in ('queued','reconcile'):
            return False
        timestamp = now()
        cursor = self.conn.execute('''
            UPDATE tasks
               SET state='starting', updated_at=?,
                   reconcile_attempts=reconcile_attempts+CASE WHEN state='reconcile' THEN 1 ELSE 0 END
             WHERE task_id=? AND state=? AND fire_at<=? AND upstream_id IS NULL
               AND error_code NOT IN (?,?)
               AND (user_id=? OR user_id IN (SELECT user_id FROM users WHERE blocked=0 AND expires_at>?))
        ''',(timestamp,task_id,expected_state,timestamp,*RECONCILE_HOLD_CODES,owner_id,timestamp))
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

    def transition(self, task_id:str, state:str, *, upstream_id:str|None=None, error_code:str|None=None, snapshot:dict|None=None, expected_state:str|None=None) -> bool:
        sets=['state=?','updated_at=?']; args:list[Any]=[state,now()]
        if upstream_id is not None:
            sets.append('upstream_id=?');args.append(upstream_id)
        if error_code is not None:
            sets.append('error_code=?');args.append(error_code)
        if snapshot is not None:
            sets.append('snapshot_json=?');args.append(json.dumps(snapshot,ensure_ascii=False))
        args.append(task_id)
        where = ' WHERE task_id=?'
        if expected_state is not None:
            where += ' AND state=?'
            args.append(expected_state)
        return self.conn.execute('UPDATE tasks SET '+','.join(sets)+where,args).rowcount == 1

    def recover(self) -> None:
        # Preserve request IDs across restarts for idempotent Tokora POST /tasks retries.
        self.conn.execute("UPDATE tasks SET state='reconcile' WHERE state='starting' AND upstream_id IS NULL")
        # Older versions stopped tracking a task after a single not-found response.
        self.conn.execute("UPDATE tasks SET state='reconcile' WHERE state='unknown' AND upstream_id IS NOT NULL")

    def cancel_queued(self, task_id:str,uid:int)->bool:
        cur=self.conn.execute("UPDATE tasks SET state='canceled',updated_at=? WHERE task_id=? AND user_id=? AND state='queued'",(now(),task_id,uid))
        return bool(cur.rowcount)

    def create_invoice(self, uid:int,stars:int,days:int=0) -> str:
        self.register(uid)
        payload='px:'+uuid.uuid4().hex
        self.conn.execute('INSERT INTO invoices(payload,user_id,stars,created_at,expires_at,days) VALUES (?,?,?,?,?,?)',
                          (payload,uid,stars,now(),now()+1800,days))
        return payload

    def invoice_valid(self, uid:int, payload:str, stars:int) -> bool:
        r=self.conn.execute('''SELECT i.paid,i.expires_at,u.blocked FROM invoices i
            JOIN users u ON i.user_id=u.user_id WHERE i.payload=? AND i.user_id=? AND i.stars=?''',
            (payload,uid,stars)).fetchone()
        return bool(r and not r['paid'] and not r['blocked'] and r['expires_at']>=now())

    def approve_invoice(self,uid:int,payload:str,stars:int,checkout_id:str)->bool:
        if not checkout_id:return False
        result=self.conn.execute('''UPDATE invoices SET checkout_id=?
            WHERE payload=? AND user_id=? AND stars=? AND paid=0
            AND (checkout_id=? OR (checkout_id='' AND expires_at>=?))
            AND EXISTS (SELECT 1 FROM users WHERE user_id=? AND blocked=0)''',
            (checkout_id,payload,uid,stars,checkout_id,now(),uid))
        return result.rowcount==1

    def charge_recorded(self, uid:int, payload:str, charge_id:str) -> bool:
        return bool(self.conn.execute('SELECT 1 FROM charges WHERE user_id=? AND invoice_payload=? AND charge_id=?',
                                      (uid,payload,charge_id)).fetchone())

    def paid_invoice(self, uid:int, payload:str,stars:int,charge_id:str,days:int,*,paid_at:int|None=None) -> int|None:
        timestamp=min(now(),paid_at) if type(paid_at) is int and paid_at>0 else now()
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            # An already charged invoice can arrive after a delay or an owner block.
            # The expiration and access check belong to pre-checkout.
            invoice = self.conn.execute('SELECT days FROM invoices WHERE payload=? AND user_id=? AND stars=? AND paid=0',
                                        (payload,uid,stars)).fetchone()
            if not invoice or not charge_id:
                self.conn.execute('ROLLBACK');return None
            # Unique charge ID prevents extension on replayed successful_payment updates.
            self.conn.execute('INSERT OR IGNORE INTO charges(charge_id,user_id,invoice_payload,created_at) VALUES (?,?,?,?)',
                              (charge_id,uid,payload,timestamp))
            if not self.conn.execute('SELECT changes()').fetchone()[0]:
                self.conn.execute('ROLLBACK');return None
            self.conn.execute('UPDATE invoices SET paid=1 WHERE payload=?',(payload,))
            previous = self.user(uid)['expires_at']
            refund=self.conn.execute("""SELECT created_at FROM payment_reviews WHERE kind='refund'
                AND charge_id=? AND user_id=? AND invoice_payload=? AND stars=?""",
                (charge_id,uid,payload,stars)).fetchone()
            if refund or self.payment_superseded(uid,timestamp):
                self.conn.execute('UPDATE charges SET refunded_at=?,grant_event_id=-1 WHERE charge_id=?',
                                  (refund['created_at'] if refund else 0,charge_id))
                if refund:self.conn.execute("DELETE FROM payment_reviews WHERE kind='refund' AND charge_id=?",(charge_id,))
                self.conn.execute('COMMIT')
                return previous
            days = invoice['days'] or days
            expiry=max(timestamp,previous)+days*86400
            self.conn.execute('UPDATE users SET expires_at=? WHERE user_id=?',(expiry,uid))
            event_id=self.record_payment_access(uid,stars,days,previous,expiry)
            if event_id:
                self.conn.execute('UPDATE charges SET grant_event_id=? WHERE charge_id=?',(event_id,charge_id))
            self.conn.execute('COMMIT')
            return expiry
        except Exception:
            self.conn.execute('ROLLBACK');raise

    def record_payment_access(self, uid:int, stars:int, days:int, previous:int, expiry:int) -> int:
        """Optional payment ledger hook, called in the same transaction as the credit."""
        return 0

    def payment_superseded(self,uid:int,paid_at:int)->bool:
        return False

    def charge_access_state(self,charge_id:str)->str:
        charge=self.conn.execute('SELECT refunded_at,grant_event_id FROM charges WHERE charge_id=?',(charge_id,)).fetchone()
        if charge and charge['refunded_at']:return 'refunded'
        if charge and charge['grant_event_id']==-1:return 'superseded'
        return 'credited'

    def record_payment_review(self,kind:str,uid:int,payload:str,stars:int,charge_id:str)->bool:
        return self.conn.execute('INSERT OR IGNORE INTO payment_reviews VALUES (?,?,?,?,?,?)',
            (kind,charge_id,uid,payload,stars,now())).rowcount==1

    def refund_invoice(self,uid:int,payload:str,stars:int,charge_id:str)->str:
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            charge=self.conn.execute('''SELECT c.refunded_at,c.grant_event_id FROM charges c
                JOIN invoices i ON i.payload=c.invoice_payload
                WHERE c.charge_id=? AND c.user_id=? AND i.payload=? AND i.stars=?''',
                (charge_id,uid,payload,stars)).fetchone()
            if not charge:
                outcome='unmatched' if self.record_payment_review('refund',uid,payload,stars,charge_id) else 'duplicate'
            elif charge['refunded_at']:
                outcome='duplicate'
            else:
                self.conn.execute('UPDATE charges SET refunded_at=? WHERE charge_id=?',(now(),charge_id))
                adjusted=self.record_refund_access(uid,charge['grant_event_id'],stars)
                outcome='recorded' if adjusted else 'review'
                if not adjusted:self.record_payment_review('refund_access',uid,payload,stars,charge_id)
            self.conn.execute('COMMIT')
            return outcome
        except Exception:
            self.conn.execute('ROLLBACK')
            raise

    def record_refund_access(self,uid:int,event_id:int,stars:int)->bool:
        return False

    def counts(self) -> dict:
        return {
            'users':self.conn.execute('SELECT COUNT(*) FROM users').fetchone()[0],
            'active':self.conn.execute('SELECT COUNT(*) FROM users WHERE expires_at>? AND blocked=0',(now(),)).fetchone()[0],
            'queued':self.conn.execute("SELECT COUNT(*) FROM tasks WHERE state='queued'").fetchone()[0],
            'running':self.conn.execute("SELECT COUNT(*) FROM tasks WHERE state IN (?,?,?,?)",ACTIVE_STATES).fetchone()[0],
        }

    def next_eligible(self, owner_id:int) -> dict | None:
        row=self.conn.execute('''SELECT t.* FROM tasks t
          JOIN users u ON u.user_id=t.user_id
          WHERE (t.state='queued' OR
                (t.state='reconcile' AND t.upstream_id IS NULL AND t.error_code NOT IN (?,?)))
          AND t.fire_at <= ?
          AND (u.user_id=? OR (u.blocked=0 AND u.expires_at>?))
          ORDER BY CASE t.state WHEN 'reconcile' THEN 0 ELSE 1 END,t.created_at,t.rowid
          LIMIT 1''',(*RECONCILE_HOLD_CODES,now(),owner_id,now())).fetchone()
        return dict(row) if row else None
