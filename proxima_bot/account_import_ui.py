"""Account file uploads. Only import metadata and counters are persisted."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import logging
from pathlib import PurePath
from secrets import token_hex

from .api import ACCOUNT_FOLDER_ID, ApiError, TelegramError
from .bot import safe_api_error
from .db import MAX_USER_ID
from .imports import ACCOUNT_FILE_SUFFIXES, MAX_ACCOUNT_FILE_BYTES, ImportSummary, import_and_assign, parse_account_file
from .presentation import h, keyboard, number, pagination, table_html

log=logging.getLogger(__name__)
PREVIEW_SECONDS=300
MAX_PREVIEW_FILES=10
MAX_PREVIEW_BYTES=20 * 1024 * 1024
MEMBER_FOLDER_REQUIRED='Попроси владельца привязать твою пустую или уже выделенную папку в админке.'


def import_folder_label(data:dict)->str:
    if data.get('folder_mode')=='none':return 'Не задавать папку'
    return str(data.get('folder_name') or data.get('folder_id') or 'Из файла, если указана').strip()


@dataclass
class FilePreview:
    token:str
    accounts:list[dict]
    fingerprints:set[bytes]
    size:int


def parse_upload(content:bytes,suffix:str)->tuple[list[dict],int]:
    accounts=parse_account_file(content,suffix)
    if any('proxy' in account for account in accounts):
        raise ValueError('Файл содержит адрес прокси. Используй серверный импорт или укажи proxyId.')
    size=len(json.dumps(accounts,ensure_ascii=False,separators=(',',':')).encode())
    return accounts,size


class AccountImportUI:
    def __init__(self,app):
        self.app=app
        self.db=app.db
        self.previews:dict[int,FilePreview]={}
        self.preview_timers:dict[int,asyncio.TimerHandle]={}
        self.task:asyncio.Task|None=None
        self.running_id:str|None=None
        self.stop_requested=False
        self.db.conn.execute("UPDATE account_imports SET state='interrupted' WHERE state='running'")

    def clear_preview(self,uid:int)->None:
        if timer:=self.preview_timers.pop(uid,None):timer.cancel()
        self.previews.pop(uid,None)

    def _touch_preview(self,uid:int)->None:
        if timer:=self.preview_timers.pop(uid,None):timer.cancel()
        self.preview_timers[uid]=asyncio.get_running_loop().call_later(PREVIEW_SECONDS,self.clear_preview,uid)

    async def close(self)->None:
        for uid in tuple(self.previews):self.clear_preview(uid)
        if self.task and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):await self.task

    def can_import(self,uid:int,target:int)->bool:
        return self.app.owner(uid) or (target==uid and self.db.is_active(uid,self.app.cfg.owner_id))

    def back_button(self,uid:int,target:int)->tuple:
        return ('↶ Аккаунты',f'adm:uaccounts:{target}' if self.app.owner(uid) else 'accounts',None)

    def get_job(self,uid:int,*,import_id:str|None=None,target_id:int|None=None)->dict|None:
        if not self.can_import(uid,target_id if target_id is not None else uid):return None
        job=self.db.account_import(uid,import_id=import_id,target_id=target_id,admin=self.app.owner(uid))
        return job if job and self.can_import(uid,job['target_id']) else None

    async def show_running(self,uid:int)->None:
        if self.get_job(uid,import_id=self.running_id):
            return await self.show(uid,import_id=self.running_id)
        await self.app.say(uid,'В кабинете уже идёт импорт. Попробуй немного позже.')

    def target_label(self,target:int)->str:
        if self.app.owner(target):return 'Владелец'
        user=self.db.user(target) or {}
        return user.get('full_name') or (f'@{user["username"]}' if user.get('username') else 'Трафер')

    async def start(self,uid:int,target:int)->None:
        if not self.can_import(uid,target):return
        if not 0<target<=MAX_USER_ID or (target!=uid and not self.db.user(target)):
            return await self.app.say(uid,'Трафер не найден. Открой его карточку в админке.')
        if self.running_id:return await self.show_running(uid)
        if not self.app.owner(uid) and not (self.db.member_import_folder(uid) or self.db.account_ids(uid)):
            return await self.app.say(uid,MEMBER_FOLDER_REQUIRED,keyboard([self.back_button(uid,target)]))
        self.clear_preview(uid)
        token=token_hex(6)
        self.db.save_draft(uid,'account_import','file',{'target':target,'token':token})
        label=h(self.target_label(target)[:100])
        await self.app.say(uid,f'<b>↑ Импорт аккаунтов</b>\n\n├ Получатель · <b>{label}</b>\n'
                           f'└ Telegram ID · <code>{target}</code>\n\n'
                           'Пришли TXT, JSON, ZIP, TAR или GZIP документом до 10 МиБ на файл. '
                           'Перед запуском бот покажет число записей и получателя.',
                           keyboard([('× Отменить','acctimp:discard:'+token,'danger')]))

    async def receive(self,uid:int,document:dict|None)->None:
        draft=self.db.draft(uid)
        if not draft or draft['mode']!='account_import' or not self.can_import(uid,draft['data']['target']):return
        if self.running_id:return await self.show_running(uid)
        if not document:return await self.app.say(uid,'Отправь TXT, JSON, ZIP, TAR или GZIP как документ.')
        suffix=PurePath(str(document.get('file_name') or '')).suffix.lower()
        if suffix not in ACCOUNT_FILE_SUFFIXES:
            return await self.app.say(uid,'Поддерживаются TXT, JSON, ZIP, TAR и GZIP.')
        if type(document.get('file_size')) is int and document['file_size']>MAX_ACCOUNT_FILE_BYTES:
            return await self.app.say(uid,'Размер файла превышает 10 МиБ.')
        try:
            content,_=await self.app.tg.fetch_upload(document.get('file_id') or '',MAX_ACCOUNT_FILE_BYTES)
        except (ValueError,TelegramError):
            return await self.app.say(uid,'Не удалось скачать документ. Проверь размер и отправь его ещё раз.')
        current=self.previews.get(uid)
        if current and current.token!=draft['data']['token']:current=None
        fingerprint=sha256(content).digest()
        if current and fingerprint in current.fingerprints:
            return await self.show_preview(uid,draft['data']['target'],current,'Этот файл уже добавлен.')
        if current and len(current.fingerprints)>=MAX_PREVIEW_FILES:
            return await self.app.say(uid,'В один импорт можно добавить до 10 файлов. Подтверди текущий список.')
        try:accounts,size=await asyncio.to_thread(parse_upload,content,suffix)
        except ValueError as exc:
            return await self.app.say(uid,'Файл не принят · '+h(exc))
        if self.db.draft(uid)!=draft or not self.can_import(uid,draft['data']['target']):return
        current=self.previews.get(uid)
        if current and current.token!=draft['data']['token']:current=None
        if size+(current.size if current else 0)>MAX_PREVIEW_BYTES:
            return await self.app.say(uid,'Объём записей в одном импорте превышает 20 МиБ. Раздели файлы.')
        if size+sum(preview.size for preview in self.previews.values())>MAX_PREVIEW_BYTES:
            return await self.app.say(uid,'В кабинете уже готовятся другие файлы. Попробуй загрузить файл немного позже.')
        self.clear_preview(uid)
        target=draft['data']['target']
        token=token_hex(6)
        if current:
            current.accounts.extend(accounts)
            current.fingerprints.add(fingerprint)
            current.size+=size
            current.token=token
        else:
            current=FilePreview(token,accounts,{fingerprint},size)
        self.previews[uid]=current
        self._touch_preview(uid)
        data={'target':target,'token':token}
        prev_folder=str(draft['data'].get('folder_id') or '')
        if ACCOUNT_FOLDER_ID.fullmatch(prev_folder):
            data['folder_id']=prev_folder
            if draft['data'].get('folder_name'):data['folder_name']=str(draft['data']['folder_name'])[:40]
        elif draft['data'].get('folder_mode')=='none':
            data['folder_mode']='none'
        self.db.save_draft(uid,'account_import','confirm',data)
        await self.show_preview(uid,target,current)

    async def show_preview(self,uid:int,target:int,preview:FilePreview,notice:str='')->None:
        draft=self.db.draft(uid) or {}
        data=draft.get('data') if isinstance(draft.get('data'),dict) else {}
        rich,plain=table_html('▤ Проверка импорта',[('Получатель',self.target_label(target)[:100]),
                              ('Telegram ID',target),('Файлов',len(preview.fingerprints)),
                              ('Записей',number(len(preview.accounts))),
                              ('Папка назначения',import_folder_label(data) if self.app.owner(uid) else 'Твоя выделенная папка')],
                              notice or 'Можно добавить ещё файл. Подтверждение доступно 5 минут.')
        await self.app.tg.rich(uid,rich,plain,keyboard(
            [('✓ Импортировать и назначить','acctimp:apply:'+preview.token,'success')],
            [('▤ Выбрать папку','acctimp:folders:'+preview.token,None)] if self.app.owner(uid) else [],
            [('× Отменить','acctimp:discard:'+preview.token,'danger')]))

    async def show_folders(self,uid:int,token:str,page:int=0)->None:
        if not self.app.owner(uid):return
        draft=self.db.draft(uid)
        preview=self.previews.get(uid)
        if not draft or draft['mode']!='account_import' or draft['data'].get('token')!=token or not preview or preview.token!=token:
            return await self.app.say(uid,'Этот экран импорта устарел. Открой аккаунты получателя заново.')
        self._touch_preview(uid)
        target=draft['data']['target']
        items=[]
        notice='Выбери папку кабинета, в которую попадут импортированные аккаунты.'
        try:
            res=await self.app.api.call('GET','/v1/account-folders',retries=0)
            if isinstance(res,dict) and res.get('available') is not False:
                items=[item for item in (res.get('items') or [])
                       if isinstance(item,dict) and ACCOUNT_FOLDER_ID.fullmatch(str(item.get('id') or ''))]
            elif isinstance(res,dict) and res.get('available') is False:
                notice='Папки в кабинете недоступны.'
        except ApiError:
            notice='Не удалось загрузить список папок кабинета.'
        current=str(draft['data'].get('folder_id') or '')
        folder_mode=draft['data'].get('folder_mode')
        size=20
        page=min(page,max(0,(len(items)-1)//size))
        start=page*size
        folders_map={}
        rows=[('Получатель',self.target_label(target)[:100]),
              ('Записей',number(len(preview.accounts))),
              ('Выбор папки',import_folder_label(draft['data']))]
        use_file=not current and folder_mode!='none'
        buttons=[
            [('Из файла'+(' ✓' if use_file else ''),f'acctimp:folder:{token}:file','primary' if use_file else None)],
            [('Не задавать папку'+(' ✓' if folder_mode=='none' else ''),f'acctimp:folder:{token}:none',
              'primary' if folder_mode=='none' else None)],
        ]
        for item in items[start:start+size]:
            fid=str(item['id'])
            name=str(item.get('name') or fid).strip()[:40] or fid
            folders_map[fid]=name
            count=number(item.get('count') or 0)
            mark=' ✓' if fid==current else ''
            buttons.append([(f'▤ {name[:32]} ({count}){mark}',f'acctimp:folder:{token}:{fid}','primary' if fid==current else None)])
        buttons.append(pagination(f'acctimp:folders:{token}:',page,start+size<len(items)))
        buttons.append([('↶ Назад к проверке',f'acctimp:folder:{token}:back',None)])
        self.db.save_draft(uid,'account_import','confirm',{**draft['data'],'folders':folders_map})
        rich,plain=table_html('▤ Папка для импорта',rows,notice)
        await self.app.tg.rich(uid,rich,plain,keyboard(*buttons))

    async def pick_folder(self,uid:int,token:str,fid:str)->None:
        if not self.app.owner(uid):return
        draft=self.db.draft(uid)
        preview=self.previews.get(uid)
        if not draft or draft['mode']!='account_import' or draft['data'].get('token')!=token or not preview or preview.token!=token:
            return await self.app.say(uid,'Этот экран импорта устарел. Открой аккаунты получателя заново.')
        self._touch_preview(uid)
        target=draft['data']['target']
        data={k:v for k,v in draft['data'].items() if k!='folders'}
        folders_map=draft['data'].get('folders') if isinstance(draft['data'].get('folders'),dict) else {}
        if fid=='back':
            return await self.show_preview(uid,target,preview)
        if fid in ('none','file'):
            data.pop('folder_id',None)
            data.pop('folder_name',None)
            if fid=='none':data['folder_mode']='none'
            else:data.pop('folder_mode',None)
            self.db.save_draft(uid,'account_import','confirm',data)
            notice='Бот пропустит поле папки при импорте.' if fid=='none' else 'Бот возьмёт папку из каждой записи, где она указана.'
            return await self.show_preview(uid,target,preview,notice)
        if not ACCOUNT_FOLDER_ID.fullmatch(fid) or fid not in folders_map:
            return
        fname=folders_map[fid]
        data.pop('folder_mode',None)
        data['folder_id']=fid
        data['folder_name']=fname
        self.db.save_draft(uid,'account_import','confirm',data)
        await self.show_preview(uid,target,preview,f'Выбрана папка: {fname}.')

    async def callback(self,uid:int,data:str)->None:
        if not self.can_import(uid,uid):return
        if data=='acctimp:self':return await self.start(uid,uid)
        parts=data.split(':')
        if len(parts)==4 and parts[1]=='folder':
            return await self.pick_folder(uid,parts[2],parts[3])
        if len(parts)==4 and parts[1]=='folders' and len(parts[3])<=6 and parts[3].isascii() and parts[3].isdecimal():
            return await self.show_folders(uid,parts[2],int(parts[3]))
        if len(parts)!=3:return
        _,action,value=parts
        if action in ('start','last') and len(value)<=19 and value.isascii() and value.isdecimal():
            target=int(value)
            if not 0<target<=MAX_USER_ID:return
            if not self.can_import(uid,target):return
            if action=='start':
                if self.app.owner(uid):return await self.start(uid,target)
                return
            return await self.show(uid,target_id=target)
        if action=='status' and len(value)==12:return await self.show(uid,import_id=value)
        if action=='result' and len(value)==12:return await self.send_result(uid,value)
        if action=='folders':return await self.show_folders(uid,value)
        if action=='stop' and value==self.running_id and self.get_job(uid,import_id=value):
            self.stop_requested=True
            return await self.show(uid,import_id=value)
        if action not in ('apply','discard'):return
        draft=self.db.draft(uid)
        if not draft or draft['mode']!='account_import' or draft['data'].get('token')!=value:
            return await self.app.say(uid,'Этот экран импорта устарел. Открой аккаунты получателя заново.')
        target=draft['data']['target']
        if not self.can_import(uid,target):return
        if action=='discard':
            self.clear_preview(uid)
            self.db.clear_draft(uid)
            return await self.app.say(uid,'Импорт отменён.',keyboard([self.back_button(uid,target)]))
        if self.running_id:return await self.show_running(uid)
        preview=self.previews.get(uid)
        if draft['step']!='confirm' or not preview or preview.token!=value:
            return await self.start(uid,target)
        if self.app.pool_checks.running_id or self.db.account_check_pending():
            return await self.show_preview(uid,target,preview,
                                           'Дождись окончания проверки аккаунтов, затем подтверди импорт.')
        folder_id=str(draft['data'].get('folder_id') or '')
        accounts=preview.accounts
        if not self.app.owner(uid):
            accounts=[{'cookies':entry['cookies']} for entry in accounts]
        elif draft['data'].get('folder_mode')=='none':
            accounts=[{key:value for key,value in entry.items() if key!='folderId'} for entry in accounts]
        elif ACCOUNT_FOLDER_ID.fullmatch(folder_id):
            accounts=[{**entry,'folderId':folder_id} for entry in accounts]
        self.clear_preview(uid)
        self.db.clear_draft(uid)
        await self.launch(uid,target,accounts)

    def render(self,uid:int,job:dict)->tuple[str,str,dict]:
        status=job['state']
        summary=job['summary']
        total=summary['total']
        finished=sum(summary.get(key,0) for key in ('assigned','duplicates','conflicts','rejected','unverified'))
        title={'running':'↻ Импорт аккаунтов','finished':'▤ Итог импорта','interrupted':'! Импорт прерван'}[status]
        conflict_label='Закреплены за другим' if self.app.owner(uid) else 'Вне твоего пула'
        rows=[('Получатель',self.target_label(job['target_id'])[:100]),('Telegram ID',job['target_id']),
              ('Обработано',f'{number(finished)} / {number(total)}'),('Закреплено',number(summary['assigned'])),
              ('Ждут проверки',number(summary['pending'])),('Дубли',number(summary['duplicates'])),
              (conflict_label,number(summary['conflicts'])),('Ошибки',number(summary['rejected']))]
        reason=summary['stopped']
        if summary.get('unverified'):
            rows.append(('UID не определён',number(summary['unverified'])))
            if not reason:reason='Для этих записей нужен файл с cookies multi_sids и sessionid, чтобы проверить владельца аккаунта.'
        if status=='interrupted':
            reason='Процесс перезапустился. Для оставшихся записей загрузи файл снова.'
        elif status=='running' and self.stop_requested and self.running_id==job['import_id']:
            reason='Остановка после текущей записи.'
        elif reason=='HTTP 401':reason='Импорт временно недоступен. Проверь диагностику.' if self.app.owner(uid) else 'Импорт временно недоступен. Обратись к владельцу.'
        if status!='running':rows.append(('Без результата',number(total-finished)))
        rich,plain=table_html(title,rows,caption=reason)
        import_id=job['import_id']
        if status=='running':
            controls=keyboard([('↻ Обновить','acctimp:status:'+import_id,None),
                               ('■ Остановить','acctimp:stop:'+import_id,'danger')])
        else:
            result=[('⇩ Назначенные ID','acctimp:result:'+import_id,None)] if summary['assigned'] else []
            start=f'acctimp:start:{job["target_id"]}' if self.app.owner(uid) else 'acctimp:self'
            controls=keyboard(result,[('↑ Загрузить ещё',start,'primary')],[self.back_button(uid,job['target_id'])])
        return rich,plain,controls

    async def send_result(self,uid:int,import_id:str)->None:
        if not self.get_job(uid,import_id=import_id):return
        items=self.db.account_import_items(import_id)
        if not self.app.owner(uid):
            assigned=set(self.db.account_ids(uid))
            items=[(aid,ready) for aid,ready in items if aid in assigned]
        if not items:return await self.app.say(uid,'Назначенных аккаунтов в этом импорте пока нет.')
        lines=[aid+' · '+('готов' if ready else 'ждёт проверки') for aid,ready in items]
        await self.app.tg.attachment(uid,('\n'.join(lines)+'\n').encode(),
                                     f'accounts-{import_id}.txt','Назначенные аккаунты · ID и статус при импорте')

    async def show(self,uid:int,*,import_id:str|None=None,target_id:int|None=None)->None:
        job=self.get_job(uid,import_id=import_id,target_id=target_id)
        if not job:return await self.app.say(uid,'Импортов для этого получателя пока нет.')
        await self.app.live_cards.open_account_import(uid,job)

    async def validate_member_folder(self,uid:int,folder_id:str,assigned:set[str])->None:
        if not ACCOUNT_FOLDER_ID.fullmatch(folder_id):raise ValueError(MEMBER_FOLDER_REQUIRED)
        if self.db.member_import_folder_assignee(folder_id) not in (None,uid):
            raise ValueError('Эта папка уже привязана к другому траферу.')
        if not assigned:
            folders=await self.app.api.call('GET','/v1/account-folders',retries=0)
            items=folders.get('items')
            if folders.get('available') is not True or not isinstance(items,list) or not any(
                    isinstance(item,dict) and item.get('id')==folder_id for item in items):
                raise ValueError('Выделенная папка недоступна. '+MEMBER_FOLDER_REQUIRED)
        scoped=await self.app.api.accounts_in_folder(folder_id,expected_count=len(assigned) if assigned else None)
        if set(scoped)!=assigned or set(self.db.account_ids(uid))!=assigned:
            raise ValueError('Состав папки отличается от твоего пула. '+MEMBER_FOLDER_REQUIRED)

    async def member_folder(self,uid:int,assigned:set[str])->str:
        folder_id=self.db.member_import_folder(uid)
        if not folder_id and assigned:
            records=await self.app.api.accounts_for(sorted(assigned))
            folders={row.get('folderId') for row in records.values()}
            if len(folders)==1 and set(records)==assigned:
                folder_id=next(iter(folders))
        if not isinstance(folder_id,str):raise ValueError(MEMBER_FOLDER_REQUIRED)
        await self.validate_member_folder(uid,folder_id,assigned)
        return folder_id

    async def launch(self,uid:int,target:int,accounts:list[dict])->None:
        if not self.can_import(uid,target):return
        if self.running_id:return await self.show_running(uid)
        import_id=token_hex(6)
        summary=ImportSummary(len(accounts))
        self.db.create_account_import(import_id,uid,target,asdict(summary))
        self.running_id=import_id
        self.stop_requested=False
        self.task=asyncio.create_task(self.run_import(uid,target,import_id,accounts,summary))
        await self.show(uid,import_id=import_id)

    async def run_import(self,uid:int,target:int,import_id:str,accounts:list[dict],
                         summary:ImportSummary)->None:
        self_service=not self.app.owner(uid)
        expected_pool=set()
        folder_id=None

        def allowed():
            return self.can_import(uid,target) and (not self_service or (
                self.db.member_import_folder(uid)==folder_id and set(self.db.account_ids(uid))==expected_pool))

        def progress(result:ImportSummary)->None:
            nonlocal summary
            summary=result
            self.db.save_account_import(import_id,'running',asdict(result))

        def assigned(aid,ready):
            expected_pool.add(aid)
            self.db.save_account_import_item(import_id,aid,ready)
            self.db.conn.execute('INSERT OR REPLACE INTO imported_owner_access VALUES (?,?,?)',(aid,target,self.app.cfg.owner_id))

        state='interrupted'
        try:
            if self_service:
                expected_pool=set(self.db.account_ids(uid))
                with self.db.account_action(None,user_id=uid):
                    if not self.can_import(uid,target):raise ValueError('Доступ к импорту закончился.')
                    if self.db.account_check_pending():raise ValueError('Дождись окончания общей проверки аккаунтов.')
                    folder_id=await self.member_folder(uid,expected_pool)
                    if not self.can_import(uid,target):raise ValueError('Доступ к импорту закончился.')
                    self.db.bind_member_import_folder(uid,folder_id)
                accounts=[{'cookies':entry['cookies'],'folderId':folder_id} for entry in accounts]
            summary=await import_and_assign(self.app.api,self.db,target,accounts,on_progress=progress,
                                             on_assignment=assigned,
                                             should_stop=lambda:self.stop_requested,self_service=self_service,allowed=allowed)
            state='finished'
            self.db.log(uid,'account_file_import',f'{target}:{summary.assigned}/{summary.total}')
        except ValueError as exc:
            summary.stopped=str(exc)
            state='finished'
        except ApiError as exc:
            summary.stopped=safe_api_error(exc.code)
            state='finished'
        except Exception as exc:
            summary.stopped='ошибка обработки'
            state='finished'
            log.warning('Account import stopped: %s',type(exc).__name__)
        finally:
            self.db.save_account_import(import_id,state,asdict(summary))
            self.running_id=None
        card = self.app.live_cards.cards.get(uid)
        if card and card.kind=='import' and card.task_id==import_id:
            await self.app.live_cards.refresh(uid,card)
