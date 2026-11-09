"""Owner-only Telegram operations console.

UI callback payloads contain IDs/actions, never credentials or arbitrary API paths.
All mutations require a fresh confirmation tied to the owner's own SQLite draft.
"""
from __future__ import annotations

import csv
import io
import json
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from .catalog import MODES
from .db import ACTIVE_STATES, MAX_USER_ID, TERMINAL_STATES, now
from .presentation import days_label, keyboard, number, pagination, state_label, state_symbol, task_view, task_rich, report_view, table_html, h
from .api import ApiError
from .bot import safe_api_error
from .worker import marked_task_name, matches_reconciliation, snapshot_state

UID=re.compile(r'^[1-9][0-9]{0,18}$')
TASK=re.compile(r'^[0-9a-f]{8}$')
ACC=re.compile(r'^[a-zA-Z0-9_-]{1,128}$')


def valid_member_id(value:str)->bool:
    return bool(UID.fullmatch(value)) and int(value)<=MAX_USER_ID

ACCESS_ACTIONS={
    'extend':('＋ Продлить подписку','success'),
    'replace':('◷ Установить срок','success'),
    'revoke':('× Отозвать подписку','danger'),
    'block':('■ Заблокировать доступ','danger'),
    'unblock':('✓ Разблокировать доступ','success'),
    'assign':('◇ Назначить аккаунт','success'),
    'unassign':('− Отвязать аккаунт','danger'),
    'limit_daily':('✓ Сохранить лимит в сутки','success'),
    'limit_open':('✓ Сохранить лимит очереди','success'),
}


def members_status(u:dict,owner_id:int)->str:
    if u['user_id']==owner_id:return 'Главный администратор'
    if u['blocked']:return 'Заблокирован'
    if u['expires_at']>now():return 'Активен'
    return 'Без подписки'


def _label(u:dict)->str:
    return ('@'+u['username'] if u.get('username') else u.get('full_name') or str(u['user_id']))[:35]


def _dt(ts:int)->str:
    return datetime.fromtimestamp(ts,timezone.utc).strftime('%d.%m.%Y %H:%M UTC') if ts else '—'


def csv_safe(value):
    """Protect spreadsheet users against formula injection in exported text fields."""
    if isinstance(value,str) and value.lstrip('\t\r\n ').startswith(('=','+','-','@')):
        return "'"+value
    return value


class SafeCsvWriter:
    def __init__(self,stream):self.writer=csv.writer(stream)
    def writerow(self,row):self.writer.writerow([csv_safe(value) for value in row])


