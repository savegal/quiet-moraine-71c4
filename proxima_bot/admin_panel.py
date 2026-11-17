"""Owner-only Telegram operations console.

UI callback payloads contain IDs/actions, never credentials or arbitrary API paths.
Subscription and order confirmations are tied to the owner's own draft.
"""
from __future__ import annotations

import csv
import io
import json
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from urllib.parse import quote
from .accounts import ACCOUNT_STATUS,session_state
from .catalog import MODES, OWNER_ONLY
from .db import ACTIVE_STATES, MAX_USER_ID, PUBLICATION_TIMER_KINDS, now, snapshot_preparing
from .presentation import ERROR_LABELS, days_label, keyboard, number, pagination, state_label, state_symbol, task_view, task_rich, report_view, table_html, h
from .rich_ui import screen
from .api import ACCOUNT_FOLDER_ID, ApiError
from .bot import PUBLICATION_REPORT_KINDS, safe_api_error
from .worker import marked_task_name, matches_reconciliation, snapshot_state

UID=re.compile(r'^[1-9][0-9]{0,18}$')
TASK=re.compile(r'^[0-9a-f]{8}$')
ACC=re.compile(r'^[a-zA-Z0-9_-]{1,128}$')
ORDERS_WITH_VIDEOS={'publish','keyword_reply','fetch','usernames','feedwarm','commentlike'}
MODE_ACCESS_LABELS={'reply_links':'Ответы по ссылкам и из сбора'}


def valid_member_id(value:str)->bool:
    return bool(UID.fullmatch(value)) and int(value)<=MAX_USER_ID

