"""Additive local metadata: tenant-safe owned artifacts and timed jobs.

Provider assets/settings are shared across the upstream workspace; local ownership
never grants access to another user's shared provider object.
"""
from __future__ import annotations
import json
import hashlib
import secrets
import sqlite3
import time
from .db import Store, now

class ExtendedStore(Store):
    def __init__(self,location:str):
        super().__init__(location)
        self.conn.executescript('''
        CREATE TABLE IF NOT EXISTS user_templates (
          user_id INTEGER NOT NULL, template_id TEXT NOT NULL, name TEXT NOT NULL,
          mode TEXT NOT NULL, payload TEXT NOT NULL, created_at INTEGER NOT NULL,
          PRIMARY KEY(user_id,template_id));
        CREATE TABLE IF NOT EXISTS external_objects (
          kind TEXT NOT NULL, external_id TEXT NOT NULL, user_id INTEGER NOT NULL,
          created_at INTEGER NOT NULL, PRIMARY KEY(kind,external_id));
        CREATE INDEX IF NOT EXISTS ix_objects_user ON external_objects(user_id,kind);
        CREATE TABLE IF NOT EXISTS user_preferences (
          user_id INTEGER PRIMARY KEY, settings_json TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS audit (
          row_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          action TEXT NOT NULL, target TEXT NOT NULL, at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_audit_at ON audit(at);
        CREATE TABLE IF NOT EXISTS subscription_events (
          event_id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL, actor_id INTEGER NOT NULL,
          action TEXT NOT NULL, days INTEGER NOT NULL DEFAULT 0,
          previous_expires INTEGER NOT NULL, new_expires INTEGER NOT NULL,
          note TEXT NOT NULL DEFAULT '', at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_subs_user ON subscription_events(user_id,at DESC);
        CREATE TABLE IF NOT EXISTS promo_codes (
          digest TEXT PRIMARY KEY, days INTEGER NOT NULL, expires_at INTEGER NOT NULL,
          issuer INTEGER NOT NULL, used_by INTEGER);
        ''')
        columns={r[1] for r in self.conn.execute('PRAGMA table_info(users)')}
        for key,definition in {
           'username':"TEXT NOT NULL DEFAULT ''",
           'full_name':"TEXT NOT NULL DEFAULT ''",
           'last_seen':'INTEGER NOT NULL DEFAULT 0',
           'daily_limit':'INTEGER',
           'open_limit':'INTEGER',
           'search_text':"TEXT NOT NULL DEFAULT ''",
        }.items():
            if key not in columns:self.conn.execute(f'ALTER TABLE users ADD COLUMN {key} {definition}')
        for row in self.conn.execute("SELECT user_id,username,full_name FROM users WHERE search_text='' AND (username!='' OR full_name!='')").fetchall():
            self.conn.execute('UPDATE users SET search_text=? WHERE user_id=?',
                ((row['username']+' '+row['full_name']).casefold(),row['user_id']))

    def set_schedule(self,task_id:str,uid:int,fire_at:int)->bool:
        if fire_at < now()+60 or fire_at > now()+365*86400:raise ValueError('Дата вне допустимого диапазона')
        r=self.conn.execute('UPDATE tasks SET fire_at=? WHERE task_id=? AND user_id=? AND state=?',
                            (fire_at,task_id,uid,'queued'))
        return r.rowcount==1

    def owned(self,kind:str,object_id:str,uid:int)->bool:
        row=self.conn.execute('SELECT user_id FROM external_objects WHERE kind=? AND external_id=?',
                              (kind,object_id)).fetchone()
        return bool(row and row[0]==uid)

    def register_object(self,kind:str,object_id:str,uid:int):
        if not object_id or len(object_id)>200:raise ValueError('Invalid external object id')
        self.conn.execute('INSERT OR IGNORE INTO external_objects VALUES (?,?,?,?)',(kind,object_id,uid,now()))
        if not self.owned(kind,object_id,uid):raise PermissionError('Object already owned')

    def my_objects(self,kind:str,uid:int)->list[str]:
        return [str(x[0]) for x in self.conn.execute(
            'SELECT external_id FROM external_objects WHERE kind=? AND user_id=? ORDER BY created_at DESC LIMIT 100',
            (kind,uid))]

    def forget_object(self,kind:str,object_id:str,uid:int):
        self.conn.execute('DELETE FROM external_objects WHERE kind=? AND external_id=? AND user_id=?',
                          (kind,object_id,uid))

    def save_template(self,uid:int,name:str,mode:str,payload:dict)->str:
        import secrets
        template_id=secrets.token_hex(4)
        if len(name)>64 or not name.strip():raise ValueError('Название шаблона должно содержать от 1 до 64 символов.')
        data=json.dumps(payload,ensure_ascii=False)
        if len(data)>512_000:raise ValueError('Шаблон слишком большой. Сократи список ссылок или текстов.')
        count=self.conn.execute('SELECT COUNT(*) FROM user_templates WHERE user_id=?',(uid,)).fetchone()[0]
        if count>=50:raise ValueError('Максимум 50 шаблонов')
        self.conn.execute('INSERT INTO user_templates VALUES (?,?,?,?,?,?)',
                          (uid,template_id,name.strip(),mode,data,now()))
        return template_id

    def templates(self,uid:int):
        return [dict(r) for r in self.conn.execute(
            'SELECT template_id,name,mode FROM user_templates WHERE user_id=? ORDER BY created_at DESC,rowid DESC',(uid,))]

    def completed_collections(self,uid:int,limit:int,offset:int):
        return [dict(r) for r in self.conn.execute(
            """SELECT task_id,display_name FROM tasks
               WHERE user_id=? AND kind='fetch' AND state='completed' AND upstream_id IS NOT NULL AND hidden=0
               ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?""",(uid,limit,offset))]

    def template(self,uid:int,template_id:str):
        row=self.conn.execute('SELECT * FROM user_templates WHERE user_id=? AND template_id=?',(uid,template_id)).fetchone()
        return dict(row) if row else None

    def delete_template(self,uid:int,template_id:str)->bool:
        r=self.conn.execute('DELETE FROM user_templates WHERE user_id=? AND template_id=?',(uid,template_id))
        return r.rowcount>0

    def preferences(self,uid:int)->dict:
        row=self.conn.execute('SELECT settings_json FROM user_preferences WHERE user_id=?',(uid,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set_preferences(self,uid:int,data:dict):
        self.conn.execute('INSERT INTO user_preferences VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET settings_json=excluded.settings_json',
                          (uid,json.dumps(data,ensure_ascii=False)))

    def log(self,uid:int,action:str,target:str=''):
        self.conn.execute('INSERT INTO audit(user_id,action,target,at) VALUES (?,?,?,?)',
                          (uid,action[:64],target[:128],now()))

    # Owner dashboard: bounded, tenant-aware queries without provider secrets.
    def touch_user(self,uid:int,first:str='',last:str='',username:str='') -> None:
        self.register(uid)
        display=str(username or '')[:64]
        fullname=(' '.join(x for x in (first,last) if x)).strip()[:128]
        self.conn.execute('UPDATE users SET username=?,full_name=?,search_text=?,last_seen=? WHERE user_id=?',
            (display,fullname,(display+' '+fullname).casefold(),now(),uid))

    def members(self,offset:int=0,limit:int=12,status:str='all',search:str='') -> tuple[list[dict],int]:
        conditions=[];params=[]
        if status=='active':conditions.append('blocked=0 AND expires_at>?');params.append(now())
        elif status=='expired':conditions.append('blocked=0 AND expires_at<=?');params.append(now())
        elif status=='blocked':conditions.append('blocked=1')
        if search:
            # Escape LIKE wildcards: a query is text, never executable SQL.
            q='%'+search.casefold()[:70].replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
            conditions.append("(CAST(user_id AS TEXT) LIKE ? ESCAPE '\\' OR search_text LIKE ? ESCAPE '\\' OR search_text LIKE ? ESCAPE '\\')")
            params.extend([q,q,q])
        where=' WHERE '+' AND '.join(conditions) if conditions else ''
        total=self.conn.execute('SELECT COUNT(*) FROM users'+where,params).fetchone()[0]
        rows=self.conn.execute('SELECT * FROM users'+where+' ORDER BY last_seen DESC,first_seen DESC,user_id DESC LIMIT ? OFFSET ?',
                               (*params,min(30,max(1,limit)),max(0,offset))).fetchall()
        return [dict(r) for r in rows],total

    def user_summary(self,uid:int) -> dict|None:
        result=self.user(uid)
        if not result:return None
        grouped=self.conn.execute('SELECT state,COUNT(*) n FROM tasks WHERE user_id=? GROUP BY state',(uid,)).fetchall()
        result['task_states']={r['state']:r['n'] for r in grouped}
        result['tasks_total']=sum(r['n'] for r in grouped)
        result['accounts_total']=self.conn.execute('SELECT COUNT(*) FROM accounts WHERE user_id=?',(uid,)).fetchone()[0]
        result['stars_paid']=self.conn.execute('''SELECT COALESCE(SUM(i.stars),0) FROM invoices i
            JOIN charges c ON c.invoice_payload=i.payload WHERE c.user_id=? AND c.refunded_at=0''',(uid,)).fetchone()[0]
        return result

    def admin_tasks(self,offset:int=0,limit:int=12,uid:int|None=None,status:str='all')->tuple[list[dict],int]:
        conditions=[];params=[]
        if uid is not None:conditions.append('t.user_id=?');params.append(uid)
        if status!='all':conditions.append('t.state=?');params.append(status)
        where=' WHERE '+' AND '.join(conditions) if conditions else ''
        count=self.conn.execute('SELECT COUNT(*) FROM tasks t'+where,params).fetchone()[0]
        rows=self.conn.execute('SELECT t.task_id,t.user_id,t.display_name,t.kind,t.state,t.error_code,t.upstream_id,t.created_at,t.updated_at,t.fire_at FROM tasks t'+where+' ORDER BY t.created_at DESC,t.rowid DESC LIMIT ? OFFSET ?',
            (*params,min(30,max(1,limit)),max(0,offset))).fetchall()
        return [dict(r) for r in rows],count

    def dashboard(self) -> dict:
        t=now()
        q=self.counts()
        q.update({
            'expired_users':self.conn.execute('SELECT COUNT(*) FROM users WHERE expires_at<=? AND blocked=0',(t,)).fetchone()[0],
            'blocked_users':self.conn.execute('SELECT COUNT(*) FROM users WHERE blocked=1').fetchone()[0],
            'done_tasks':self.conn.execute("SELECT COUNT(*) FROM tasks WHERE state='completed'").fetchone()[0],
            'failed_tasks':self.conn.execute("SELECT COUNT(*) FROM tasks WHERE state IN ('failed','unknown')").fetchone()[0],
            'tasks_total':self.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],
            'assigned_accounts':self.conn.execute('SELECT COUNT(*) FROM accounts').fetchone()[0],
            'stars_paid':self.conn.execute('''SELECT COALESCE(SUM(i.stars),0) FROM invoices i
                JOIN charges c ON c.invoice_payload=i.payload WHERE c.refunded_at=0''').fetchone()[0],
            'payments':self.conn.execute('SELECT COUNT(*) FROM charges WHERE refunded_at=0').fetchone()[0],
        })
        return q

    def subscriptions(self,uid:int|None=None,limit:int=15)->list[dict]:
        where='WHERE user_id=?' if uid is not None else ''
        return [dict(r) for r in self.conn.execute('SELECT * FROM subscription_events '+where+' ORDER BY event_id DESC LIMIT ?',
            (uid,min(50,limit)) if uid is not None else (min(50,limit),))]

    def payments(self,limit:int=15)->list[dict]:
        return [dict(r) for r in self.conn.execute('''SELECT i.user_id,i.stars,i.created_at,c.charge_id,c.created_at paid_at,c.refunded_at
            FROM invoices i JOIN charges c ON c.invoice_payload=i.payload
            ORDER BY c.created_at DESC LIMIT ?''',(min(limit,50),))]

    def audit_entries(self,limit:int=20)->list[dict]:
        return [dict(r) for r in self.conn.execute('SELECT * FROM audit ORDER BY row_id DESC LIMIT ?',
            (min(limit,50),))]

    def record_subscription(self,uid:int,actor:int,action:str,days:int,previous:int,expiry:int,note:str='')->int:
        event=self.conn.execute('INSERT INTO subscription_events(user_id,actor_id,action,days,previous_expires,new_expires,note,at) VALUES (?,?,?,?,?,?,?,?)',
            (uid,actor,action,days,previous,expiry,note[:100],now()))
        self.log(actor,'subscription_'+action,str(uid))
        return event.lastrowid

    def modify_access(self,uid:int,actor:int,action:str,days:int=0)->int:
        if uid<=0 or uid==actor:raise ValueError('Нельзя изменять собственный доступ.')
        if action in ('extend','replace') and not 1<=days<=36500:
            raise ValueError('Укажи 1–36500 дней.')
        if action not in ('extend','replace','revoke','block','unblock'):
            raise ValueError('Неизвестное действие.')
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            self.register(uid)
            member=self.user(uid);prev=int(member['expires_at'])
            if action=='extend':expiry=max(prev,now())+days*86400;blocked=int(member['blocked'])
            elif action=='replace':expiry=now()+days*86400;blocked=int(member['blocked'])
            elif action=='revoke':expiry=0;blocked=int(member['blocked'])
            elif action=='block':expiry=prev;blocked=1
            else:expiry=prev;blocked=0
            self.conn.execute('UPDATE users SET expires_at=?,blocked=? WHERE user_id=?',(expiry,blocked,uid))
            self.record_subscription(uid,actor,action,days,prev,expiry)
            self.conn.execute('COMMIT')
            return expiry
        except Exception:
            self.conn.execute('ROLLBACK');raise

    def change_limit(self,uid:int,actor:int,field:str,value:int)->None:
        columns={'daily':'daily_limit','open':'open_limit'}
        if field not in columns or not 0<=value<=100000:raise ValueError('Неверный лимит')
        self.register(uid)
        self.conn.execute('UPDATE users SET '+columns[field]+'=? WHERE user_id=?',(value,uid))
        self.log(actor,'limit_'+field,f'{uid}:{value}')

    def queue_task(self,uid:int,name:str,kind:str,payload:dict,**kwargs)->str:
        member=self.user(uid)
        if uid==getattr(self,'owner_id',0):
            # Owner has unlimited access: 0 disables daily/open quotas.
            kwargs['max_daily']=0;kwargs['max_open']=0
        elif member:
            if member['daily_limit'] is not None:kwargs['max_daily']=int(member['daily_limit'])
            if member['open_limit'] is not None:kwargs['max_open']=int(member['open_limit'])
        return super().queue_task(uid,name,kind,payload,**kwargs)

    def record_payment_access(self,uid:int,stars:int,days:int,previous:int,expiry:int)->int:
        return self.record_subscription(uid,0,'stars',days,previous,expiry,f'{stars} XTR')

    def payment_superseded(self,uid:int,paid_at:int)->bool:
        return self.conn.execute("""SELECT 1 FROM subscription_events
            WHERE user_id=? AND action IN ('replace','revoke') AND at>? LIMIT 1""",(uid,paid_at)).fetchone() is not None

    def record_refund_access(self,uid:int,event_id:int,stars:int)->bool:
        if event_id==-1:
            expiry=self.user(uid)['expires_at']
            self.record_subscription(uid,0,'refund',0,expiry,expiry,f'{stars} XTR')
            return True
        payment=self.conn.execute("SELECT 1 FROM subscription_events WHERE user_id=? AND event_id=? AND action='stars'",
                                  (uid,event_id)).fetchone()
        if not payment:return False
        previous=self.user(uid)['expires_at']
        reset=self.conn.execute('''SELECT event_id,new_expires FROM subscription_events
            WHERE user_id=? AND action IN ('replace','revoke') ORDER BY event_id DESC LIMIT 1''',(uid,)).fetchone()
        if reset and reset['event_id']>event_id:
            # A later manual replacement starts its own access period.
            self.record_subscription(uid,0,'refund',0,previous,previous,f'{stars} XTR')
            return True
        rows=self.conn.execute('''SELECT e.*,c.refunded_at,c.created_at AS paid_at FROM subscription_events e
            LEFT JOIN charges c ON c.grant_event_id=e.event_id
            WHERE e.user_id=? AND e.event_id>? ORDER BY e.event_id''',(uid,reset['event_id'] if reset else 0)).fetchall()
        expiry=reset['new_expires'] if reset else rows[0]['previous_expires']
        for row in rows:
            if row['action'] in ('extend','promo','stars') and not row['refunded_at']:
                expiry=max(expiry,row['paid_at'] or row['at'])+row['days']*86400
        expiry=min(previous,expiry)
        self.conn.execute('UPDATE users SET expires_at=? WHERE user_id=?',(expiry,uid))
        self.record_subscription(uid,0,'refund',0,previous,expiry,f'{stars} XTR')
        return True

    def issue_promo(self, actor:int, days:int) -> str:
        if not 1 <= days <= 36500:
            raise ValueError('Укажи срок подписки от 1 до 36500 дней.')
        code = secrets.token_hex(8).upper()
        digest = hashlib.sha256(code.encode('ascii')).hexdigest()
        self.conn.execute('INSERT INTO promo_codes(digest,days,expires_at,issuer) VALUES (?,?,?,?)',
                          (digest,days,now()+7*86400,actor))
        self.log(actor,'promo_created',str(days))
        return code

    def redeem_promo(self, uid:int, code:str) -> int | None:
        digest = hashlib.sha256(code.strip().upper().encode('utf-8')).hexdigest()
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            self.register(uid)
            user = self.user(uid)
            row = self.conn.execute('SELECT * FROM promo_codes WHERE digest=? AND expires_at>? AND used_by IS NULL',
                                    (digest,now())).fetchone()
            if not row or user['blocked'] or uid==row['issuer']:
                self.conn.execute('ROLLBACK')
                return None
            previous = user['expires_at']
            expiry = max(now(),previous)+row['days']*86400
            self.conn.execute('UPDATE promo_codes SET used_by=? WHERE digest=?',(uid,digest))
            self.conn.execute('UPDATE users SET expires_at=? WHERE user_id=?',(expiry,uid))
            self.record_subscription(uid,row['issuer'],'promo',row['days'],previous,expiry)
            self.conn.execute('COMMIT')
            return expiry
        except Exception:
            self.conn.execute('ROLLBACK')
            raise
