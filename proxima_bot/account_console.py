"""Telegram account inventory, session diagnostics and safe reports."""
from __future__ import annotations

import asyncio
import csv
import io
import re
import secrets
from collections.abc import AsyncIterator
from hashlib import blake2s, sha256
from urllib.parse import quote, urlsplit

from .accounts import ACCOUNT_STATUS, has_proxy_binding, session_state
from .admin_panel import csv_safe
from .api import ACCOUNT_FOLDER_ID, ACCOUNT_PAGE_SIZE, ApiError
from .bot import safe_api_error
from .catalog import VALID_LONG_ID, VIDEO_ID, is_tiktok_url
from .db import MAX_USER_ID, now
from .export_parts import EXPORT_PART_BYTES, ExportParts
from .operations import VALID_ID
from .presentation import h, keyboard, number, pagination, public_system_text, rich_table, table_html

LIST_SIZE = 10
COMMENT_SIZE = 6
DRAFT_MODE = 'account_console'
SESSION_FILTERS = {
    'all': 'Все', 'active': 'Активные', 'expired': 'Истёкшие',
    'error': 'С ошибками', 'unknown': 'Не проверены',
}
HEALTH_FIELDS = (
    ('attempts24h', 'Попыток за сутки'),
    ('errors24h', 'Ошибок за сутки'),
    ('moderation24h', 'На модерации за сутки'),
    ('pending24h', 'Ожидают за сутки'),
    ('consecutiveErrors', 'Ошибок подряд'),
)


def public_account_username(profile:object)->str:
    username = profile.get('username') if isinstance(profile,dict) else None
    if isinstance(username,str) and re.fullmatch(r'@?[A-Za-z0-9_.]{1,32}',username):
        return '@'+username.lstrip('@')
    return ''


def account_page_items(result:dict, limit:int)->list[dict]:
    items = result.get('accounts') if isinstance(result,dict) else None
    if (not isinstance(items,list) or len(items)>limit or any(
            not isinstance(item,dict) or not isinstance(item.get('accountId'),str)
            or not VALID_ID.fullmatch(item['accountId']) for item in items)):
        raise ApiError('invalid_response')
    return items