ACCESS_ACTIONS={
    'extend':('＋ Продлить подписку','success'),
    'replace':('◷ Установить срок','success'),
    'revoke':('× Отозвать подписку','danger'),
    'block':('■ Заблокировать доступ','danger'),
    'unblock':('✓ Разблокировать доступ','success'),
    'assign':('◇ Назначить аккаунт','success'),
    'assign_batch':('◇ Выдать свободные аккаунты','success'),
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
        paused=hasattr(self.db,'dispatch_paused') and self.db.dispatch_paused()
        queue_state='На паузе' if paused else ('Выкл (.env)' if not self.cfg.dispatch_enabled else 'Активна')
        rows=[('Траферов',number(d['users']-bool(owner))),
              ('С подпиской / без',f'{number(active)} / {number(expired)}'),
              ('Заблокированы',number(d['blocked_users']-int(owner_status=='Заблокирован'))),
              ('Заказов',number(d['tasks_total'])),
              ('В очереди / в работе',f'{number(d["queued"])} / {number(d["running"])}'),
              ('Готово / ошибки',f'{number(d["done_tasks"])} / {number(d["failed_tasks"])}'),
              ('Назначено аккаунтов',number(d['assigned_accounts'])),
              ('Очередь запусков',queue_state)]
        rich,plain=table_html('▣ Центр управления',rows)
        if cabinet_name := getattr(self, 'cabinet_name', ''):
            rich += '<footer>Кабинет · ' + h(cabinet_name) + '</footer>'
            plain += '\n\nКабинет · ' + h(cabinet_name)
        await self.tg.rich(uid,rich,plain,keyboard(
            [('▥ Живая очередь','adm:liveq:0','primary'),
             ('▷ Снять паузу' if paused else 'Ⅱ Пауза очереди','adm:qpause:'+('off' if paused else 'on'),'success' if paused else 'danger')],
            [('◇ Траферы','adm:users:all:0','primary'),('▣ Заказы','adm:jobs:all:0',None)],
            [('＋ Выдать доступ','adm:pick:grant','success'),('▤ Как подключить','adm:onboard',None)],
            [('↑ Импорт себе',f'acctimp:start:{uid}','success'),('▤ Последний импорт',f'acctimp:last:{uid}',None)],
            [('◇ Мой пул',f'adm:uaccounts:{uid}',None)],
            [('⌕ Поиск трафера','adm:search',None),('◈ Промокод','promo_create',None)],
            [('◷ Подписки','adm:subs',None),('▣ Виды заказов','adm:modes',None)],
            [('▧ Журнал','adm:audit',None),('⇩ Экспорт CSV','adm:exports',None)],
            [('⚙︎ Кабинет','adm:api',None),('✓ Проверить API','admin_health',None)],
            *(([('▦ Кабинеты и ключи', 'ws:menu', None)],) if cabinet_name else ()),
            [('↶ Обзор','home',None)]))

    async def admin_order_modes(self,uid:int)->None:
        if not self.owner(uid):return
        restricted=self.restricted_modes()
        buttons=[]
        for mode,fields in MODES.items():
            if mode in OWNER_ONLY:
                continue
            label=MODE_ACCESS_LABELS.get(mode,fields[0])
            owner_only=mode in restricted
            status='🔒' if owner_only else '✓'
            buttons.append([(f'{status} {label}',f'adm:mode:{mode}:{int(not owner_only)}',None)])
        rich,plain=screen('▣ Виды заказов', (('✓','Доступно команде'),('🔒','Только владелец')),
                          subtitle='Выбери вид заказа, чтобы изменить доступ.',
                          caption='Проверка аккаунтов и публикации видео и фото доступны владельцу.')
        await self.tg.rich(uid,rich,plain,keyboard(*buttons,[('↶ Панель','admin',None)]))

    async def admin_order_mode_confirm(self,uid:int,mode:str,restricted:bool)->None:
        if not self.owner(uid) or mode not in MODES or mode in OWNER_ONLY:return
        if restricted==(mode in self.restricted_modes()):return await self.admin_order_modes(uid)
        nonce=secrets.token_hex(6)
        self.db.save_draft(uid,'admin_order_mode','confirm',{'mode':mode,'restricted':restricted,'nonce':nonce})
        choice='Оставить только владельцу' if restricted else 'Разрешить команде'
        label=MODE_ACCESS_LABELS.get(mode,MODES[mode][0])
        await self.say(uid,f'<b>▣ {h(label)}</b>\n\n{choice}?',keyboard(
            [(f'✓ {choice}',f'adm:modeapply:{nonce}','danger' if restricted else 'success')],
            [('↶ Отмена',f'adm:modecancel:{nonce}',None)]))

    async def admin_order_mode_apply(self,uid:int,nonce:str,*,cancel:bool=False)->None:
        if not self.owner(uid):return
        draft=self.db.draft(uid)
        if not draft or draft['mode']!='admin_order_mode' or draft['data'].get('nonce')!=nonce:
            return await self.admin_order_modes(uid)
        self.db.clear_draft(uid)
        if now()-draft['updated_at']>600:
            return await self.say(uid,'Подтверждение истекло.',keyboard([('↶ Виды заказов','adm:modes',None)]))
        if not cancel:
            self.set_mode_restricted(draft['data']['mode'],draft['data']['restricted'])
        await self.admin_order_modes(uid)

    def _member_label(self,target:int)->str:
        if target==self.cfg.owner_id:return 'Владелец'
        user=self.db.user(target) or {}
        if user.get('full_name'):return str(user['full_name'])[:40]
        if user.get('username'):return '@'+str(user['username'])[:32]
        return str(target)

    async def admin_live_queue(self,uid:int,page:int=0)->None:
        if not self.owner(uid):return
        ts=now()
        paused=hasattr(self.db,'dispatch_paused') and self.db.dispatch_paused()
        queue_states=(*ACTIVE_STATES,'reconcile','queued')
        placeholders=','.join('?' for _ in queue_states)
        all_rows=self.db.conn.execute(
            "SELECT task_id,user_id,display_name,kind,state,error_code,snapshot_json,execution_started_at,fire_at,created_at "
            f"FROM tasks WHERE state IN ({placeholders}) "
            "ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'paused' THEN 1 WHEN 'stopping' THEN 2 "
            "WHEN 'starting' THEN 3 WHEN 'reconcile' THEN 4 ELSE 5 END, created_at ASC, rowid ASC",
            queue_states,
        ).fetchall()
        size=8
        page=max(0,min(page,(len(all_rows)-1)//size)) if all_rows else 0
        visible=all_rows[page*size:(page+1)*size]
        running_cnt=sum(1 for r in all_rows if r['state'] in ACTIVE_STATES)
        queued_cnt=sum(1 for r in all_rows if r['state']=='queued')
        queue_state='На паузе' if paused else ('Выкл (.env)' if not self.cfg.dispatch_enabled else 'Активна')
        rows=[('Очередь запусков',queue_state),
              ('Активные / в очереди',f'{number(running_cnt)} / {number(queued_cnt)}')]
        buttons=[]
        for r in visible:
            st=r['state']
            snap={}
            with_snap=r['snapshot_json'] or '{}'
            try:snap=json.loads(with_snap)
            except Exception:snap={}
            if st=='running':
                started=r['execution_started_at'] or 0
                preparing=snapshot_preparing(snap)
                if preparing:
                    stage='Подготовка'
                elif r['kind'] in PUBLICATION_TIMER_KINDS:
                    stage='Публикации' if started else 'Ожидание начала выполнения'
                else:
                    stage='В работе'
                if started and not preparing:
                    stage+=f' · {max(0,(ts-started)//60)} мин'
            elif st=='queued':
                fire=r['fire_at'] or 0
                if fire>ts:stage=f'В очереди · старт через {max(1,(fire-ts+59)//60)} мин'
                else:stage='В очереди'
                if r['error_code']:stage+=' · '+ERROR_LABELS.get(r['error_code'],r['error_code'])
            else:
                stage=state_label(st)
                if r['error_code']:stage+=' · '+ERROR_LABELS.get(r['error_code'],r['error_code'])
            who=self._member_label(r['user_id'])
            name=str(r['display_name'] or r['kind'] or r['task_id'])[:28]
            rows.append((f'{state_symbol(st)} {who[:18]} · {name}',stage))
            buttons.append([(f'{state_symbol(st)} {who[:14]} · {name[:20]}',f'adm:job:{r["task_id"]}',None)])
        if not visible:
            rows.append(('Статус','Активных и отложенных заказов сейчас нет'))
        buttons.append(pagination('adm:liveq:',page,len(all_rows)>(page+1)*size))
        buttons.append([('▷ Снять паузу' if paused else 'Ⅱ Пауза очереди','adm:lqpause:'+('off' if paused else 'on'),'success' if paused else 'danger'),
                        ('↻ Обновить',f'adm:liveq:{page}',None)])
        buttons.append([('↶ Панель','admin',None)])
        caption='Очередь приостановлена: активные заказы завершатся, новые запуски не уйдут.' if paused else f'Актуально: стр. {page+1}. Нажми на заказ для деталей или остановки.'
        rich,plain=table_html('▥ Живая очередь',rows,caption)
        await self.tg.rich(uid,rich,plain,keyboard(*buttons))

    async def admin_users(self,uid:int,status:str='all',page:int=0,search:str=''):
        if not self.owner(uid):return
        if status not in ('all','active','expired','blocked') or page<0 or page>100000:return
        if search=='__saved__':search=self.db.preferences(uid).get('admin_search','')
        users,total=self.db.members(page*10,10,status,search)
        if page and page*10>=total:
            page=max(0,(total-1)//10)
            users,total=self.db.members(page*10,10,status,search)
        first=page*10+1 if users else 0
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
        is_owner=target==self.cfg.owner_id
        can_photos=bool(self.db.can_use_photos(target)) if hasattr(self.db,'can_use_photos') else False
        values=[
            ('Трафер',identity),('Telegram ID',target),('Статус',members_status(u,self.cfg.owner_id)),
            ('Подписка до',self.format_date(u['expires_at'])),
            ('Аккаунты / заказы',f'{number(u["accounts_total"])} / {number(u["tasks_total"])}'),
            ('В очереди / в работе',f'{number(active.get("queued",0))} / {number(sum(active.get(k,0) for k in ACTIVE_STATES))}'),
            ('Лимиты в сутки / открытых',f'{u["daily_limit"] if u["daily_limit"] is not None else "общий"} / {u["open_limit"] if u["open_limit"] is not None else "общий"}'),
            ('Фото в комментах','Включены' if (is_owner or can_photos) else 'Запрещены'),
            ('Последняя активность',self.format_date(u.get('last_seen',0))),
        ]
        if is_owner:
            values=[(label,value) for label,value in values if label not in ('Подписка до','Лимиты в сутки / открытых')]
        title='◇ Карточка владельца' if is_owner else '◇ Карточка трафера'
        caption='' if is_owner else 'Кнопки с ＋ продлевают подписку. «Установить срок» отсчитывает дни от сегодня.'
        rich,plain=table_html(title,values,caption)
        tid=str(target)
        access_rows=[
          [('＋ 1 день','adm:edit:extend:'+tid+':1','success'),('＋ 2 дня','adm:edit:extend:'+tid+':2','success')],
          [('＋ 7 дней','adm:edit:extend:'+tid+':7','success'),('＋ 30 дней','adm:edit:extend:'+tid+':30','success')],
          [('＋ Свой срок','adm:edit:extend:'+tid+':custom','primary'),('◷ Установить срок','adm:edit:replace:'+tid+':custom',None)],
        ]
        pool_rows=[
          [('▣ Заказы','adm:utasks:'+tid+':0',None),('◇ Аккаунты','adm:uaccounts:'+tid,None)],
          [('↑ Импорт себе' if is_owner else '↑ Импорт аккаунтов этому траферу','acctimp:start:'+tid,'success')],
          [('▤ Синхронизировать с папкой','adm:foldersync:'+tid,'primary')],
          [('＋ Назначить аккаунт','adm:edit:assign:'+tid+':custom',None),('− Отвязать','adm:edit:unassign:'+tid+':custom','danger')],
          [('＋ Выдать свободные аккаунты','adm:edit:assign_batch:'+tid+':custom',None)],
        ]
        policy_rows=[
          [('▤ Лимит в сутки','adm:edit:limit_daily:'+tid+':custom',None),('▤ Лимит очереди','adm:edit:limit_open:'+tid+':custom',None)],
          [('▧ Запретить фото' if can_photos else '✓ Разрешить фото',f'adm:toggle_photo:{tid}','danger' if can_photos else 'success')],
          [('◷ Подписки','adm:usubs:'+tid,None),('× Отозвать срок','adm:edit:revoke:'+tid,'danger')],
          [(( '✓ Разблокировать' if u['blocked'] else '■ Заблокировать'),
              'adm:edit:'+('unblock' if u['blocked'] else 'block')+':'+tid,'success' if u['blocked'] else 'danger')],
        ]
        rows=pool_rows if is_owner else access_rows+pool_rows+policy_rows
        rows.append([('↶ Траферы','adm:users:all:0',None),('▣ Панель','admin',None)])
        await self.tg.rich(uid,rich,plain,keyboard(*rows))

    async def admin_user_accounts(self,uid:int,target:int,page:int=0):
        if not self.owner(uid):return
        ids=self.db.account_ids(target)
        size=10
        page=max(0,min(page,(len(ids)-1)//size))
        visible=ids[page*size:(page+1)*size]
        account_error=''
        try:accounts=await self.assigned_accounts(visible)
        except ApiError as exc:
            accounts={}
            account_error=safe_api_error(exc.code)
        rows=[]
        lines=[]
        for aid in visible:
            if not ACC.fullmatch(aid):continue
            item=accounts.get(aid)
            profile=(item or {}).get('profile') or {}
            name=profile.get('username')
            label='@'+name if isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9_.]{1,32}',name) else aid
            status='○ Данные недоступны' if account_error else ACCOUNT_STATUS[session_state(item)]
            rows.append([(f'{status[0]} {label[:35]}','account:'+aid,None)])
            lines.append('├ '+h(label)+' · '+h(status)+'\n   <code>'+h(aid)+'</code>')
        rows.extend([pagination(f'adm:uaccounts:{target}:',page,len(ids)>(page+1)*size),
                     [('▥ Сводка пула',f'adm:pool:{target}','primary'),('⇩ CSV пула',f'ac:export:{target}',None)],
                     [('✓ Проверить сессии',f'check_pool:{target}','primary')] if ids else [],
                     [('↑ Импорт аккаунтов этому траферу',f'acctimp:start:{target}','success'),
                      ('▤ Последний импорт',f'acctimp:last:{target}',None)],
                     [('▤ Синхронизировать с папкой',f'adm:foldersync:{target}','primary')],
                     [('＋ Назначить по ID',f'adm:edit:assign:{target}:custom',None),
                      ('− Отвязать по ID',f'adm:edit:unassign:{target}:custom',None)],
                     [('↶ Панель','admin',None)] if target==uid else [('↶ Трафер',f'adm:u:{target}',None)]])
        title='Мой пул' if target==uid else 'Аккаунты трафера'
        text=f'<b>◇ {title}</b> · <code>{target}</code>\nВсего · <code>{number(len(ids))}</code> · стр. {page+1}\n\n'
        text+='\n'.join(lines) if lines else 'Нет назначенных аккаунтов.'
        if account_error:text+='\n\n'+h(account_error)
        await self.say(uid,text,self.owner_keyboard(*rows))

    async def admin_jobs(self,uid:int,page:int=0,state:str='all',target:int|None=None):
        if not self.owner(uid):return
        if page<0 or page>100000 or state not in ('all','queued',*ACTIVE_STATES,'reconcile','completed','stopped','failed','canceled'):return
        jobs,total=self.db.admin_tasks(page*10,10,target,state)
        if page and page*10>=total:
            page=max(0,(total-1)//10)
            jobs,total=self.db.admin_tasks(page*10,10,target,state)
        title=f'<b>▣ Заказы {"трафера "+str(target) if target is not None else "команды"}</b>'
        text=title+f' · {page*10+1 if jobs else 0}–{min(total,(page+1)*10)} / {total}\n\n'
        rows=[]
        for j in jobs:
            text+=f'├ <code>{j["task_id"]}</code> · {h(j["display_name"][:37])}\n   {h(state_label(j["state"]))} · ID {j["user_id"]}\n'
            rows.append([(f'{state_symbol(j["state"])} {j["task_id"]} · {state_label(j["state"])}','adm:job:'+j['task_id'],None)])
        if not jobs:text+='Пока нет заказов.'
        if target is None:
            filters=[('▤ Все','all'),('◷ Очередь','queued'),('▷ Запуск','starting'),
                     ('▷ В работе','running'),('Ⅱ Пауза','paused'),('■ Остановка','stopping'),
                     ('↻ Сверка','reconcile'),('! Ошибки','failed'),('✓ Готово','completed'),
                     ('■ Остановлены','stopped'),('× Отменены','canceled')]
            tabs=[(label,f'adm:jobs:{key}:0','primary' if state==key else None) for label,key in filters]
            rows[0:0]=[tabs[index:index+3] for index in range(0,len(tabs),3)]
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
        if job and job['upstream_id'] and job['state']!='reconcile':
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
        if t['upstream_id'] and t['state']!='reconcile':
            rows.append([('▤ Результаты','entries:'+job_id,'primary'),('⇩ Экспорт','exports:'+job_id,None)])
            if t['kind'] in PUBLICATION_REPORT_KINDS:
                rows.append([('▥ Отчёт','adm:report:'+job_id,None)])
            if t['kind'] in ORDERS_WITH_VIDEOS:
                rows.append([('▷ Видео заказа','videos:'+job_id,None)])
            if t['kind']=='fetch':
                rows.append([('▥ Итоги сбора','collected:'+job_id,None)])
            if t['kind']!='import' and ACC.fullmatch(t['upstream_id']):
                rows.append([('◇ Аккаунты задания','task_accounts:'+t['upstream_id']+':0',None)])
        if t['state']=='queued':
            rows.append([('× Отменить очередь','adm:jobedit:cancel:'+job_id,'danger')])
        if t['state']=='reconcile':
            rows.append([('↻ Сверка заказа','adm:reconcile:'+job_id,'primary')])
        if t['state'] in ('running','paused') and t['upstream_id']:
            control=('Ⅱ Пауза','adm:control:pause:') if t['state']=='running' else ('▷ Продолжить','adm:control:resume:')
            rows.append([(control[0],control[1]+job_id,None),('■ Остановить','adm:jobedit:stop:'+job_id,'danger')])
        if self.can_verify_order(t):
            rows.append([('✓ Проверить статусы','adm:jobedit:verify:'+job_id,None)])
        if t['upstream_id']:
            rows.append([('↻ Обновить','adm:refresh:'+job_id,None)])
        rows.append([('◇ Трафер','adm:u:'+str(t['user_id']),None),('↶ Заказы','adm:jobs:all:0',None)])
        rich=task_rich(t,(('Трафер',t['user_id']),('Тип',kind),('Создан',self.format_date(t['created_at'])),
                          ('Внешний ID',t['upstream_id'] or '—')))
        return rich,text,self.owner_keyboard(*rows)

    async def admin_report(self,uid:int,tid:str):
        if not self.owner(uid):return
        t=self.db.task(tid)
        if not t or not t['upstream_id'] or t['state']=='reconcile' or t['kind'] not in PUBLICATION_REPORT_KINDS:
            return await self.say(uid,'Отчёт о публикациях недоступен.',keyboard([('↶ Заказ','adm:job:'+tid,None)]))
        rows = [[('↻ Обновить отчёт','adm:report:'+tid,None)]]
        if self.can_prepare_report(t):
            rows.append([('▥ Подготовить отчёт','adm:jobedit:prepare_report:'+tid,'primary')])
        rows.append([('⇩ Экспорт','exports:'+tid,None),('↶ Заказ','adm:job:'+tid,None)])
        markup = keyboard(*rows)
        try:
            report=await self.api.publication(t['upstream_id'])
            rich,plain=report_view(t,report)
            preparation = report.get('preparation')
            if isinstance(preparation, dict):
                progress = f'{number(preparation.get("checked"))} / {number(preparation.get("total"))}'
                detail = f'Подготовка · {h(preparation.get("state"))} · {progress}'
                rich += f'<p>{detail}</p>'
                plain += f'\n\n{detail}'
            return await self.tg.rich(uid,rich,plain,markup)
        except ApiError as exc:
            message=('Подробный отчёт будет доступен после завершения заказа. Текущая статистика — в карточке.'
                     if exc.code=='job_running' else safe_api_error(exc.code))
            return await self.say(uid,message,markup)

    @staticmethod
    def can_prepare_report(task:dict)->bool:
        return (bool(task['upstream_id']) and task['kind'] in PUBLICATION_REPORT_KINDS
                and task['state'] in ('completed', 'stopped'))

    async def admin_history(self,uid:int,mode:str,target:int|None=None):
        if not self.owner(uid):return
        if mode in ('subs','usubs'):
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
        if not self.owner(uid) or kind not in ('members','jobs','subscriptions'):return
        stream=io.StringIO();writer=SafeCsvWriter(stream)
        if kind=='members':
            writer.writerow(['user_id','username','full_name','first_seen_utc','last_seen_utc','expires_utc','blocked','accounts','tasks','daily_limit','open_limit'])
            for r in self.db.conn.execute('SELECT user_id FROM users ORDER BY user_id'):
                u=self.db.user_summary(r['user_id'])
                writer.writerow([u['user_id'],u['username'],u['full_name'],_dt(u['first_seen']),_dt(u['last_seen']),_dt(u['expires_at']),u['blocked'],u['accounts_total'],u['tasks_total'],u['daily_limit'],u['open_limit']])
        elif kind=='jobs':
            writer.writerow(['task_id','user_id','name','kind','state','upstream_id','created_utc','updated_utc','error_code'])
            for r in self.db.conn.execute('SELECT task_id,user_id,display_name,kind,state,upstream_id,created_at,updated_at,error_code FROM tasks ORDER BY created_at DESC'):
                writer.writerow([r['task_id'],r['user_id'],r['display_name'],r['kind'],r['state'],r['upstream_id'] or '',_dt(r['created_at']),_dt(r['updated_at']),r['error_code']])
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
        if target<=0 or (target==self.cfg.owner_id and action not in ('assign','unassign','assign_batch')):
            return await self.say(uid,'Главного администратора изменить нельзя.')
        if action not in ACCESS_ACTIONS:return
        label,style=ACCESS_ACTIONS[action]
        self.db.register(target)
        if value=='custom':
            self.db.save_draft(uid,'admin_edit','input',{'action':action,'target':target})
            prompts={'extend':'Введи количество дней, которое нужно ДОБАВИТЬ (1–36500).',
                     'replace':'Введи срок в днях ОТ СЕГОДНЯ (1–36500).',
                     'assign':'Введи ID существующего аккаунта из API.',
                     'assign_batch':'Сколько свободных аккаунтов с активной сессией назначить? От 1 до 300.',
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
        if action=='assign_batch':
            if not re.fullmatch(r'[0-9]{1,3}',value) or not 1<=int(value)<=300:
                raise ValueError('Укажи от 1 до 300 аккаунтов.')
            return int(value)
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
        if target==self.cfg.owner_id and action not in ('assign','unassign','assign_batch'):return
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
            elif action=='assign_batch':
                count=await self.assign_batch(target,val)
                self.db.log(uid,'assign_batch',f'{target}:{count}')
                message=f'✓ Выдано свободных активных аккаунтов · <code>{count} / {val}</code>.'
            else:return
            await self.say(uid,message,keyboard(
                [('◇ Карточка участника','adm:u:'+str(target),'primary'),('◇ Аккаунты',f'adm:uaccounts:{target}',None)]))
            if action in ('extend','replace','revoke','block','unblock'):
                try:await self.say(target,'<b>◇ Доступ Proxima Soft</b>\n\n'+
                    ('Твой доступ обновлён администратором. Открой /access, чтобы проверить срок.' if action not in ('revoke','block') else 'Доступ изменён администратором. Подробности: /access.'))
                except Exception:pass
        except ApiError as exc:await self.say(uid,'Операция не подтверждена API: '+safe_api_error(exc.code))
        except (ValueError,PermissionError) as exc:await self.say(uid,'Не выполнено: '+h(exc))

    async def assign_batch(self,target:int,limit:int)->int:
        used={row[0] for row in self.db.conn.execute('SELECT account_id FROM accounts')}
        existing=set(self.db.account_ids(target))
        existing_folders:set[str]=set()
        seen=set()
        candidates:list[tuple[str,str]]=[]
        bucket_counts:dict[str,int]={}
        offset=0
        while True:
            page=await self.api.call('GET','/v1/accounts',params={'status':'active','offset':offset,'limit':200})
            accounts=page.get('accounts')
            if not isinstance(accounts,list):raise ApiError('invalid_response')
            page_ids={item.get('accountId') for item in accounts if isinstance(item,dict)
                      and isinstance(item.get('accountId'),str)}
            if not page_ids-seen:break
            for item in accounts:
                if not isinstance(item,dict):continue
                aid=item.get('accountId')
                if not isinstance(aid,str) or not ACC.fullmatch(aid):
                    continue
                raw_fid=str(item.get('folderId') or '').strip()
                fid=raw_fid if ACCOUNT_FOLDER_ID.fullmatch(raw_fid) else ''
                if aid in existing:
                    existing_folders.add(fid)
                    continue
                if item.get('status')!='active':
                    continue
                if aid not in used and aid not in seen:
                    candidates.append((aid,fid))
                    used.add(aid)
                    bucket_counts[fid]=bucket_counts.get(fid,0)+1
            seen.update(page_ids)
            if not existing and max(bucket_counts.values(),default=0)>=limit:
                break
            if existing and len(existing_folders)==1 and existing.issubset(seen):
                only_fid=next(iter(existing_folders))
                if sum(1 for _,f in candidates if f==only_fid)>=limit:
                    break
            if len(accounts)<200:break
            offset+=len(accounts)
        missing_existing=existing-seen
        if missing_existing and hasattr(self,'assigned_accounts') and (
                any(ACCOUNT_FOLDER_ID.fullmatch(f) for f in existing_folders)
                or any(ACCOUNT_FOLDER_ID.fullmatch(f) for _,f in candidates)):
            resolved=await self.assigned_accounts(sorted(missing_existing))
            if isinstance(resolved,dict):
                for aid in missing_existing:
                    row=resolved.get(aid)
                    if isinstance(row,dict):
                        raw_fid=str(row.get('folderId') or '').strip()
                        existing_folders.add(raw_fid if ACCOUNT_FOLDER_ID.fullmatch(raw_fid) else '')
        if len(existing_folders)>1:
            raise ValueError('У пользователя уже назначены аккаунты из разных папок. Выровняй папку перед массовым назначением.')
        if len(existing_folders)==1:
            only_fid=next(iter(existing_folders))
            if only_fid:
                selected=[aid for aid,fid in candidates if fid==only_fid][:limit]
            else:
                selected=[aid for aid,fid in candidates if not fid][:limit]
                if candidates and not selected:
                    raise ValueError('У пользователя уже есть аккаунты без папки, а свободные активные аккаунты находятся в папках.')
        else:
            buckets:dict[str,list[str]]={}
            for aid,fid in candidates:
                buckets.setdefault(fid,[]).append(aid)
            if buckets:
                chosen_fid=max(buckets,key=lambda k:(len(buckets[k])>=limit,bool(k),len(buckets[k])))
                selected=buckets[chosen_fid][:limit]
            else:
                selected=[]
        return self.db.assign_available_accounts(target,selected)

    def _folder_sync_diff(self,target:int,accounts:dict[str,dict])->tuple[set[str],int,list[str],list[str],list[str],int]:
        folder_ids={aid for aid in accounts if ACC.fullmatch(aid)}
        active_cnt=sum(1 for aid in folder_ids if (accounts.get(aid) or {}).get('status')=='active')
        current_ids=set(self.db.account_ids(target))
        owners={row[0]:row[1] for row in self.db.conn.execute('SELECT account_id,user_id FROM accounts')}
        to_add=sorted(aid for aid in folder_ids if aid not in owners)
        foreign=sorted(aid for aid in folder_ids if aid in owners and owners[aid]!=target)
        to_remove=sorted(current_ids-folder_ids)
        return folder_ids,active_cnt,to_add,foreign,to_remove,len(folder_ids&current_ids)

    async def admin_folder_sync_list(self,uid:int,target:int,page:int=0)->None:
        if not self.owner(uid):return
        if not self.db.user(target) and target!=self.cfg.owner_id:
            return await self.say(uid,'Трафер не найден.')
        try:
            res=await self.api.call('GET','/v1/account-folders',retries=0)
        except ApiError as exc:
            return await self.say(uid,safe_api_error(exc.code),keyboard([('↶ Карточка',f'adm:u:{target}',None)]))
        if isinstance(res,dict) and res.get('available') is False:
            return await self.say(uid,'Папки аккаунтов в кабинете пока недоступны.',
                                    keyboard([('↶ Карточка',f'adm:u:{target}',None)]))
        items=[item for item in (res.get('items') or []) if isinstance(item,dict)
               and ACCOUNT_FOLDER_ID.fullmatch(str(item.get('id') or ''))]
        rows=[('Получатель',self._member_label(target)),
              ('Назначено сейчас',number(len(self.db.account_ids(target)))),
              ('Папок / без папки',f'{number(len(items))} / {number(res.get("unassigned") or 0)}')]
        size=10
        page=max(0,min(page,(len(items)-1)//size))
        start=page*size
        buttons=[]
        for item in items[start:start+size]:
            fid=str(item['id'])
            name=str(item.get('name') or fid).strip()[:24] or fid
            cnt=number(item.get('count') or 0)
            act=number(item.get('active') or 0)
            buttons.append([(f'▤ {name} ({cnt} · акт. {act})',f'adm:fsp:{target}:{fid}',None)])
        if not buttons:
            rows.append(('Папки','Создай папку для трафера на сайте кабинета'))
        buttons.append(pagination(f'adm:foldersync:{target}:',page,start+size<len(items)))
        buttons.append([('↶ Аккаунты',f'adm:uaccounts:{target}',None),('↶ Карточка',f'adm:u:{target}',None)])
        rich,plain=table_html('▤ Синхронизация с папкой',rows,'Выбери папку кабинета, состав которой нужно сопоставить с пулом трафера.')
        await self.tg.rich(uid,rich,plain,self.owner_keyboard(*buttons))

    async def admin_folder_sync_preview(self,uid:int,target:int,fid:str)->None:
        if not self.owner(uid):return
        try:
            accounts=await self.api.accounts_in_folder(fid)
        except ApiError as exc:
            return await self.say(uid,safe_api_error(exc.code),keyboard([('↶ Папки',f'adm:foldersync:{target}',None)]))
        folder_ids,active_cnt,to_add,foreign,to_remove,already=self._folder_sync_diff(target,accounts)
        rows=[('Получатель',self._member_label(target)),
              ('Папка кабинета',fid),
              ('В папке (активных)',f'{number(len(folder_ids))} ({number(active_cnt)})'),
              ('Уже у трафера',number(already)),
              ('Свободных для добавления',number(len(to_add))),
              ('Занято другими траферами',number(len(foreign))),
              ('Лишних у трафера (вне папки)',number(len(to_remove)))]
        buttons=[]
        if to_add:
            buttons.append([('＋ Добавить свободные из папки',f'adm:fsg:{target}:{fid}:a','success')])
        if to_add or to_remove:
            buttons.append([('⇄ Точно 1-в-1 (добавить и убрать лишние)',f'adm:fsg:{target}:{fid}:e','danger' if to_remove else 'primary')])
        buttons.append([('↶ Выбор папки',f'adm:foldersync:{target}',None),('↶ Аккаунты',f'adm:uaccounts:{target}',None)])
        caption=('В этой папке есть аккаунты других траферов — для запуска без ошибок оставь в папке только одного участника.'
                 if foreign else '«Точно 1-в-1» привяжет свободные аккаунты папки и отвяжет аккаунты вне папки (если они не заняты в задачах).')
        rich,plain=table_html('▤ Предпросмотр синхронизации',rows,caption)
        await self.tg.rich(uid,rich,plain,keyboard(*buttons))

    async def admin_folder_sync_apply(self,uid:int,target:int,fid:str,mode:str)->None:
        if not self.owner(uid):return
        if self.db.account_check_pending():
            return await self.say(uid,'Дождись окончания общей проверки аккаунтов.',
                                    keyboard([('↶ Предпросмотр',f'adm:fsp:{target}:{fid}',None)]))
        try:
            accounts=await self.api.accounts_in_folder(fid)
        except ApiError as exc:
            return await self.say(uid,safe_api_error(exc.code),keyboard([('↶ Папки',f'adm:foldersync:{target}',None)]))
        folder_ids,_,to_add,foreign,to_remove,_=self._folder_sync_diff(target,accounts)
        added=0
        removed=0
        skipped_busy=0
        if to_add:
            try:added=self.db.assign_available_accounts(target,to_add)
            except ValueError as exc:
                return await self.say(uid,h(str(exc)),keyboard([('↶ Предпросмотр',f'adm:fsp:{target}:{fid}',None)]))
        if mode=='e' and to_remove:
            for aid in to_remove:
                try:
                    if self.db.unassign(target,aid):removed+=1
                except ValueError:
                    skipped_busy+=1
        self.db.log(uid,'folder_sync',f'{target}:{fid}:+{added}/-{removed}')
        rows=[('Получатель',self._member_label(target)),
              ('Папка кабинета',fid),
              ('Добавлено',number(added)),
              ('Отвязано лишних',number(removed))]
        if skipped_busy:rows.append(('Пропущено (заняты в задачах)',number(skipped_busy)))
        if foreign:rows.append(('Занято другими траферами',number(len(foreign))))
        assigned=set(self.db.account_ids(target))
        rows.append(('Всего у трафера',number(len(assigned))))
        if mode=='e' and assigned!=folder_ids:
            rows.extend([('Не назначено из папки',number(len(folder_ids-assigned))),
                         ('Осталось вне папки',number(len(assigned-folder_ids)))])
            title='Синхронизация выполнена частично'
        elif mode=='e':
            title='✓ Пул совпадает с папкой'
        else:
            title='✓ Добавление из папки завершено'
        rich,plain=table_html(title,rows)
        await self.tg.rich(uid,rich,plain,keyboard(
            [('◇ Аккаунты трафера',f'adm:uaccounts:{target}','primary')],
            [('↶ Карточка',f'adm:u:{target}',None)]))

    async def admin_task_change(self,uid:int,action:str,tid:str):
        if not self.owner(uid) or not TASK.fullmatch(tid) or action not in ('cancel','stop','verify','prepare_report'):return
        t=self.db.task(tid)
        if not t:return await self.say(uid,'Заказ не найден.')
        if action=='cancel' and t['state']!='queued':return await self.say(uid,'Заказ уже не в очереди.')
        if action=='stop' and (t['state'] not in ('running','paused') or not t['upstream_id']):
            return await self.say(uid,'Операция недоступна на данном этапе.')
        if action=='verify' and not self.can_verify_order(t):
            return await self.say(uid,'Операция недоступна на данном этапе.')
        if action=='prepare_report' and not self.can_prepare_report(t):
            return await self.say(uid,'Подготовка отчёта доступна после завершения публикаций.')
        nonce=secrets.token_hex(6)
        self.db.save_draft(uid,'admin_jobedit','confirm',{'action':action,'tid':tid,'nonce':nonce})
        labels={'cancel':'Убрать из очереди','stop':'Остановить заказ','verify':'Проверить статусы',
                'prepare_report':'Подготовить отчёт'}
        await self.say(uid,f'<b>{labels[action]}</b>\n\nЗаказ · <code>{tid}</code> · трафер <code>{t["user_id"]}</code>',keyboard(
                [('✓ Подтверждаю','adm:jobapply:'+nonce,'danger' if action in ('cancel','stop') else 'primary')],
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
                await self.stop_task(t)
            elif action=='verify' and self.can_verify_order(t):
                self.save_verified_snapshot(t,await self.api.verify(t['upstream_id']))
            elif action=='prepare_report' and self.can_prepare_report(t):
                path = '/v1/tasks/'+quote(t['upstream_id'],safe='')+'/report/prepare'
                preparation = await self.api.call('POST',path,retries=0)
                self.db.log(uid,'admin_task_prepare_report',tid)
                progress = f'{number(preparation.get("checked"))} / {number(preparation.get("total"))}'
                return await self.say(uid,'<b>▥ Подготовка отчёта</b>\n'
                    f'{h(preparation.get("state") or "Принято")} · {progress}',keyboard(
                    [('↻ Открыть отчёт','adm:report:'+tid,'primary')],
                    [('↶ Заказ','adm:job:'+tid,None)]))
            else:return
            self.db.log(uid,'admin_task_'+action,tid)
            await self.admin_job(uid,tid)
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
                return await self.say(uid,'<b>↪ Связать заказ</b>\n\nОтправь точный ID задания из кабинета.',
                                      keyboard([('↶ Отмена','adm:discard',None)]))
            if action=='absent':
                nonce=secrets.token_hex(6)
                fields=('task_id','state','error_code','updated_at','reconcile_attempts','upstream_id')
                self.db.save_draft(uid,'admin_reconcile','absent',{'nonce':nonce,'job':{key:job[key] for key in fields}})
                return await self.say(uid,'<b>! Закрыть сверку?</b>\n\nУбедись, что заказ завершился или отсутствует в кабинете. Закрытие сверки разблокирует его аккаунты.',
                                      keyboard([('× Подтвердить','adm:settle:'+nonce,'danger')],
                                               [('↶ Отмена','adm:discard',None)]))
            self.db.log(uid,'reconciliation_held',tid)
        name=marked_task_name(job)
        rows=[[('■ Удержать повторы','adm:hold:'+tid,None)]]
        if name or job['upstream_id']:
            rows.append([('↪ Связать по ID','adm:bind:'+tid,'primary')])
        rows.extend([[('× Закрыть сверку','adm:absent:'+tid,'danger')],[('↶ Заказ','adm:job:'+tid,None)]])
        text=(f'<b>↻ Сверка заказа</b> · <code>{tid}</code>\n\n'
              'Проверь задание в кабинете. Поиск по маркеру продолжает работать в фоне.')
        if name:text+=f'\n\nНазвание в кабинете\n<code>{h(name)}</code>'
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
            return await self.say(uid,'По этому ID найдено другое задание. Проверь ID и маркер.')
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
                return await self.say(uid,'Задание найдено по сохранённому ID. Обнови статус в карточке сверки.')
        active=await self.api.active_tasks()
        if any(matches_reconciliation(current,result) or result.get('id')==current['upstream_id'] for result in active):
            return await self.say(uid,'Задание найдено среди активных. Открой карточку сверки.')
        if active and not marked_task_name(current) and not current['upstream_id']:
            return await self.say(uid,'Старый заказ без маркера можно закрыть после завершения активных задач кабинета.')
        if self.db.settle_reconciliation_absent(job):
            self.db.log(uid,'reconciliation_closed',job['task_id'])
        self.db.clear_draft(uid)
        await self.admin_job(uid,job['task_id'])

    async def admin_command(self,uid:int,text:str)->bool:
        cmd=text.split(maxsplit=1)[0].split('@')[0].lower()
        if cmd not in ('/admin','/traffers','/traffer','/admin_tasks','/grant','/setsub','/setdays','/revoke',
                       '/block','/unblock','/adminsearch','/limit','/assign','/unassign'):
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
            elif cmd in ('/grant','/setsub','/setdays') and len(parts)==3 and valid_member_id(parts[1]):
                await self.admin_edit(uid,'extend' if cmd=='/grant' else 'replace',int(parts[1]),parts[2])
            elif cmd in ('/assign','/unassign') and len(parts)==3 and valid_member_id(parts[1]):
                await self.admin_edit(uid,cmd[1:],int(parts[1]),parts[2])
            elif cmd=='/limit' and len(parts)==4 and valid_member_id(parts[1]) and parts[2] in ('daily','open'):
                await self.admin_edit(uid,'limit_'+parts[2],int(parts[1]),parts[3])
            elif cmd in ('/revoke','/block','/unblock') and len(parts)==2 and valid_member_id(parts[1]):
                await self.admin_edit(uid,{'/revoke':'revoke','/block':'block','/unblock':'unblock'}[cmd],int(parts[1]),'')
            else:await self.say(uid,'Проверь формат команды и Telegram ID. Открой /admin, чтобы выполнить действие через меню.')
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
            elif len(fields)==3 and fields[1]=='liveq' and fields[2].isdigit():
                await self.admin_live_queue(uid,int(fields[2]))
            elif data=='adm:modes':
                await self.admin_order_modes(uid)
            elif len(fields)==4 and fields[1]=='mode' and fields[3] in ('0','1'):
                await self.admin_order_mode_confirm(uid,fields[2],fields[3]=='1')
            elif len(fields)==3 and fields[1] in ('modeapply','modecancel'):
                await self.admin_order_mode_apply(uid,fields[2],cancel=fields[1]=='modecancel')
            elif data in ('adm:qpause:on','adm:qpause:off'):
                mode=fields[2]
                self.db.set_dispatch_paused(mode=='on')
                self.db.log(uid,'dispatch_pause',mode)
                await self.admin_dashboard(uid)
            elif data in ('adm:lqpause:on','adm:lqpause:off'):
                mode=fields[2]
                self.db.set_dispatch_paused(mode=='on')
                self.db.log(uid,'dispatch_pause',mode)
                await self.admin_live_queue(uid,0)
            elif len(fields)==3 and fields[1]=='toggle_photo' and valid_member_id(fields[2]):
                target=int(fields[2])
                if hasattr(self.db,'toggle_photo_access'):
                    self.db.toggle_photo_access(target,uid)
                await self.admin_user(uid,target)
            elif len(fields)==3 and fields[1]=='foldersync' and valid_member_id(fields[2]):
                await self.admin_folder_sync_list(uid,int(fields[2]))
            elif (len(fields)==4 and fields[1]=='foldersync' and valid_member_id(fields[2])
                  and re.fullmatch(r'[0-9]{1,6}',fields[3])):
                await self.admin_folder_sync_list(uid,int(fields[2]),int(fields[3]))
            elif len(fields)==4 and fields[1]=='fsp' and valid_member_id(fields[2]) and ACCOUNT_FOLDER_ID.fullmatch(fields[3]):
                await self.admin_folder_sync_preview(uid,int(fields[2]),fields[3])
            elif len(fields)==5 and fields[1]=='fsg' and valid_member_id(fields[2]) and ACCOUNT_FOLDER_ID.fullmatch(fields[3]) and fields[4] in ('a','e'):
                await self.admin_folder_sync_apply(uid,int(fields[2]),fields[3],fields[4])
            elif data=='adm:onboard':
                rich,plain=table_html('◇ Как подключить участника',[
                    ('1. Telegram','Попроси человека открыть бота и отправить тебе свой ID из /myid.'),
                    ('2. Доступ','Выдай срок по Telegram ID. При необходимости задай лимиты задач.'),
                    ('3. Аккаунты','В карточке участника назначь свободные аккаунты или загрузи его файл.'),
                    ('4. Проверка','Проверь сессии пула. Участник увидит свои аккаунты и сможет создать заказ.'),
                ],'Управление общими прокси доступно владельцу.')
                await self.tg.rich(uid,rich,plain,keyboard(
                    [('＋ Выдать доступ','adm:pick:grant','success')],[('↶ Панель','admin',None)]))
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
            elif len(fields)==3 and fields[1]=='pool' and valid_member_id(fields[2]):
                await self.pool_summary(uid,int(fields[2]))
            elif len(fields)==4 and fields[1]=='uaccounts' and valid_member_id(fields[2]) and fields[3].isdigit():
                await self.admin_user_accounts(uid,int(fields[2]),int(fields[3]))
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
            elif len(fields)==4 and fields[1]=='control' and TASK.fullmatch(fields[3]):
                job=self.db.task(fields[3])
                if job and await self.change_task_execution(uid,job,fields[2]):await self.admin_job(uid,fields[3])
            elif len(fields)==3 and fields[1]=='jobapply':await self.admin_task_apply(uid,fields[2])
            elif len(fields) in (4,5) and fields[1]=='edit' and valid_member_id(fields[3]):
                await self.admin_edit(uid,fields[2],int(fields[3]),fields[4] if len(fields)==5 else '')
            elif len(fields)==3 and fields[1]=='apply':await self.admin_apply(uid,fields[2])
            elif data in ('adm:subs','adm:audit'):
                await self.admin_history(uid,data[4:])
            elif len(fields)==3 and fields[1]=='usubs' and valid_member_id(fields[2]):
                await self.admin_history(uid,'usubs',int(fields[2]))
        except (ValueError,OverflowError) as exc:
            await self.say(uid,'Некорректные параметры: '+h(str(exc)))
        return True