class AdminPanelMixin:
    async def admin_dashboard(self,uid:int):
        if not self.owner(uid):return
        d=self.db.dashboard()
        owner=self.db.user(uid)
        owner_status=members_status(owner,0) if owner else ''
        active=d['active']-int(owner_status=='Активен')
        expired=d['expired_users']-int(owner_status=='Без подписки')
        rows=[('Траферов',number(d['users']-bool(owner))),
              ('С подпиской / без',f'{number(active)} / {number(expired)}'),
              ('Заблокированы',number(d['blocked_users']-int(owner_status=='Заблокирован'))),
              ('Заказов',number(d['tasks_total'])),
              ('В очереди / в работе',f'{number(d["queued"])} / {number(d["running"])}'),
              ('Готово / ошибки',f'{number(d["done_tasks"])} / {number(d["failed_tasks"])}'),
              ('Назначено аккаунтов',number(d['assigned_accounts'])),
              ('Оплаты',f'{number(d["stars_paid"])} XTR · {number(d["payments"])} счетов')]
        rich,plain=table_html('▣ Центр управления',rows)
        await self.tg.rich(uid,rich,plain,keyboard(
            [('◇ Траферы','adm:users:all:0','primary'),('▣ Заказы','adm:jobs:all:0',None)],
            [('＋ Выдать подписку','adm:pick:grant','success')],
            [('⌕ Поиск трафера','adm:search',None),('◈ Промокод','promo_create',None)],
            [('▤ Оплаты','adm:payments',None),('◷ Подписки','adm:subs',None)],
            [('▧ Журнал','adm:audit',None),('⇩ Экспорт CSV','adm:exports',None)],
            [('⚙︎ Кабинет Tokora','adm:api',None),('✓ Проверить API','admin_health',None)],
            [('↶ Обзор','home',None)]))

    async def admin_users(self,uid:int,status:str='all',page:int=0,search:str=''):
        if not self.owner(uid):return
        if status not in ('all','active','expired','blocked') or page<0 or page>100000:return
        if search=='__saved__':search=self.db.preferences(uid).get('admin_search','')
        users,total=self.db.members(page*10,10,status,search)
        first=page*10+1 if total else 0
        out=f'<b>◇ Траферы</b> · {first}–{min(total,(page+1)*10)} / {total}\n'
        if search:out+='⌕ Поиск: <code>'+h(search[:70])+'</code>\n'
        out+='\n'
        actions=[]
        for user in users:
            name=_label(user)
            status_label=members_status(user,self.cfg.owner_id)
            marker={'Активен':'✓','Заблокирован':'■','Главный администратор':'▣'}.get(status_label,'○')
            out+=f'{marker} <code>{user["user_id"]}</code> · {h(name)}\n'
            actions.append([(f'{marker} {name[:25]} · {status_label}','adm:u:'+str(user['user_id']),None)])
        if not users:out+='Ничего не найдено.\n'
        if not search:
            filters=[('▤ Все','all'),('✓ Активные','active'),('○ Без подписки','expired'),('■ Блок','blocked')]
            tabs=[(label,f'adm:users:{key}:0','primary' if status==key else None) for label,key in filters]
            actions[0:0]=[tabs[:2],tabs[2:]]
        mode='adm:result:' if search else f'adm:users:{status}:'
        actions.append(pagination(mode,page,(page+1)*10<total))
        actions.extend([[('⌕ Поиск','adm:search',None),('＋ Новый доступ','adm:pick:grant','success')],
                        [('↶ Панель владельца','admin',None)]])
        await self.say(uid,out[:3980],keyboard(*actions))

    async def admin_user(self,uid:int,target:int):
        if not self.owner(uid):return
        u=self.db.user_summary(target)
        if not u:return await self.say(uid,'Трафер не найден. Можешь выдать доступ по Telegram ID.')
        active=u['task_states']
        name=u.get('full_name')
        handle='@'+u['username'] if u.get('username') else ''
        identity=' · '.join(value for value in (name,handle) if value) or '—'
        values=[
            ('Трафер',identity),('Telegram ID',target),('Статус',members_status(u,self.cfg.owner_id)),
            ('Подписка до',self.format_date(u['expires_at'])),
            ('Аккаунты / заказы',f'{number(u["accounts_total"])} / {number(u["tasks_total"])}'),
            ('В очереди / в работе',f'{number(active.get("queued",0))} / {number(sum(active.get(k,0) for k in ACTIVE_STATES))}'),
            ('Оплачено',f'{number(u["stars_paid"])} XTR'),
            ('Лимиты в сутки / открытых',f'{u["daily_limit"] if u["daily_limit"] is not None else "общий"} / {u["open_limit"] if u["open_limit"] is not None else "общий"}'),
            ('Последняя активность',self.format_date(u.get('last_seen',0))),
        ]
        rich,plain=table_html('◇ Карточка трафера',values,'Кнопки с ＋ продлевают подписку. «Установить срок» отсчитывает дни от сегодня.')
        tid=str(target)
        rows=[
          [('＋ 1 день','adm:edit:extend:'+tid+':1','success'),('＋ 2 дня','adm:edit:extend:'+tid+':2','success')],
          [('＋ 7 дней','adm:edit:extend:'+tid+':7','success'),('＋ 30 дней','adm:edit:extend:'+tid+':30','success')],
          [('＋ Свой срок','adm:edit:extend:'+tid+':custom','primary'),('◷ Установить срок','adm:edit:replace:'+tid+':custom',None)],
          [('▣ Заказы','adm:utasks:'+tid+':0',None),('◇ Аккаунты','adm:uaccounts:'+tid,None)],
          [('＋ Назначить аккаунт','adm:edit:assign:'+tid+':custom',None),('− Отвязать','adm:edit:unassign:'+tid+':custom','danger')],
          [('▤ Лимит в сутки','adm:edit:limit_daily:'+tid+':custom',None),('▤ Лимит очереди','adm:edit:limit_open:'+tid+':custom',None)],
          [('◷ Подписки','adm:usubs:'+tid,None),('× Отозвать срок','adm:edit:revoke:'+tid,'danger')],
          [(( '✓ Разблокировать' if u['blocked'] else '■ Заблокировать'),
              'adm:edit:'+('unblock' if u['blocked'] else 'block')+':'+tid,'success' if u['blocked'] else 'danger')],
          [('↶ Траферы','adm:users:all:0',None),('▣ Панель','admin',None)]]
        await self.tg.rich(uid,rich,plain,keyboard(*rows))

    async def admin_user_accounts(self,uid:int,target:int):
        if not self.owner(uid):return
        ids=self.db.account_ids(target)
        # Account IDs can be 128 characters: never embed them in 64-byte callbacks.
        rows=[[('＋ Добавить','adm:edit:assign:'+str(target)+':custom','success'),
               ('− Отвязать по ID','adm:edit:unassign:'+str(target)+':custom',None)],
              [('↶ Трафер','adm:u:'+str(target),None)]]
        await self.say(uid,'<b>◇ Аккаунты трафера</b> · '+str(target)+'\n\n'+
            ('\n'.join('├ <code>'+h(x)+'</code>' for x in ids[:40]) if ids else 'Нет назначенных аккаунтов.')+
            '\n\nДля отвязки выбери действие ниже и введи ID.',keyboard(*rows))

    async def admin_jobs(self,uid:int,page:int=0,state:str='all',target:int|None=None):
        if not self.owner(uid):return
        if page<0 or page>100000 or state not in ('all','queued','running','paused','reconcile','completed','stopped','failed','canceled'):return
        jobs,total=self.db.admin_tasks(page*10,10,target,state)
        title=f'<b>▣ Заказы {"трафера "+str(target) if target is not None else "команды"}</b>'
        text=title+f' · {page*10+1 if total else 0}–{min(total,(page+1)*10)} / {total}\n\n'
        rows=[]
        for j in jobs:
            text+=f'├ <code>{j["task_id"]}</code> · {h(j["display_name"][:37])}\n   {h(state_label(j["state"]))} · ID {j["user_id"]}\n'
            rows.append([(f'{state_symbol(j["state"])} {j["task_id"]} · {state_label(j["state"])}','adm:job:'+j['task_id'],None)])
        if not jobs:text+='Пока нет заказов.'
        if target is None:
            filters=[('▤ Все','all'),('◷ Очередь','queued'),('▷ В работе','running'),('Ⅱ Пауза','paused'),
                     ('↻ Сверка','reconcile'),('! Ошибки','failed'),('✓ Готово','completed'),
                     ('■ Остановлены','stopped'),('× Отменены','canceled')]
            tabs=[(label,f'adm:jobs:{key}:0','primary' if state==key else None) for label,key in filters]
            rows[0:0]=[tabs[:3],tabs[3:6],tabs[6:]]
        prefix=f'adm:utasks:{target}:' if target is not None else f'adm:jobs:{state}:'
        rows.append(pagination(prefix,page,(page+1)*10<total))
        rows.append([('↶ К траферу','adm:u:'+str(target),None)] if target is not None else [('↶ Панель владельца','admin',None)])
        await self.say(uid,text[:4000],keyboard(*rows))

    async def admin_job(self,uid:int,job_id:str):
        if not self.owner(uid):return
        t=self.db.task(job_id)
        if not t:return await self.say(uid,'Заказ не найден.')
        await self.live_cards.open(uid,t,self.render_admin_job(t),admin=True)

    @staticmethod
    def can_verify_order(task:dict)->bool:
        return (bool(task['upstream_id']) and task['kind']=='publish'
                and task['state'] in ('running','paused','completed','stopped'))

    async def admin_refresh_job(self,uid:int,tid:str)->None:
        if not self.owner(uid):return
        job=self.db.task(tid)
        if job and job['upstream_id'] and job['state'] in TERMINAL_STATES:
            self.save_verified_snapshot(job,await self.api.task(job['upstream_id']))
        await self.admin_job(uid,tid)

    def render_admin_job(self,t:dict)->tuple[str,str,dict]:
        job_id=t['task_id']
        kind=MODES.get(t['kind'],('Импорт аккаунтов' if t['kind']=='import' else t['kind'],))[0]
        text=(task_view(t)+'\n\n<b>◇ Сведения о заказе</b>\n'
              f'├ Трафер: <code>{t["user_id"]}</code>\n'
              f'├ Тип: {h(kind)}\n'
              f'├ Создан: {h(self.format_date(t["created_at"]))}\n'
              f'└ Внешний ID: <code>{h(t["upstream_id"] or "—")}</code>')
        rows=[]
        if t['upstream_id']:
            rows.append([('▥ Отчёт','adm:report:'+job_id,'primary')])
        if t['state']=='queued':
            rows.append([('× Отменить очередь','adm:jobedit:cancel:'+job_id,'danger')])
        if t['state']=='reconcile':
            rows.append([('↻ Сверка с Tokora','adm:reconcile:'+job_id,'primary')])
        if t['state'] in ('running','paused') and t['upstream_id']:
            rows.append([('■ Остановить','adm:jobedit:stop:'+job_id,'danger')])
        if self.can_verify_order(t):
            rows.append([('✓ Проверить статусы','adm:jobedit:verify:'+job_id,None)])
        if t['upstream_id']:
            rows.append([('↻ Обновить','adm:refresh:'+job_id,None)])
        rows.append([('◇ Трафер','adm:u:'+str(t['user_id']),None),('↶ Заказы','adm:jobs:all:0',None)])
        rich=task_rich(t,(('Трафер',t['user_id']),('Тип',kind),('Создан',self.format_date(t['created_at'])),
                          ('Внешний ID',t['upstream_id'] or '—')))
        return rich,text,keyboard(*rows)

    async def admin_report(self,uid:int,tid:str):
        if not self.owner(uid):return
        t=self.db.task(tid)
        if not t or not t['upstream_id']:return await self.say(uid,'Отчёт пока недоступен.')
        try:
            report=await self.api.publication(t['upstream_id'])
            rich,plain=report_view(t,report)
            return await self.tg.rich(uid,rich,plain,keyboard([('↶ Заказ','adm:job:'+tid,None)]))
        except ApiError as exc:return await self.say(uid,safe_api_error(exc.code))

    async def admin_history(self,uid:int,mode:str,target:int|None=None):
        if not self.owner(uid):return
        if mode=='payments':
            entries=self.db.payments();text='<b>▤ Платежи Telegram Stars</b>\n\n'
            for v in entries:
                stamp=self.format_date(v['refunded_at'] or v['paid_at'])
                status='↩ Возврат' if v['refunded_at'] else '✓ Оплата'
                text+=f'├ {status} · <code>{v["user_id"]}</code> · {v["stars"]} XTR · {stamp}\n'
            reviews=self.db.conn.execute('SELECT * FROM payment_reviews ORDER BY created_at DESC LIMIT 8').fetchall()
            if reviews:
                text+='\n<b>! Требуют сверки</b>\n'
                labels={'payment':'Оплата','refund':'Возврат','refund_access':'Срок после возврата'}
                for v in reviews:
                    text+=f'├ {labels.get(v["kind"],"Операция")} · <code>{v["user_id"]}</code> · {v["stars"]} XTR\n'
                    text+=f'└ ID · <code>{h(v["charge_id"][:120])}</code>\n'
                entries=entries or reviews
        elif mode in ('subs','usubs'):
            entries=self.db.subscriptions(target);text='<b>◷ Изменения подписок</b>\n\n'
            for v in entries:
                text+=(f'├ <code>{v["user_id"]}</code> · {h(v["action"])}'
                       f' {v["days"]:+d} дн.\n   {self.format_date(v["at"])} · исполнитель {v["actor_id"]}\n')
        elif mode=='audit':
            entries=self.db.audit_entries();text='<b>▧ Журнал администратора</b>\n\n'
            for v in entries:text+=f'├ {self.format_date(v["at"])} · <code>{v["user_id"]}</code>\n   {h(v["action"])} / {h(v["target"][:40])}\n'
        else:return
        if not entries:text+='Записей пока нет.'
        await self.say(uid,text[:3950],keyboard([('↶ Трафер','adm:u:'+str(target),None)] if target is not None else [('↶ Панель','admin',None)]))

    async def admin_export(self,uid:int,kind:str):
        if not self.owner(uid) or kind not in ('members','jobs','payments','subscriptions'):return
        stream=io.StringIO();writer=SafeCsvWriter(stream)
        if kind=='members':
            writer.writerow(['user_id','username','full_name','first_seen_utc','last_seen_utc','expires_utc','blocked','accounts','tasks','paid_xtr','daily_limit','open_limit'])
            for r in self.db.conn.execute('SELECT user_id FROM users ORDER BY user_id'):
                u=self.db.user_summary(r['user_id'])
                writer.writerow([u['user_id'],u['username'],u['full_name'],_dt(u['first_seen']),_dt(u['last_seen']),_dt(u['expires_at']),u['blocked'],u['accounts_total'],u['tasks_total'],u['stars_paid'],u['daily_limit'],u['open_limit']])
        elif kind=='jobs':
            writer.writerow(['task_id','user_id','name','kind','state','upstream_id','created_utc','updated_utc','error_code'])
            for r in self.db.conn.execute('SELECT task_id,user_id,display_name,kind,state,upstream_id,created_at,updated_at,error_code FROM tasks ORDER BY created_at DESC'):
                writer.writerow([r['task_id'],r['user_id'],r['display_name'],r['kind'],r['state'],r['upstream_id'] or '',_dt(r['created_at']),_dt(r['updated_at']),r['error_code']])
        elif kind=='payments':
            writer.writerow(['user_id','stars','paid_utc','charge_id','refunded_utc'])
            for r in self.db.conn.execute('''SELECT i.user_id,i.stars,c.created_at,c.charge_id,c.refunded_at
                FROM charges c JOIN invoices i ON i.payload=c.invoice_payload ORDER BY c.created_at DESC'''):
                writer.writerow([r['user_id'],r['stars'],_dt(r['created_at']),r['charge_id'],_dt(r['refunded_at'])])
        else:
            writer.writerow(['user_id','actor_id','action','days','previous_expires','new_expires','at'])
            for r in self.db.conn.execute('SELECT * FROM subscription_events ORDER BY event_id DESC'):
                writer.writerow([r['user_id'],r['actor_id'],r['action'],r['days'],_dt(r['previous_expires']),_dt(r['new_expires']),_dt(r['at'])])
        data=('\ufeff'+stream.getvalue()).encode('utf-8')
        if len(data)>18*1024*1024:return await self.say(uid,'Отчёт слишком большой. Сделай экспорт через SQLite на сервере.')
        await self.tg.attachment(uid,data,f'proxima_{kind}.csv','Экспорт владельца · CSV')
        self.db.log(uid,'export_'+kind)

    async def admin_edit(self,uid:int,action:str,target:int,value:str='custom'):
        if not self.owner(uid):return
        if target<=0 or target==self.cfg.owner_id:return await self.say(uid,'Главного администратора изменить нельзя.')
        if action not in ACCESS_ACTIONS:return
        label,style=ACCESS_ACTIONS[action]
        self.db.register(target)
        if value=='custom':
            self.db.save_draft(uid,'admin_edit','input',{'action':action,'target':target})
            prompts={'extend':'Введи количество дней, которое нужно ДОБАВИТЬ (1–36500).',
                     'replace':'Введи срок в днях ОТ СЕГОДНЯ (1–36500).',
                     'assign':'Введи ID существующего аккаунта из API.',
                     'unassign':'Введи ID аккаунта, который нужно отвязать.',
                     'limit_daily':'Лимит задач в сутки (0 — без лимита).',
                     'limit_open':'Лимит одновременно ожидающих и выполняемых задач (0 — без лимита).'}
            return await self.say(uid,f'<b>{h(label)}</b>\nТрафер · <code>{target}</code>\n\n'+prompts.get(action,'Введи значение.'),
                                  keyboard([('↶ Отмена','adm:discard',None)]))
        try:
            val=self._validate_edit(action,value)
        except ValueError as exc:return await self.say(uid,'Ошибка ввода: '+h(exc))
        nonce=secrets.token_hex(6)
        self.db.save_draft(uid,'admin_edit','confirm',{'action':action,'target':target,'value':val,'nonce':nonce})
        value_label=days_label(val) if action in ('extend','replace') else str(val) if val is not None else ''
        text=f'<b>{h(label)}</b>\n\n├ Трафер · <code>{target}</code>'
        if value_label:text+='\n└ Значение · <code>'+h(value_label)+'</code>'
        return await self.say(uid,text,keyboard([(label,'adm:apply:'+nonce,style)],
                                                [('↶ Отмена','adm:discard',None)]))

    @staticmethod
    def _validate_edit(action:str,value:str):
        if action in ('extend','replace','limit_daily','limit_open'):
            try:x=int(value)
            except (TypeError,ValueError):raise ValueError('Требуется целое число') from None
            if (action in ('extend','replace') and not 1<=x<=36500) or (action.startswith('limit_') and not 0<=x<=100000):
                raise ValueError('Число вне допустимого диапазона')
            return x
        if action in ('assign','unassign'):
            if not ACC.fullmatch(str(value)):raise ValueError('Некорректный ID аккаунта')
            return str(value)
        if action in ('revoke','block','unblock'):return ''
        raise ValueError('Неизвестная операция')

    async def admin_input(self,uid:int,text:str)->bool:
        d=self.db.draft(uid)
        if not d or not self.owner(uid):return False
        if d['mode']=='admin_reconcile' and d['step']=='bind':
            await self.admin_bind_reconciliation(uid,d['data']['task_id'],text.strip())
            return True
        if d['mode']=='admin_search':
            if not 1<=len(text.strip())<=70:return await self.say(uid,'Введи ID, имя или username (1–70 символов).') or True
            prefs=self.db.preferences(uid);prefs['admin_search']=text.strip()
            self.db.set_preferences(uid,prefs);self.db.clear_draft(uid)
            await self.admin_users(uid,search=text.strip());return True
        if d['mode']=='admin_pick':
            value=text.strip()
            if not valid_member_id(value):
                await self.say(uid,'Проверь Telegram ID. Трафер может узнать его командой /myid.');return True
            self.db.clear_draft(uid)
            await self.admin_edit(uid,'extend',int(value),'custom');return True
        if d['mode']=='admin_edit':
            if d['step']=='input':
                await self.admin_edit(uid,d['data']['action'],int(d['data']['target']),text.strip());return True
            if d['step']=='confirm':
                await self.say(uid,'Параметры готовы. Подтверди кнопкой либо /cancel.');return True
        return False

    async def admin_apply(self,uid:int,nonce:str):
        if not self.owner(uid):return
        d=self.db.draft(uid)
        if not d or d['mode']!='admin_edit' or d['step']!='confirm' or d['data'].get('nonce')!=nonce:return
        if now()-d['updated_at']>600:
            self.db.clear_draft(uid)
            return await self.say(uid,'Подтверждение устарело. Начни заново.')
        data=d['data'];action=data['action'];target=int(data['target']);val=data['value']
        # Consume before mutating to prevent double action on duplicate callbacks.
        self.db.clear_draft(uid)
        if target==self.cfg.owner_id:return
        try:
            if action in ('extend','replace','revoke','block','unblock'):
                expiry=self.db.modify_access(target,uid,action,int(val or 0))
                message='✓ Доступ изменён. Новый срок: <code>'+h(self.format_date(expiry))+'</code>.'
            elif action in ('limit_daily','limit_open'):
                self.db.change_limit(target,uid,'daily' if action=='limit_daily' else 'open',val)
                message='✓ Лимит сохранён.'
            elif action in ('assign','unassign'):
                if action=='assign':
                    message=await self.assign_account(target,val)
                else:
                    message='✓ Отвязано.' if self.db.unassign(target,val) else 'Привязка не найдена.'
                self.db.log(uid,action+'_account',str(target)+':'+val)
            else:return
            await self.say(uid,message,keyboard([('◇ Карточка трафера','adm:u:'+str(target),'primary')]))
            if action in ('extend','replace','revoke','block','unblock'):
                try:await self.say(target,'<b>◇ Доступ Proxima Soft</b>\n\n'+
                    ('Твой доступ обновлён администратором. Открой /access, чтобы проверить срок.' if action not in ('revoke','block') else 'Доступ изменён администратором. Подробности: /access.'))
                except Exception:pass
        except ApiError as exc:await self.say(uid,'Операция не подтверждена API: '+safe_api_error(exc.code))
        except (ValueError,PermissionError) as exc:await self.say(uid,'Не выполнено: '+h(exc))

    async def admin_task_change(self,uid:int,action:str,tid:str):
        if not self.owner(uid) or not TASK.fullmatch(tid) or action not in ('cancel','stop','verify'):return
        t=self.db.task(tid)
        if not t:return await self.say(uid,'Заказ не найден.')
        if action=='cancel' and t['state']!='queued':return await self.say(uid,'Заказ уже не в очереди.')
        if action=='stop' and (t['state'] not in ('running','paused') or not t['upstream_id']):
            return await self.say(uid,'Операция недоступна на данном этапе.')
        if action=='verify' and not self.can_verify_order(t):
            return await self.say(uid,'Операция недоступна на данном этапе.')
        nonce=secrets.token_hex(6)
        self.db.save_draft(uid,'admin_jobedit','confirm',{'action':action,'tid':tid,'nonce':nonce})
        labels={'cancel':'Убрать из очереди','stop':'Остановить заказ','verify':'Проверить статусы'}
        await self.say(uid,f'<b>{labels[action]}</b>\n\nЗаказ · <code>{tid}</code> · трафер <code>{t["user_id"]}</code>',keyboard(
                [('✓ Подтверждаю','adm:jobapply:'+nonce,'danger' if action!='verify' else 'primary')],
                [('× Отмена','adm:job:'+tid,None)]))

    async def admin_task_apply(self,uid:int,nonce:str):
        if not self.owner(uid):return
        d=self.db.draft(uid)
        if not d or d['mode']!='admin_jobedit' or d['data'].get('nonce')!=nonce:return
        if now()-d['updated_at']>600:self.db.clear_draft(uid);return await self.say(uid,'Подтверждение истекло.')
        action,tid=d['data']['action'],d['data']['tid'];self.db.clear_draft(uid)
        t=self.db.task(tid)
        if not t:return
        try:
            if action=='cancel':
                success=self.db.cancel_queued(tid,t['user_id'])
                if not success:return await self.say(uid,'Задача уже запущена.')
            elif action=='stop' and t['state'] in ('running','paused') and t['upstream_id']:
                await self.api.stop(t['upstream_id']);self.db.transition(tid,'stopping',expected_state=t['state'])
            elif action=='verify' and self.can_verify_order(t):
                self.save_verified_snapshot(t,await self.api.verify(t['upstream_id']))
            else:return
            self.db.log(uid,'admin_task_'+action,tid)
            await self.say(uid,'✓ Команда принята.',keyboard([('▣ Заказ','adm:job:'+tid,None)]))
        except ApiError as exc:
            await self.say(uid,'Состояние операции не подтверждено: '+safe_api_error(exc.code))

    async def admin_reconciliation(self,uid:int,tid:str,action:str='view')->None:
        if not self.owner(uid) or not TASK.fullmatch(tid):return
        job=self.db.task(tid)
        if not job or job['state']!='reconcile':
            return await self.admin_job(uid,tid)
        if action in ('bind','absent','hold'):
            job=self.db.hold_reconciliation(tid)
            if not job:return await self.admin_job(uid,tid)
            if action=='bind':
                self.db.save_draft(uid,'admin_reconcile','bind',{'task_id':tid})
                return await self.say(uid,'<b>↪ Связать заказ</b>\n\nОтправь точный ID задания из Tokora.',
                                      keyboard([('↶ Отмена','adm:discard',None)]))
            if action=='absent':
                nonce=secrets.token_hex(6)
                fields=('task_id','state','error_code','updated_at','reconcile_attempts','upstream_id')
                self.db.save_draft(uid,'admin_reconcile','absent',{'nonce':nonce,'job':{key:job[key] for key in fields}})
                return await self.say(uid,'<b>! Закрыть сверку?</b>\n\nПодтверди только после проверки, что этот заказ не выполняется в Tokora. Это разблокирует его аккаунты.',
                                      keyboard([('× Подтвердить','adm:settle:'+nonce,'danger')],
                                               [('↶ Отмена','adm:discard',None)]))
            self.db.log(uid,'reconciliation_held',tid)
        name=marked_task_name(job)
        rows=[[('■ Удержать повторы','adm:hold:'+tid,None)]]
        if name or job['upstream_id']:
            rows.append([('↪ Связать по ID','adm:bind:'+tid,'primary')])
        rows.extend([[('× Закрыть сверку','adm:absent:'+tid,'danger')],[('↶ Заказ','adm:job:'+tid,None)]])
        text=(f'<b>↻ Сверка с Tokora</b> · <code>{tid}</code>\n\n'
              'Проверь задание в кабинете Tokora. Поиск по маркеру продолжает работать в фоне.')
        if name:text+=f'\n\nНазвание в Tokora\n<code>{h(name)}</code>'
        if job['upstream_id']:text+=f'\nВнешний ID · <code>{h(job["upstream_id"])}</code>'
        if job['error_code']=='reconcile_review':text+='\nПовторный запуск удержан.'
        await self.say(uid,text,keyboard(*rows))

    async def admin_bind_reconciliation(self,uid:int,tid:str,external_id:str)->None:
        if not self.owner(uid):return
        if not TASK.fullmatch(tid) or not ACC.fullmatch(external_id):
            return await self.say(uid,'Некорректный ID задания.')
        job=self.db.task(tid)
        if not job or job['state']!='reconcile':return await self.admin_job(uid,tid)
        result=await self.api.task(external_id)
        state=snapshot_state(result)
        known_id=job['upstream_id']==external_id
        if (not state or result.get('id')!=external_id or result.get('kind')!=job['kind']
                or (not known_id and not matches_reconciliation(job,result))):
            return await self.say(uid,'Tokora вернула другое задание. Проверь ID и маркер.')
        try:
            saved=self.db.transition(tid,state,upstream_id=external_id,snapshot=result,
                error_code='execution_failed' if state=='failed' else '',expected_state='reconcile')
        except sqlite3.IntegrityError:
            return await self.say(uid,'Этот ID уже привязан к другому заказу.')
        if saved:self.db.log(uid,'reconciliation_bound',tid)
        self.db.clear_draft(uid)
        await self.admin_job(uid,tid)

    async def admin_settle_reconciliation(self,uid:int,nonce:str)->None:
        if not self.owner(uid):return
        draft=self.db.draft(uid)
        if (not draft or draft['mode']!='admin_reconcile' or draft['step']!='absent'
                or draft['data'].get('nonce')!=nonce or now()-draft['updated_at']>600):
            return
        job=draft['data']['job']
        current=self.db.task(job['task_id'])
        if not current or current['state']!='reconcile':
            self.db.clear_draft(uid)
            return await self.admin_job(uid,job['task_id'])
        if job['upstream_id']:
            try:
                await self.api.task(job['upstream_id'])
            except ApiError as exc:
                if exc.status!=404 and exc.code not in ('not_found','job_not_found'):raise
            else:
                return await self.say(uid,'Tokora находит задание по сохранённому ID. Обнови статус в карточке сверки.')
        active=await self.api.active_tasks()
        if any(matches_reconciliation(current,result) or result.get('id')==current['upstream_id'] for result in active):
            return await self.say(uid,'Задание найдено среди активных. Открой карточку сверки.')
        if active and not marked_task_name(current) and not current['upstream_id']:
            return await self.say(uid,'Старый заказ без маркера можно закрыть после завершения активных задач Tokora.')
        if self.db.settle_reconciliation_absent(job):
            self.db.log(uid,'reconciliation_closed',job['task_id'])
        self.db.clear_draft(uid)
        await self.admin_job(uid,job['task_id'])

    async def admin_command(self,uid:int,text:str)->bool:
        cmd=text.split(maxsplit=1)[0].split('@')[0].lower()
        if cmd not in ('/admin','/traffers','/traffer','/admin_tasks','/grant','/setsub','/revoke','/block','/unblock','/adminsearch'):
            return False
        if not self.owner(uid):return True
        parts=text.split()
        try:
            if cmd=='/admin':await self.admin_dashboard(uid)
            elif cmd=='/traffers':await self.admin_users(uid)
            elif cmd=='/admin_tasks':await self.admin_jobs(uid)
            elif cmd=='/adminsearch':
                self.db.save_draft(uid,'admin_search','input',{})
                await self.say(uid,'Отправь имя, @username или Telegram ID. /cancel — отмена.')
            elif cmd=='/traffer' and len(parts)==2 and valid_member_id(parts[1]):
                await self.admin_user(uid,int(parts[1]))
            elif cmd in ('/grant','/setsub') and len(parts)==3 and valid_member_id(parts[1]):
                await self.admin_edit(uid,'extend' if cmd=='/grant' else 'replace',int(parts[1]),parts[2])
            elif cmd in ('/revoke','/block','/unblock') and len(parts)==2 and valid_member_id(parts[1]):
                await self.admin_edit(uid,{'/revoke':'revoke','/block':'block','/unblock':'unblock'}[cmd],int(parts[1]),'')
            else:await self.say(uid,'Формат: /grant ID ДНИ, /setsub ID ДНИ, /traffer ID, /revoke ID, /block ID, /unblock ID.')
        except ValueError as exc:await self.say(uid,'Ошибка: '+h(exc))
        return True

    async def admin_callback(self,uid:int,data:str)->bool:
        if not data.startswith('adm:'):return False
        if not self.owner(uid):return True
        fields=data.split(':')
        try:
            if data=='adm:discard':
                self.db.clear_draft(uid)
                await self.admin_dashboard(uid)
            elif data=='adm:search':
                self.db.save_draft(uid,'admin_search','input',{})
                await self.say(uid,'<b>⌕ Поиск трафера</b>\n\nОтправь Telegram ID, @username или имя.',
                               keyboard([('↶ Отмена','adm:discard',None)]))
            elif data=='adm:pick:grant':
                self.db.save_draft(uid,'admin_pick','input',{})
                await self.say(uid,'<b>＋ Выдать подписку</b>\n\nОтправь Telegram ID трафера. На следующем шаге укажи срок.',
                               keyboard([('↶ Отмена','adm:discard',None)]))
            elif data=='adm:api':
                await self.say(uid,'<b>▣ Управление общим API</b>\n\n'
                    'Разделы аккаунтов, прокси, публикаций и материалов доступны в меню владельца. '
                    'Команды /api и /op — только для главного администратора.',keyboard(
                    [('◇ Аккаунты','accounts',None),('◈ Прокси','proxies',None)],
                    [('▣ Публикации','posts',None),('▤ Материалы','materials',None)],
                    [('▤ Статистика кабинета','owner_show:stats',None),('⚙ Настройки','settings',None)],
                    [('↶ Панель','admin',None)]))
            elif data=='adm:exports':
                await self.say(uid,'<b>⇩ Экспорт CSV</b>\n\nФайлы содержат только локальные данные команды, без cookies, прокси и ключей API.',keyboard(
                    [('◇ Траферы','adm:export:members',None)],
                    [('▣ Заказы','adm:export:jobs',None)],
                    [('▤ Платежи','adm:export:payments',None)],
                    [('◷ Подписки','adm:export:subscriptions',None)],
                    [('↶ Панель','admin',None)]))
            elif len(fields)==3 and fields[1]=='export':await self.admin_export(uid,fields[2])
            elif len(fields)==4 and fields[1]=='users' and fields[2] in ('all','active','expired','blocked'):
                await self.admin_users(uid,fields[2],int(fields[3]))
            elif len(fields)==3 and fields[1]=='result':
                await self.admin_users(uid,'all',int(fields[2]),'__saved__')
            elif len(fields)==3 and fields[1]=='u' and valid_member_id(fields[2]):
                await self.admin_user(uid,int(fields[2]))
            elif len(fields)==3 and fields[1]=='uaccounts' and valid_member_id(fields[2]):
                await self.admin_user_accounts(uid,int(fields[2]))
            elif len(fields)==4 and fields[1]=='utasks' and valid_member_id(fields[2]):
                await self.admin_jobs(uid,int(fields[3]),target=int(fields[2]))
            elif len(fields)==4 and fields[1]=='jobs':await self.admin_jobs(uid,int(fields[3]),fields[2])
            elif len(fields)==3 and fields[1]=='job' and TASK.fullmatch(fields[2]):await self.admin_job(uid,fields[2])
            elif len(fields)==3 and fields[1]=='refresh' and TASK.fullmatch(fields[2]):await self.admin_refresh_job(uid,fields[2])
            elif len(fields)==3 and fields[1]=='report' and TASK.fullmatch(fields[2]):await self.admin_report(uid,fields[2])
            elif len(fields)==3 and fields[1] in ('reconcile','hold','bind','absent'):
                await self.admin_reconciliation(uid,fields[2],fields[1])
            elif len(fields)==3 and fields[1]=='settle':await self.admin_settle_reconciliation(uid,fields[2])
            elif len(fields)==4 and fields[1]=='jobedit':await self.admin_task_change(uid,fields[2],fields[3])
            elif len(fields)==3 and fields[1]=='jobapply':await self.admin_task_apply(uid,fields[2])
            elif len(fields) in (4,5) and fields[1]=='edit' and valid_member_id(fields[3]):
                await self.admin_edit(uid,fields[2],int(fields[3]),fields[4] if len(fields)==5 else '')
            elif len(fields)==3 and fields[1]=='apply':await self.admin_apply(uid,fields[2])
            elif data in ('adm:payments','adm:subs','adm:audit'):
                await self.admin_history(uid,data[4:])
            elif len(fields)==3 and fields[1]=='usubs' and valid_member_id(fields[2]):
                await self.admin_history(uid,'usubs',int(fields[2]))
        except (ValueError,OverflowError) as exc:
            await self.say(uid,'Некорректные параметры: '+h(str(exc)))
        return True
