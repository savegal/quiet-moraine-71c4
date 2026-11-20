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
from .db import Store, UNKNOWN_TASK_POOL, WORKSPACE_ACTION, now

class ExtendedStore(Store):
    def __init__(self,location:str,*,timezone_name:str='Europe/Moscow'):
        super().__init__(location,timezone_name=timezone_name)
        self.conn.executescript('''
        CREATE TABLE IF NOT EXISTS user_templates (
          user_id INTEGER NOT NULL, template_id TEXT NOT NULL, name TEXT NOT NULL,
          mode TEXT NOT NULL, payload TEXT NOT NULL, created_at INTEGER NOT NULL,
          PRIMARY KEY(user_id,template_id));
        CREATE TABLE IF NOT EXISTS template_reviews (
          user_id INTEGER NOT NULL, template_id TEXT NOT NULL, revision TEXT NOT NULL UNIQUE,
          name TEXT NOT NULL, mode TEXT NOT NULL, payload TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('pending','rejected')),
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
        CREATE TABLE IF NOT EXISTS owner_callbacks (
          owner_id INTEGER NOT NULL, route TEXT NOT NULL, callback TEXT NOT NULL,
          PRIMARY KEY(owner_id,route));
        CREATE TABLE IF NOT EXISTS order_mode_access (
          mode TEXT PRIMARY KEY, restricted INTEGER NOT NULL CHECK(restricted IN (0,1)));
        CREATE TABLE IF NOT EXISTS account_imports (
          import_id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
          state TEXT NOT NULL, summary_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS account_import_items (
          import_id TEXT NOT NULL, account_id TEXT NOT NULL, ready INTEGER NOT NULL,
          PRIMARY KEY(import_id,account_id));
        CREATE TABLE IF NOT EXISTS member_import_folders (
          user_id INTEGER PRIMARY KEY, folder_id TEXT NOT NULL UNIQUE);
        ''')
        columns={r[1] for r in self.conn.execute('PRAGMA table_info(users)')}
        for key,definition in {
           'username':"TEXT NOT NULL DEFAULT ''",
           'full_name':"TEXT NOT NULL DEFAULT ''",
           'last_seen':'INTEGER NOT NULL DEFAULT 0',
           'daily_limit':'INTEGER',
           'open_limit':'INTEGER',
           'search_text':"TEXT NOT NULL DEFAULT ''",
           'can_use_photos':'INTEGER NOT NULL DEFAULT 0',
        }.items():
            if key not in columns:self.conn.execute(f'ALTER TABLE users ADD COLUMN {key} {definition}')
        tpl_columns={r[1] for r in self.conn.execute('PRAGMA table_info(user_templates)')}
        if 'is_shared' not in tpl_columns:
            self.conn.execute('ALTER TABLE user_templates ADD COLUMN is_shared INTEGER NOT NULL DEFAULT 0')
        if 'approved_revision' not in tpl_columns:
            with self._write_transaction():
                self.conn.execute('ALTER TABLE user_templates ADD COLUMN approved_revision TEXT')
                self.conn.execute('''INSERT INTO template_reviews
                    SELECT user_id,template_id,lower(hex(randomblob(6))),name,mode,payload,'pending'
                    FROM user_templates''')
        for row in self.conn.execute("SELECT user_id,username,full_name FROM users WHERE search_text='' AND (username!='' OR full_name!='')").fetchall():
            self.conn.execute('UPDATE users SET search_text=? WHERE user_id=?',
                ((row['username']+' '+row['full_name']).casefold(),row['user_id']))

    def set_schedule(self,task_id:str,uid:int,fire_at:int)->bool:
        if fire_at < now()+60 or fire_at > now()+365*86400:raise ValueError('Дата вне допустимого диапазона')
        r=self.conn.execute('UPDATE tasks SET fire_at=? WHERE task_id=? AND user_id=? AND state=?',
                            (fire_at,task_id,uid,'queued'))
        return r.rowcount==1

    def save_owner_route(self,uid:int,route:str,callback:str)->None:
        self.conn.execute('INSERT OR IGNORE INTO owner_callbacks VALUES (?,?,?)',(uid,route,callback))

    def owner_route(self,uid:int,route:str)->str:
        row=self.conn.execute('SELECT callback FROM owner_callbacks WHERE owner_id=? AND route=?',(uid,route)).fetchone()
        return row[0] if row else ''

    def create_account_import(self,import_id:str,owner_id:int,target_id:int,summary:dict)->None:
        self.conn.execute('INSERT INTO account_imports VALUES (?,?,?,?,?)',
                          (import_id,owner_id,target_id,'running',json.dumps(summary,ensure_ascii=False)))

    def save_account_import(self,import_id:str,state:str,summary:dict)->None:
        self.conn.execute('UPDATE account_imports SET state=?,summary_json=? WHERE import_id=?',
                          (state,json.dumps(summary,ensure_ascii=False),import_id))

    def account_import(self,owner_id:int,*,import_id:str|None=None,target_id:int|None=None,admin:bool=False)->dict|None:
        access=() if admin else (owner_id,)
        where='' if admin else ' AND owner_id=?'
        if import_id:
            row=self.conn.execute('SELECT * FROM account_imports WHERE import_id=?'+where,(import_id,*access)).fetchone()
        else:
            row=self.conn.execute('SELECT * FROM account_imports WHERE target_id=?'+where+' ORDER BY rowid DESC LIMIT 1',
                                  (target_id,*access)).fetchone()
        return {**dict(row),'summary':json.loads(row['summary_json'])} if row else None

    def member_import_folder(self,uid:int)->str|None:
        row=self.conn.execute('SELECT folder_id FROM member_import_folders WHERE user_id=?',(uid,)).fetchone()
        return row['folder_id'] if row else None

    def member_import_folder_assignee(self,folder_id:str)->int|None:
        row=self.conn.execute('SELECT user_id FROM member_import_folders WHERE folder_id=?',(folder_id,)).fetchone()
        return row['user_id'] if row else None

    def bind_member_import_folder(self,uid:int,folder_id:str)->None:
        try:
            self.conn.execute('INSERT INTO member_import_folders VALUES (?,?) '
                              'ON CONFLICT(user_id) DO UPDATE SET folder_id=excluded.folder_id',(uid,folder_id))
        except sqlite3.IntegrityError as exc:
            raise ValueError('Эта папка уже привязана к другому траферу.') from exc

    def save_account_import_item(self,import_id:str,account_id:str,ready:bool)->None:
        self.conn.execute('INSERT OR IGNORE INTO account_import_items VALUES (?,?,?)',
                          (import_id,account_id,int(ready)))

    def account_import_items(self,import_id:str)->list[tuple[str,bool]]:
        return [(row[0],bool(row[1])) for row in self.conn.execute(
            'SELECT account_id,ready FROM account_import_items WHERE import_id=? ORDER BY rowid',(import_id,))]

    def assign_available_accounts(self,uid:int,candidates:list[str])->int:
        with self._write_transaction():
            leased = self._leased_accounts()
            if WORKSPACE_ACTION in leased:
                raise ValueError('Дождись завершения операции в кабинете.')
            used, unknown_pool = self._task_accounts(include_queued=True)
            if unknown_pool:
                raise ValueError(UNKNOWN_TASK_POOL)
            busy = leased | used
            self.register(uid)
            already_assigned = {row[0] for row in self.conn.execute('SELECT account_id FROM accounts')}
            assignable = [account for account in dict.fromkeys(candidates)
                          if account not in busy and account not in already_assigned]
            if not assignable:
                return 0
            # Reassignment replaces any stale owner access from an earlier targeted import.
            self.conn.executemany('DELETE FROM imported_owner_access WHERE account_id=?',
                                  ((account,) for account in assignable))
            result=self.conn.executemany('INSERT OR IGNORE INTO accounts(account_id,user_id) VALUES (?,?)',
                                         ((account,uid) for account in assignable))
            return result.rowcount

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

    def save_template(self,uid:int,name:str,mode:str,payload:dict,*,template_id:str|None=None,
                      draft_token:str|None=None)->str:
        if len(name)>64 or not name.strip():raise ValueError('Название шаблона должно содержать от 1 до 64 символов.')
        data=json.dumps(payload,ensure_ascii=False)
        if len(data)>512_000:raise ValueError('Шаблон слишком большой. Сократи список ссылок или текстов.')
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            if draft_token is not None:
                draft=self.draft(uid)
                if (not draft or draft['mode']!='local_template' or draft['step']!='review'
                        or draft['data'].get('token')!=draft_token
                        or draft['data'].get('template_id')!=template_id):
                    raise ValueError('Подтверждение устарело. Открой шаблон заново.')
            revision=secrets.token_hex(6)
            if template_id is not None:
                if not self.template(uid,template_id,owned_only=True):
                    raise ValueError('Шаблон больше не существует.')
            else:
                count=self.conn.execute('SELECT COUNT(*) FROM user_templates WHERE user_id=?',(uid,)).fetchone()[0]
                if count>=50:raise ValueError('Максимум 50 шаблонов')
                template_id=secrets.token_hex(4)
                self.conn.execute('''INSERT INTO user_templates
                    (user_id,template_id,name,mode,payload,created_at) VALUES (?,?,?,?,?,?)''',
                    (uid,template_id,name.strip(),mode,data,now()))
            self.conn.execute('''INSERT INTO template_reviews VALUES (?,?,?,?,?,?,'pending')
                ON CONFLICT(user_id,template_id) DO UPDATE SET revision=excluded.revision,name=excluded.name,
                mode=excluded.mode,payload=excluded.payload,state='pending' ''',
                (uid,template_id,revision,name.strip(),mode,data))
            self.log(uid,'template_submitted',template_id)
            if draft_token is not None:self.clear_draft(uid)
            self.conn.execute('COMMIT')
        except Exception:
            self.conn.execute('ROLLBACK')
            raise
        return template_id

    def template_review(self,uid:int,tid:str)->dict|None:
        row=self.conn.execute('SELECT * FROM template_reviews WHERE user_id=? AND template_id=?',(uid,tid)).fetchone()
        return dict(row) if row else None

    def pending_template_reviews(self,limit:int,offset:int=0)->list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM template_reviews WHERE state='pending' ORDER BY rowid DESC LIMIT ? OFFSET ?",(limit,offset))]

    def pending_template_review(self,revision:str)->dict|None:
        row=self.conn.execute("SELECT * FROM template_reviews WHERE revision=? AND state='pending'",(revision,)).fetchone()
        return dict(row) if row else None

    def decide_template_review(self,actor:int,revision:str,approve:bool)->dict|None:
        if actor!=getattr(self,'owner_id',None):return None
        with self._write_transaction():
            proposed=self.pending_template_review(revision)
            if not proposed:return None
            uid,tid=proposed['user_id'],proposed['template_id']
            if approve:
                self.conn.execute('UPDATE user_templates SET name=?,mode=?,payload=?,approved_revision=? WHERE user_id=? AND template_id=?',
                                  (proposed['name'],proposed['mode'],proposed['payload'],revision,uid,tid))
                self.conn.execute('DELETE FROM template_reviews WHERE revision=?',(revision,))
            else:
                self.conn.execute("UPDATE template_reviews SET state='rejected' WHERE revision=?",(revision,))
            self.log(actor,'template_approved' if approve else 'template_rejected',f'{uid}:{tid}')
            return proposed

    def templates(self,uid:int):
        owner_id=getattr(self,'owner_id',0)
        if owner_id and owner_id!=uid:
            rows=self.conn.execute(
                '''SELECT template_id,name,mode,user_id,is_shared,approved_revision FROM user_templates
                   WHERE user_id=? OR (user_id=? AND is_shared=1 AND approved_revision IS NOT NULL)
                   ORDER BY (user_id=?) DESC, created_at DESC, rowid DESC''',
                (uid,owner_id,uid))
        else:
            rows=self.conn.execute(
                'SELECT template_id,name,mode,user_id,is_shared,approved_revision FROM user_templates WHERE user_id=? ORDER BY created_at DESC,rowid DESC',
                (uid,))
        return [{**dict(r),'owned':r['user_id']==uid,'is_shared':bool(r['is_shared'])} for r in rows]

    def completed_collections(self,uid:int,limit:int,offset:int):
        return [dict(r) for r in self.conn.execute(
            """SELECT task_id,display_name FROM tasks
               WHERE user_id=? AND kind='fetch' AND state='completed' AND upstream_id IS NOT NULL AND hidden=0
               ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?""",(uid,limit,offset))]

    def template(self,uid:int,template_id:str,*,owned_only:bool=False):
        owner_id=getattr(self,'owner_id',0)
        if owned_only or not owner_id or owner_id==uid:
            row=self.conn.execute('SELECT * FROM user_templates WHERE user_id=? AND template_id=?',(uid,template_id)).fetchone()
        else:
            row=self.conn.execute(
                '''SELECT * FROM user_templates WHERE template_id=? AND (user_id=? OR (user_id=? AND is_shared=1 AND approved_revision IS NOT NULL))
                   ORDER BY (user_id=?) DESC LIMIT 1''',
                (template_id,uid,owner_id,uid)).fetchone()
        return {**dict(row),'owned':row['user_id']==uid,'is_shared':bool(row['is_shared'])} if row else None

    def set_template_shared(self,uid:int,template_id:str,shared:bool)->bool:
        r=self.conn.execute('UPDATE user_templates SET is_shared=? WHERE user_id=? AND template_id=?',
                            (int(bool(shared)),uid,template_id))
        return r.rowcount>0

    def delete_template(self,uid:int,template_id:str)->bool:
        with self._write_transaction():
            r=self.conn.execute('DELETE FROM user_templates WHERE user_id=? AND template_id=?',(uid,template_id))
            self.conn.execute('DELETE FROM template_reviews WHERE user_id=? AND template_id=?',(uid,template_id))
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

    def order_mode_access(self) -> dict[str, bool]:
        return {row['mode']: bool(row['restricted']) for row in self.conn.execute(
            'SELECT mode,restricted FROM order_mode_access')}

    def set_order_mode_access(self, actor: int, mode: str, restricted: bool) -> None:
        with self._write_transaction():
            result = self.conn.execute('''
                INSERT INTO order_mode_access(mode,restricted) VALUES (?,?)
                ON CONFLICT(mode) DO UPDATE SET restricted=excluded.restricted
                WHERE order_mode_access.restricted!=excluded.restricted
            ''', (mode, restricted))
            if result.rowcount:
                self.log(actor, 'order_mode_restrict' if restricted else 'order_mode_allow', mode)

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
        })
        return q

    def subscriptions(self,uid:int|None=None,limit:int=15)->list[dict]:
        where='WHERE user_id=?' if uid is not None else ''
        return [dict(r) for r in self.conn.execute('SELECT * FROM subscription_events '+where+' ORDER BY event_id DESC LIMIT ?',
            (uid,min(50,limit)) if uid is not None else (min(50,limit),))]

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

    def can_use_photos(self,uid:int)->bool:
        if uid==getattr(self,'owner_id',0):
            return True
        user=self.user(uid)
        return bool(user and user.get('can_use_photos'))

    def toggle_photo_access(self,uid:int,actor:int)->bool:
        if uid<=0 or uid==getattr(self,'owner_id',0):
            raise ValueError('Нельзя изменить доступ к фото этому пользователю.')
        self.register(uid)
        current=self.can_use_photos(uid)
        new_value=not current
        self.conn.execute('UPDATE users SET can_use_photos=? WHERE user_id=?',(int(new_value),uid))
        self.log(actor,'photo_access_'+('on' if new_value else 'off'),str(uid))
        return new_value

    def queue_task(self,uid:int,name:str,kind:str,payload:dict,**kwargs)->str:
        member=self.user(uid)
        if uid==getattr(self,'owner_id',0):
            # Owner has unlimited access: 0 disables daily/open quotas.
            kwargs['max_daily']=0;kwargs['max_open']=0
        elif member:
            if member['daily_limit'] is not None:kwargs['max_daily']=int(member['daily_limit'])
            if member['open_limit'] is not None:kwargs['max_open']=int(member['open_limit'])
        return super().queue_task(uid,name,kind,payload,**kwargs)

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