class AccountConsoleMixin:
    def account_timestamp(self,value:object)->str:
        if type(value) is int and 0 < value < 32_503_680_000_000:
            return self.format_date(value//1000)
        return '—'

    def account_error_message(self,uid:int,exc:ApiError)->str:
        message = safe_api_error(exc.code)
        if self.owner(uid) and exc.request_id:
            message += '\nID запроса · <code>'+h(public_system_text(exc.request_id))+'</code>'
        return message

    def account_card_token(self,uid:int,aid:str)->str:
        key = sha256(self.cfg.telegram_token.encode()).digest()
        return blake2s(f'{uid}:{aid}'.encode(),key=key,digest_size=12).hexdigest()

    def can_view_account(self,uid:int,aid:str)->bool:
        return bool(VALID_ID.fullmatch(aid) and (self.owner(uid) or (
            self.db.is_active(uid,self.cfg.owner_id) and aid in self.db.account_ids(uid))))

    async def accounts(self,uid:int,page:int=0,status_filter:str='')->None:
        if self.owner(uid):
            return await self.account_inventory(uid,page,status_filter or 'all')
        if not self.db.is_active(uid,self.cfg.owner_id):
            return await self.access(uid)
        import_row=[('↑ Загрузить аккаунты','acctimp:self','success')]
        if self.db.account_import(uid,target_id=uid):
            import_row.append(('▤ Последний импорт',f'acctimp:last:{uid}',None))
        ids = self.db.account_ids(uid)
        if not ids:
            return await self.say(uid,
                '<b>◇ Мои аккаунты</b>\n\nВ твоём пуле пока нет аккаунтов.',
                keyboard(import_row,[('↶ Обзор','home',None)]))

        page = max(0,min(page,(len(ids)-1)//LIST_SIZE))
        visible = ids[page*LIST_SIZE:(page+1)*LIST_SIZE]
        nav = pagination('accounts_page:',page,(page+1)*LIST_SIZE<len(ids))
        rows = [
            nav,
            [('▥ Состояние пула','accounts_summary','primary'),('✓ Проверить пул','check_pool',None)],
            [('⇩ Выгрузить CSV','ac:export',None)],
            [('↶ Обзор','home',None)],
        ]
        try:
            accounts = await self.assigned_accounts(visible)
        except ApiError as exc:
            return await self.say(uid,'<b>◇ Мои аккаунты</b>\n\n'+self.account_error_message(uid,exc),
                                  keyboard(import_row,*rows))
        if not self.db.is_active(uid,self.cfg.owner_id):
            return await self.access(uid)
        if not set(visible).issubset(self.db.account_ids(uid)):
            return await self.say(uid,'Пул изменился. Открой список заново.',
                                  keyboard([('↻ Мои аккаунты','accounts','primary')]))
        items, cards = [], []
        for index,aid in enumerate(visible,page*LIST_SIZE+1):
            account = accounts.get(aid)
            username = public_account_username((account or {}).get('profile')) or f'Аккаунт {index}'
            status = ACCOUNT_STATUS[session_state(account)]
            items.append((username,status))
            if account is not None and VALID_ID.fullmatch(aid):
                token = self.account_card_token(uid,aid)
                cards.append([(f'{status[0]} {username}',f'ac:member:{token}',None)])
        rich,plain = table_html(f'◇ Мои аккаунты · стр. {page+1}',items,
                                f'{number(len(ids))} в пуле. Статусы последней проверки.')
        await self.tg.rich(uid,rich,plain,keyboard(import_row,*cards,*rows))

    async def account_inventory(self,uid:int,page:int,status:str)->None:
        if not self.owner(uid) or status not in SESSION_FILTERS:
            return
        page = max(0,page)
        query = self.db.preferences(uid).get('account_search','')
        offset = page*LIST_SIZE
        options = {'status':status} if status!='all' else {}
        controls = [
            [('⌕ Найти','ac:search','primary'),('⇩ Выгрузить CSV','ac:export',None)],
            [('▤ Все','ac:list:all:0',None),('✓ Активные','ac:list:active:0',None)],
            [('× Истёкшие','ac:list:expired:0',None),('! С ошибками','ac:list:error:0',None),
             ('○ Не проверены','ac:list:unknown:0',None)],
            [('▤ Папки кабинета','ac:folders:0',None),('▤ Проверка кабинета','owner_show:accounts_check','primary')],
            [('↑ Импорт себе','upload:cookies','success'),('▤ Последний импорт',f'acctimp:last:{uid}',None)],
            [('◈ Прокси','proxies',None),('↶ Панель','admin',None)],
        ]
        if query:
            controls.insert(1,[('× Сбросить поиск','ac:clear_search',None)])
        try:
            if query:
                result = await self.api.call('GET','/v1/accounts',
                                             params={'offset':offset,'limit':LIST_SIZE,'q':query,**options})
            else:
                result = await self.api.account_page(offset,LIST_SIZE,**options)
            items = account_page_items(result,LIST_SIZE)
        except ApiError as exc:
            return await self.say(uid,self.account_error_message(uid,exc),self.owner_keyboard(*controls))
        total = result.get('total',len(items))
        total = total if type(total) is int and total>=0 else len(items)
        prefix = 'accounts_page:' if status=='all' and not query else f'ac:list:{status}:'
        nav = pagination(prefix,page,offset+LIST_SIZE<total)
        if not items and page:
            nav = [('← К первой странице',f'ac:list:{status}:0',None),*nav]
        cards = []
        for item in items:
            aid = item['accountId']
            username = public_account_username(item.get('profile')) or aid[:26]
            label = ACCOUNT_STATUS[session_state(item)]
            cards.append([(f'{label[0]} {username} · {label[2:]}','account:'+aid,None)])

        values = [('Выборка',SESSION_FILTERS[status]),('Найдено',number(total))]
        if query:
            values.append(('Поиск',query))
        counts = result.get('counts')
        if isinstance(counts,dict):
            fields = (
                ('total','Всего в кабинете'),('active','Активные сессии'),
                ('expired','Истекли'),('error','Ошибки проверки'),('unknown','Не проверены'),
                ('webOnly','Только веб'),('paused','На паузе'),
            )
            values.extend((label,number(counts[key])) for key,label in fields if type(counts.get(key)) is int)
            if counts.get('lastCheckedAt'):
                values.append(('Проверка кабинета',self.account_timestamp(counts['lastCheckedAt'])))
        caption = 'Открой аккаунт, чтобы посмотреть его состояние.' if items else 'В этой выборке аккаунтов нет.'
        rich,plain = table_html(f'◇ Аккаунты кабинета · стр. {page+1}',values,caption)
        await self.tg.rich(uid,rich,plain,self.owner_keyboard(*cards,nav,*controls))

    async def account_folders(self,uid:int,page:int)->None:
        back=[('↶ Аккаунты','accounts',None)]
        try:
            data=await self.api.call('GET','/v1/account-folders',retries=0)
            items=data.get('items')
            unassigned=data.get('unassigned')
            if (not isinstance(items,list) or type(unassigned) is not int or unassigned<0
                    or type(data.get('available')) is not bool):
                raise ApiError('invalid_response')
            folders=[]
            for row in items:
                if (not isinstance(row,dict) or not isinstance(row.get('id'),str)
                        or not ACCOUNT_FOLDER_ID.fullmatch(row['id']) or not isinstance(row.get('name'),str)
                        or any(type(row.get(key)) is not int or row[key]<0 for key in ('count','active'))):
                    raise ApiError('invalid_response')
                folders.append((row['name'][:80],row['id'],f'{number(row["active"])} / {number(row["count"])}'))
        except ApiError as exc:
            return await self.say(uid,self.account_error_message(uid,exc),self.owner_keyboard(back))
        current=self.db.preferences(uid).get('order_account_folder')
        mode='all' if current in (None,'','all') else current
        names={folder:name for name,folder,_ in folders}
        scope_labels={'all':'Все аккаунты кабинета','none':'Аккаунты без папки','assigned':'Мой назначенный пул'}
        caption='Папки создаются в веб-кабинете. Владельцу по умолчанию доступны все аккаунты кабинета; для каждого трафера используй отдельную папку.'
        if not data['available']:
            caption='Сервер сейчас не может показать папки кабинета.'
        rows=[('Папок',number(len(folders))),('Аккаунтов без папки',number(unassigned)),
              ('Для моих заказов',scope_labels.get(mode,names.get(mode,mode)))]
        rich,plain=table_html('▤ Папки кабинета',rows,caption)
        page=min(page,max(0,(len(folders)-1)//LIST_SIZE))
        visible=folders[page*LIST_SIZE:(page+1)*LIST_SIZE]
        if visible:
            rich+=rich_table(('Папка','ID','Активны / всего'),visible)
            plain+='\n\n'+'\n'.join(f'{h(name)} · <code>{folder}</code> · {h(count)}' for name,folder,count in visible)
        nav=pagination('ac:folders:',page,(page+1)*LIST_SIZE<len(folders))
        controls=[]
        if mode!='all':
            controls.append([('Все аккаунты кабинета','ac:order_folder:all',None)])
        if mode!='none' and unassigned>0 and data['available']:
            controls.append([('Аккаунты без папки','ac:order_folder:none',None)])
        controls.extend([[(f'Для моих заказов · {name[:34]}','ac:order_folder:'+folder,None)]
                         for name,folder,_ in visible if folder!=mode and data['available']])
        if mode!='assigned' and self.db.account_ids(uid):
            controls.append([('Мой назначенный пул','ac:order_folder:assigned',None)])
        await self.tg.rich(uid,rich,plain,self.owner_keyboard(*controls,nav,back))

    async def account_info(self,uid:int,aid:str,view:dict|None=None)->None:
        if not self.can_view_account(uid,aid):
            return await self.say(uid,'Аккаунт недоступен.',keyboard([('↶ Аккаунты','accounts',None)]))
        try:
            account = view if view is not None else await self.api.call('GET','/v1/accounts/'+quote(aid,safe=''))
            if not isinstance(account,dict) or account.get('accountId')!=aid:
                raise ApiError('invalid_response')
        except ApiError as exc:
            message = 'Аккаунт больше не найден в кабинете.' if exc.status==404 else self.account_error_message(uid,exc)
            return await self.say(uid,message,keyboard([('↶ Аккаунты','accounts',None)]))
        if not self.can_view_account(uid,aid):
            return await self.say(uid,'Аккаунт недоступен.',keyboard([('↶ Аккаунты','accounts',None)]))
        profile = account.get('profile')
        username = public_account_username(profile)
        values = [
            ('Сессия',ACCOUNT_STATUS[session_state(account)]),
            ('Проверена',self.account_timestamp(account.get('checkedAt'))),
            ('Прокси','Привязан' if has_proxy_binding(account) else (
                'Общий пул' if account.get('proxySource')=='tokora' else 'Нет явной привязки')),
        ]
        health = account.get('publicationHealth')
        if isinstance(health,dict):
            values.extend((label,number(health[key])) for key,label in HEALTH_FIELDS if type(health.get(key)) is int)
            cooldown = health.get('cooldownUntil')
            if type(cooldown) is int and cooldown>now()*1000:
                values.append(('Пауза публикаций до',self.account_timestamp(cooldown)))
            if health.get('blockReason'):
                values.append(('Публикации','Есть ограничение'))
        rows = [[('↶ Мои аккаунты','accounts',None)]]
        if self.owner(uid):
            owner = self.db.conn.execute('SELECT user_id FROM accounts WHERE account_id=?',(aid,)).fetchone()
            values.extend([('ID аккаунта',aid),('Назначен',owner[0] if owner else 'Свободен')])
            if isinstance(profile,dict):
                nickname = profile.get('nickname')
                if isinstance(nickname,str) and nickname.strip():
                    values.append(('Имя профиля',nickname.strip()[:80]))
                values.extend((label,number(profile[key])) for key,label in (
                    ('followers','Подписчики'),('videos','Видео')) if type(profile.get(key)) is int)
            rows = [
                [('✓ Проверить сессию','ac:check:'+aid,'primary'),('↻ Обновить профиль','ac:profile:'+aid,None)],
                [('◈ Привязка прокси','px:bind:'+aid,None),('⌕ Комментарии к видео','ac:video:'+aid,None)],
                [('× Удалить из кабинета','acc_delete_confirm:'+aid,'danger')],
            ]
            if owner:
                rows.append([('▥ К пулу пользователя',f'adm:uaccounts:{owner[0]}',None)])
            rows.append([('↶ Аккаунты кабинета','accounts',None)])
        title = f'◇ {username}' if username else '◇ Аккаунт'
        rich,plain = table_html(title,values,'Сессия и публикации проверяются отдельно.')
        markup = self.owner_keyboard(*rows) if self.owner(uid) else keyboard(*rows)
        await self.tg.rich(uid,rich,plain,markup)

    async def account_console_change(self,uid:int,aid:str,action:str)->None:
        routes = {'check':'/check','profile':'/profile/refresh'}
        if not self.owner(uid) or not VALID_ID.fullmatch(aid) or action not in routes:
            return
        try:
            with self.db.account_action(aid):
                view = await self.api.call('POST','/v1/accounts/'+quote(aid,safe='')+routes[action],retries=0)
        except ValueError as exc:
            return await self.say(uid,h(exc),self.owner_keyboard([('↶ Аккаунт','account:'+aid,None)]))
        except ApiError as exc:
            return await self.say(uid,self.account_error_message(uid,exc),
                                  self.owner_keyboard([('↶ Аккаунт','account:'+aid,None)]))
        self.db.log(uid,'account_'+action,aid)
        await self.account_info(uid,aid,view)

    async def workspace_accounts(self, *, status:str='')->AsyncIterator[tuple[str,dict]]:
        offset = 0
        snapshot_total = None
        seen: set[str] = set()
        filters = {'status':status} if status else {}
        while True:
            try:
                result = await self.api.account_page(offset,ACCOUNT_PAGE_SIZE,**filters)
            except ApiError as exc:
                if exc.status!=429 or not 0<exc.retry_after<=60:
                    raise
                await asyncio.sleep(exc.retry_after)
                result = await self.api.account_page(offset,ACCOUNT_PAGE_SIZE,**filters)
            items = account_page_items(result,ACCOUNT_PAGE_SIZE)
            total = result.get('total')
            if type(total) is not int or total<0 or total<offset+len(items):
                raise ApiError('invalid_response')
            if snapshot_total is None:
                snapshot_total = total
            if not items and offset<snapshot_total:
                raise ApiError('invalid_response')
            for item in items[:snapshot_total-offset]:
                aid = item['accountId']
                if aid in seen:
                    raise ApiError('invalid_response')
                seen.add(aid)
                yield aid,item
            offset += len(items)
            if offset>=snapshot_total:
                return

    async def account_export_pool(self,ids:list[str])->AsyncIterator[tuple[str,dict|None]]:
        accounts = await self.assigned_accounts(ids)
        for aid in ids:
            yield aid,accounts.get(aid)

    async def account_console_export(self,uid:int,target:int|None=None)->None:
        owner = self.owner(uid)
        if not owner and (target is not None or not self.db.is_active(uid,self.cfg.owner_id)):
            return await self.access(uid)
        if target is not None and (not 0<target<=MAX_USER_ID or (target!=uid and not self.db.user(target))):
            return await self.say(uid,'Пользователь не найден.')
        workspace = owner and target is None
        ids = [] if workspace else self.db.account_ids(target if target is not None else uid)
        back = f'adm:uaccounts:{target}' if target is not None else 'accounts'
        nav = keyboard([('↶ К аккаунтам',back,None)])
        if not workspace and not ids:
            return await self.say(uid,'В пуле пока нет аккаунтов для выгрузки.',nav)
        await self.say(uid,'Готовлю CSV с данными аккаунтов. Большая выгрузка может занять несколько минут.',nav)
        assignments = {}
        if owner:
            assignments = {r['account_id']:r['user_id'] for r in self.db.conn.execute(
                'SELECT account_id,user_id FROM accounts')}
        rows = self.workspace_accounts() if workspace else self.account_export_pool(ids)
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)

        def encode_row(values:list)->bytes:
            stream.seek(0)
            stream.truncate()
            writer.writerow([csv_safe(value) for value in values])
            return stream.getvalue().encode('utf-8')

        pool_ids = set(ids)

        def can_send_pool()->bool:
            return self.db.is_active(uid,self.cfg.owner_id) and pool_ids.issubset(self.db.account_ids(uid))

        identity = ['account_id','assigned_telegram_id'] if owner else ['account_number']
        header = b'\xef\xbb\xbf'+encode_row([*identity,'username','session_status','checked_at','proxy_bound',
                                              *[field for field,_ in HEALTH_FIELDS],'cooldown_until'])
        filename = 'proxima_accounts.csv' if workspace else 'proxima_account_pool.csv'
        caption = 'Proxima Soft · '+('Аккаунты кабинета' if workspace else 'Пул аккаунтов')
        index = 0
        with ExportParts(filename,header=header,max_bytes=EXPORT_PART_BYTES) as export:
            try:
                async for aid,account in rows:
                    index += 1
                    info = account or {}
                    health = info.get('publicationHealth')
                    health = health if isinstance(health,dict) else {}
                    export.append(encode_row([
                        *([aid,assignments.get(aid,'')] if owner else [index]),
                        public_account_username(info.get('profile')),session_state(account),
                        self.account_timestamp(info.get('checkedAt')),
                        ('yes' if info.get('proxy') else 'no') if account is not None else '',
                        *[health[field] if type(health.get(field)) is int else '' for field,_ in HEALTH_FIELDS],
                        self.account_timestamp(health.get('cooldownUntil')),
                    ]))
            except ApiError as exc:
                return await self.say(uid,self.account_error_message(uid,exc),nav)
            except ValueError as exc:
                return await self.say(uid,h(exc),nav)
            if not index:
                return await self.say(uid,'В кабинете пока нет аккаунтов для выгрузки.',nav)
            if not await export.send(self.tg,uid,caption,allowed=None if owner else can_send_pool):
                return await self.say(uid,'Доступ к пулу изменился. Обнови список аккаунтов.',nav)
        if owner:
            self.db.log(uid,'account_export',str(target) if target is not None else 'workspace')

    async def account_video_comments(self,uid:int,aid:str,video:str,cursor:int,cid:str='')->None:
        if (not self.owner(uid) or not VALID_ID.fullmatch(aid) or not VALID_LONG_ID.fullmatch(video)
                or (cid and not VALID_LONG_ID.fullmatch(cid))):
            return
        base = f'/v1/accounts/{quote(aid,safe="")}/videos/{video}/comments'
        path = f'{base}/{cid}/replies' if cid else base
        prefix = f'ac:replies:{aid}:{video}:{cid}:' if cid else f'ac:comments:{aid}:{video}:'
        params = {'limit':COMMENT_SIZE}
        if cursor:
            params['cursor'] = cursor
        try:
            with self.db.account_action(aid):
                result = await self.api.call('GET',path,params=params)
            comments = result.get('items')
            if not isinstance(comments,list) or len(comments)>COMMENT_SIZE:
                raise ApiError('invalid_response')
        except ValueError as exc:
            return await self.say(uid,h(exc),self.owner_keyboard([('↶ Аккаунт','account:'+aid,None)]))
        except ApiError as exc:
            return await self.say(uid,self.account_error_message(uid,exc),self.owner_keyboard(
                [('↻ Повторить',prefix+str(cursor),'primary')],[('↶ Аккаунт','account:'+aid,None)]))
        title = 'Ответы на комментарий' if cid else 'Комментарии к видео'
        text = f'<b>⌕ {title}</b>\nВидео · <code>{video}</code>\n'
        if type(result.get('total')) is int:
            text += f'Всего · {number(result["total"])}\n'
        rows = []
        for index,item in enumerate(comments,1):
            if not isinstance(item,dict):
                continue
            comment_id = item.get('id')
            if (not isinstance(comment_id,str) or not VALID_LONG_ID.fullmatch(comment_id)
                    or item.get('videoId')!=video):
                continue
            author = public_account_username(item.get('author')) or 'Пользователь TikTok'
            body = str(item.get('text') or '—')[:220]
            text += f'\n<b>{index}. {h(author)}</b>\n{h(body)}\n'
            text += f'♡ {number(item.get("likes"))} · Ответы {number(item.get("replyCount"))}\n'
            actions = []
            parent_id = str(item.get('parentId') or (cid if cid and comment_id != cid else ''))
            if not cid:
                actions.append((f'↪ Ответы {index}',f'ac:replies:{aid}:{video}:{comment_id}:0',None))
            like_suffix = f':{parent_id}' if parent_id else ''
            if item.get('liked') is not True:
                actions.append((f'♡ Лайк {index}',f'ac:like:{aid}:{video}:{comment_id}:1{like_suffix}',None))
            if item.get('liked') is not False:
                actions.append((f'− Снять {index}',f'ac:like:{aid}:{video}:{comment_id}:0{like_suffix}',None))
            rows.append(actions)
        if not rows:
            text += '\nДоступных записей на этой странице нет.'
        next_cursor = result.get('nextCursor')
        nav = []
        if cursor:
            nav.append(('← К первой странице',prefix+'0',None))
        if result.get('hasMore') is True and type(next_cursor) is int and 0<=next_cursor<10**20 and next_cursor!=cursor:
            nav.append(('→ Далее',prefix+str(next_cursor),'primary'))
        rows.append(nav)
        if cid:
            rows.append([('↶ К комментариям',f'ac:comments:{aid}:{video}:0',None)])
        rows.append([('↶ Аккаунт','account:'+aid,None)])
        await self.say(uid,text,self.owner_keyboard(*rows))

    async def account_like_confirm(self,uid:int,aid:str,video:str,cid:str,liked:bool,parent_id:str='')->None:
        if (not self.owner(uid) or not VALID_ID.fullmatch(aid)
                or not VALID_LONG_ID.fullmatch(video) or not VALID_LONG_ID.fullmatch(cid)
                or (parent_id and not VALID_LONG_ID.fullmatch(parent_id))):
            return
        if self.db.account_in_use(aid):
            return await self.say(uid,'Аккаунт занят. Дождись завершения задачи.',
                                  self.owner_keyboard([('↶ Аккаунт','account:'+aid,None)]))
        nonce = secrets.token_hex(6)
        self.db.save_draft(uid,DRAFT_MODE,'like',{
            'account':aid,'video':video,'comment':cid,'liked':liked,'nonce':nonce,'parent':parent_id,
        })
        label = 'Поставить лайк' if liked else 'Снять лайк'
        await self.say(uid,
            f'<b>♡ {label}?</b>\n\nАккаунт · <code>{h(aid)}</code>\nКомментарий · <code>{cid}</code>',
            self.owner_keyboard([('✓ Подтвердить','ac:apply_like:'+nonce,'primary')],
                                [('× Отмена','ac:cancel_like:'+nonce,None)]))

    async def account_like_apply(self,uid:int,nonce:str,*,cancel:bool=False)->None:
        if not self.owner(uid):
            return
        draft = self.db.draft(uid)
        if (not draft or draft['mode']!=DRAFT_MODE or draft['step']!='like'
                or draft['data'].get('nonce')!=nonce):
            return
        self.db.clear_draft(uid)
        data = draft['data']
        aid,video,cid,liked = data['account'],data['video'],data['comment'],data['liked']
        parent_id = str(data.get('parent') or '')
        if cancel:
            if parent_id:
                return await self.account_video_comments(uid,aid,video,0,cid=parent_id)
            return await self.account_video_comments(uid,aid,video,0)
        refresh_cb = f'ac:replies:{aid}:{video}:{parent_id}:0' if parent_id else f'ac:comments:{aid}:{video}:0'
        refresh_label = '↻ Обновить ответы' if parent_id else '↻ Обновить комментарии'
        nav = self.owner_keyboard([(refresh_label,refresh_cb,'primary')],
                                  [('↶ Аккаунт','account:'+aid,None)])
        if now()-draft['updated_at']>600:
            return await self.say(uid,'Подтверждение истекло. Выбери действие заново.',nav)
        try:
            path = f'/v1/accounts/{quote(aid,safe="")}/videos/{video}/comments/{cid}/like'
            payload = {'liked':liked}
            if parent_id:
                payload['parentId'] = parent_id
            with self.db.account_action(aid):
                result = await self.api.call('PUT',path,payload=payload,retries=0)
            if (result.get('videoId')!=video or result.get('commentId')!=cid
                    or result.get('requestedLiked') is not liked):
                raise ApiError('invalid_response')
        except ValueError as exc:
            return await self.say(uid,h(exc),nav)
        except ApiError as exc:
            return await self.say(uid,self.account_error_message(uid,exc)+'\nОбнови список перед повтором.',nav)
        self.db.log(uid,'account_comment_like' if liked else 'account_comment_unlike',aid)
        if result.get('confirmed') is True:
            message = 'Лайк подтверждён.' if liked else 'Снятие лайка подтверждено.'
        elif result.get('accepted') is True:
            message = 'Запрос принят. Обнови комментарии, чтобы проверить результат.'
        else:
            message = 'Изменение не подтверждено. Обнови комментарии перед повтором.'
        await self.say(uid,message,nav)

    async def account_console_callback(self,uid:int,data:str)->bool:
        if not data.startswith('ac:'):
            return False
        _,action,*parts = data.split(':')
        if action=='export':
            if not parts:
                await self.account_console_export(uid)
            elif self.owner(uid) and len(parts)==1 and re.fullmatch(r'[0-9]{1,19}',parts[0]):
                await self.account_console_export(uid,int(parts[0]))
            return True
        if action=='member':
            if not self.db.is_active(uid,self.cfg.owner_id):
                await self.access(uid)
            elif len(parts)==1 and re.fullmatch(r'[0-9a-f]{24}',parts[0]):
                aid = next((a for a in self.db.account_ids(uid) if self.account_card_token(uid,a)==parts[0]),'')
                await self.account_info(uid,aid)
            return True
        if not self.owner(uid):
            return True
        if action=='list' and len(parts)==2 and re.fullmatch(r'[0-9]{1,7}',parts[1]):
            await self.account_inventory(uid,int(parts[1]),parts[0])
        elif action=='folders' and len(parts)==1 and re.fullmatch(r'[0-9]{1,7}',parts[0]):
            await self.account_folders(uid,int(parts[0]))
        elif action=='order_folder' and len(parts)==1 and (
                parts[0] in ('clear','all','none','assigned') or ACCOUNT_FOLDER_ID.fullmatch(parts[0])):
            prefs=self.db.preferences(uid)
            if parts[0] in ('clear','all'):
                prefs.pop('order_account_folder',None)
            else:
                prefs['order_account_folder']=parts[0]
            self.db.set_preferences(uid,prefs)
            await self.account_folders(uid,0)
        elif action=='search' and not parts:
            self.db.save_draft(uid,DRAFT_MODE,'search',{})
            await self.say(uid,'<b>⌕ Поиск аккаунта</b>\n\nВведи имя, ник или ID аккаунта.',
                           keyboard([('× Отмена','ac:cancel',None)]))
        elif action in ('clear_search','cancel') and not parts:
            if action=='clear_search':
                prefs = self.db.preferences(uid)
                prefs.pop('account_search',None)
                self.db.set_preferences(uid,prefs)
            draft = self.db.draft(uid)
            if draft and draft['mode']==DRAFT_MODE:
                self.db.clear_draft(uid)
            await self.accounts(uid)
        elif action in ('check','profile') and len(parts)==1:
            await self.account_console_change(uid,parts[0],action)
        elif action=='video' and len(parts)==1 and VALID_ID.fullmatch(parts[0]):
            self.db.save_draft(uid,DRAFT_MODE,'video',{'account':parts[0]})
            await self.say(uid,'<b>⌕ Комментарии к видео</b>\n\nПришли ссылку TikTok или ID видео.',
                           keyboard([('× Отмена','ac:cancel',None)]))
        elif action=='comments' and len(parts)==3 and re.fullmatch(r'[0-9]{1,20}',parts[-1]):
            await self.account_video_comments(uid,parts[0],parts[1],int(parts[2]))
        elif action=='replies' and len(parts)==4 and re.fullmatch(r'[0-9]{1,20}',parts[-1]):
            await self.account_video_comments(uid,parts[0],parts[1],int(parts[3]),parts[2])
        elif action=='like' and len(parts) in (4,5) and parts[3] in ('0','1'):
            parent=parts[4] if len(parts)==5 else ''
            if parent and not VALID_LONG_ID.fullmatch(parent):
                return True
            await self.account_like_confirm(uid,parts[0],parts[1],parts[2],parts[3]=='1',parent)
        elif action in ('apply_like','cancel_like') and len(parts)==1:
            await self.account_like_apply(uid,parts[0],cancel=action=='cancel_like')
        return True

    async def account_console_input(self,uid:int,text:str)->bool:
        draft = self.db.draft(uid)
        if not draft or draft['mode']!=DRAFT_MODE:
            return False
        if not self.owner(uid):
            return True
        value = text.strip()
        if draft['step']=='search':
            if not 1<=len(value)<=100 or not value.isprintable():
                await self.say(uid,'Введи от 1 до 100 символов для поиска.')
                return True
            self.db.clear_draft(uid)
            self.db.set_preferences(uid,{**self.db.preferences(uid),'account_search':value})
            await self.accounts(uid)
        elif draft['step']=='video':
            try:
                if VALID_LONG_ID.fullmatch(value):
                    video = value
                elif len(value)<=450 and is_tiktok_url(value):
                    direct = VIDEO_ID.search(urlsplit(value).path)
                    video = direct.group(1) if direct else await self.api.resolve(value)
                else:
                    await self.say(uid,'Нужна ссылка TikTok или числовой ID видео.')
                    return True
            except ApiError as exc:
                await self.say(uid,self.account_error_message(uid,exc))
                return True
            self.db.clear_draft(uid)
            await self.account_video_comments(uid,draft['data']['account'],video,0)
        elif draft['step']=='like':
            await self.say(uid,'Подтверди действие кнопкой или введи /cancel.')
        return True
