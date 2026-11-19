"""Proxima Soft full interface: independent per-user jobs, branded reports,
Telegram Rich Messages, media uploads and owner-only shared workspace actions.

The provider workspace is shared; accounts, proxies, materials, post drafts and
settings are NEVER exposed for member mutations across user boundaries.
"""
from __future__ import annotations
import asyncio
from base64 import b64decode
import csv
from datetime import datetime
from hashlib import blake2s
import time
import io
import json
import re
from secrets import token_hex
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo
from .account_checks import PoolChecks, read_workspace_account_check, start_workspace_account_check, stop_workspace_account_check
from .account_console import AccountConsoleMixin, public_account_username
from .account_import_ui import AccountImportUI
from .accounts import ACCOUNT_STATUS, check_status, session_state, valid_task_folder
from .bot import BotApp, PUBLICATION_REPORT_KINDS, safe_api_error
from .admin_panel import AdminPanelMixin, csv_safe
from .catalog import (
    MODES, OWNER_ONLY, MODE_KIND, limited_lines, parse_targets, parse_options, parse_feedwarm,
    validate_like_links, build_payload, validate_task_options, validate_feedwarm, is_tiktok_url,
    PHOTO_LABELS, photo_choices, task_photo_fields,
    MAX_FETCH_COUNT, MAX_COLLECTED_REPLY_TARGETS, order_settings, parse_fetch_count,
    collected_reply_targets, is_collection_reply, valid_collection_targets, validate_reply_links,
    MAX_REPLY_INPUT_LINES, MAX_REPLY_LINK_LENGTH, TASK_LIMITS,
    validate_username_targets, video_search_terms, edit_video_search_terms, video_search_settings,
)
from .api import ACCOUNT_FOLDER_ID, ApiError, Telegram, TelegramError, scan_active_tasks
from .db import OPEN_STATES, TERMINAL_STATES, now, settlement_deadline, task_execution_limit_seconds, task_updates_live
from .export_parts import EXPORT_PART_BYTES, ExportParts
from .live_cards import LiveCards, POLL_SECONDS, workspace_task_updates_live
from .operations import declared_operations, load_spec, operation_channel, operation_plan, valid_body
from .presentation import (MODE_SYMBOLS, error_breakdown, h, keyboard, minutes_label, number, pagination, report_view,
                           result_status, state_label, state_symbol, task_view, task_rich, table_html, rich_table,
                           REPLY_CHECK_NOTE, task_has_replies, empty_video_search)
from .worker import snapshot_state
from .proxy_console import ProxyConsoleMixin
from .order_templates import (LocalTemplatesMixin, MEDIA_TEMPLATE_MODES, TEMPLATE_LABELS, task_order_mode,
                              template_input_steps, template_values, template_mode)
from .order_form import (order_fields, field_value, field_text, parse_field, set_field,
                         complete_order_form, form_rows, edit_order_form, VOLUME_KEYS, field_presets, parse_volume_message,
                         publication_blocks, reply_quantity, volume_example)
from .media_order_form import MediaOrderFormMixin
from .rich_ui import screen, button_row, disclosure, home_rich

SAFE_ID=re.compile(r'^[a-zA-Z0-9_-]{1,128}$')
SERVICE_NAME=re.compile(r'tokora(?:\.online)?|токор[аыуое]',re.IGNORECASE)
# The live entries endpoint accepts 1–100 even though OpenAPI declares 200.
EXPORT_ENTRIES_PAGE_SIZE=100
NATIVE_LINK_EXPORT_KINDS={'publish','keyword_reply','feedwarm'}
POST_STATUS_LABELS={
    'queued':'В очереди','uploading':'Загрузка медиа','submitting':'Отправка поста',
    'pending':'Обработка','moderation':'На модерации','published':'Опубликовано',
    'rejected':'Отклонено','failed':'Ошибка','unknown':'Результат неизвестен',
    'cancelled':'Отменено','unconfirmed':'Не подтверждено','not_published':'Публикация не состоялась',
}

# Explicit UI operations. Each path exists in bundled OpenAPI; placeholders are
# filled only from server-validated IDs, never arbitrary URL input.
ADMIN_ACTIONS={
    'posts_pause':('POST','/v1/posts/queue/pause',{}),
    'posts_resume':('POST','/v1/posts/queue/resume',{}),
    'posts_refresh':('POST','/v1/posts/queue/refresh',{}),
    'posts_cancel_queued':('POST','/v1/posts/queue/cancel-queued',{}),
    'posts_clear_history':('POST','/v1/posts/queue/clear-history',{}),
}
# Destructive actions require a two-step confirmation rather than trusting a
# stale callback or freeform user text.
DESTRUCTIVE={'accounts_check_stop','posts_cancel_queued','posts_clear_history'}
PROMPTS={
 'name':'Введи короткое название, чтобы найти заказ в списке. До 60 символов.',
 'videos':'Отправь ссылки TikTok или ID видео сообщением либо UTF-8 TXT. Одна ссылка или ID на строку, до 100 строк. Подойдут обычные и короткие ссылки.',
 'fetch_count':'Сколько комментариев собрать с каждого видео? Выбери 100 или 1000. По умолчанию — 100.',
 'fetch_keywords':'По каким ключевым словам собирать комментарии? Отправь слова или фразы по одной на строку. Можно пропустить — собрать без фильтра.',
 'fetch_replies':'Собирать также ответы под комментариями? По умолчанию — без ответов.',
 'comments':'Отправь тексты комментариев сообщением или TXT. Один текст на строку, до 100 строк.',
 'replies':'Отправь тексты ответов сообщением или TXT. Один текст на строку, до 100 строк.',
 'reply_links':'Пришли ссылки на комментарии TikTok или пары «ID видео:ID комментария». До 300 непустых строк, каждая до 2048 символов. Подойдёт сообщение или UTF-8 TXT.',
 'like_links':'Нужна именно ссылка на комментарий, а не на видео. Пришли ссылки на комментарии TikTok или пары «ID видео:ID комментария». До 300 строк, сообщением или UTF-8 TXT.',
 'likes':'Сколько лайков необходимо на комментарий? Целое число 1–100.',
 'keywords':'Пришли поисковые ключевые фразы, по одной в строке (до 100).',
 'warm_json':'Напиши тему сбора. Например, <code>обзор телефона</code>. Можно указать до 20 тем, по одной на строку. ',
 'options':'Выбери объём и действия для этого заказа. Нажми на параметр, чтобы изменить его.',
 'when':'Поставь задачу в очередь сейчас или укажи дату и время в формате <code>ДД.ММ.ГГГГ ЧЧ:ММ</code> ({zone}).',
}

STEP_LABELS={
    'name':'Название','videos':'Ссылки на видео','comments':'Тексты комментариев','replies':'Тексты ответов',
    'fetch_count':'Лимит сбора','fetch_replies':'Сбор ответов','fetch_keywords':'Ключевые слова сбора',
    'reply_links':'Ссылки для ответов','like_links':'Ссылки для лайков','likes':'Количество лайков',
    'keywords':'Ключевые фразы','warm_json':'Параметры поиска','options':'Объём и действия','when':'Время запуска',
    'review':'Проверка заказа',
}

ORDER_LIMIT_LABELS={
    'commentsPerVideo':'Комментариев на видео',
    'repliesPerVideo':'Ответов на видео',
    'repliesPerComment':'Ответов на комментарий',
}
FETCH_OPTION_KEYS={'fetch_count':{'fetchCount','collectAll'},'fetch_replies':{'includeReplies'}}
REVIEW_EDIT_STEPS={'edit_options':'options','edit_when':'when','edit_search':'warm_json',
                   'edit_name':'name','edit_comments':'comments','edit_replies':'replies',
                   'edit_likes':'likes','edit_keywords':'keywords'}


def order_settings_rows(mode:str,settings:dict)->list[tuple[str,str|int]]:
    if mode=='fetch':
        count=settings.get('fetchCount')
        if settings.get('collectAll'):
            limit='Все доступные'
        elif count:
            limit=f'До {number(count)}'
            if settings.get('collectAll') is None:
                limit+=' при выключенном сборе всех'
        else:
            limit='По настройкам кабинета'
        replies={True:'Собирать',False:'Пропустить'}.get(settings.get('includeReplies'),'По настройкам кабинета')
        return [('Лимит комментариев',limit),('Ответы под комментариями',replies)]
    return [(label,settings[key]) for key,label in ORDER_LIMIT_LABELS.items() if settings.get(key) is not None]


def pth_id(value:str)->str:
    if not SAFE_ID.fullmatch(str(value)):raise ValueError('Invalid ID')
    return quote(str(value),safe='')


def post_plan_expired(plan:dict)->bool:
    expires=plan.get('expiresAt')
    if type(expires) is not int or expires<=0:
        return False
    return (expires/1000 if expires>=1_000_000_000_000 else expires)<=time.time()


def post_plan_ready(plan:dict)->bool:
    return (plan.get('state')=='ready' and not (plan.get('queued') or plan.get('error'))
            and type(plan.get('postsTotal')) is int and plan['postsTotal']>0 and not post_plan_expired(plan))


def select_items(data:dict,keys=('items','accounts','proxies','schedules','presets','drafts','posts'))->list[dict]:
    for k in keys:
        if isinstance(data.get(k),list):return data[k]
    return []


def preset_items(data:dict)->list[dict]:
    return [*select_items(data,('items',)),*({**item,'builtin':True} for item in select_items(data,('builtins',)))]


def owner_list_page(items:list, key:str, page:int, size:int):
    page = min(page, max(0, (len(items)-1)//size))
    start = page*size
    nav = pagination(f'owner_show:{key}:', page, start+size < len(items))
    return items[start:start+size], page, nav


def public_comment_query(name:str,value:str)->bool:
    if name not in ('comment_id','reply_id','cid','parent_comment_id'):
        return False
    if re.fullmatch(r'[0-9]{1,30}',value):
        return True
    if name!='cid' or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}={0,2}',value):
        return False
    try:
        decoded=b64decode(value+'='*(-len(value)%4),altchars=b'-_',validate=True)
    except ValueError:
        return False
    return bool(re.fullmatch(rb'[0-9]{10,30}',decoded))


def public_result_link(value)->str:
    if not isinstance(value,str) or len(value)>1000 or not is_tiktok_url(value):
        return ''
    url=urlsplit(value)
    if url.scheme!='https':return ''
    query=[(name,item) for name,item in parse_qsl(url.query) if public_comment_query(name,item)]
    return urlunsplit((url.scheme,url.netloc,url.path,urlencode(query),''))


def public_result_id(value)->str:
    return value if isinstance(value,str) and re.fullmatch(r'[0-9]{1,30}',value) else ''


def public_video_link(value)->str:
    """Open an original video even when the API supplies only its numeric ID."""
    if not isinstance(value,str):return ''
    pair=re.fullmatch(r'([0-9]{1,30})\s*[:\s]\s*[0-9]{1,30}',value)
    video_id=public_result_id(value) or (pair.group(1) if pair else '')
    if video_id:return 'https://www.tiktok.com/share/video/'+video_id
    link=public_result_link(value)
    if not link:return ''
    match=re.search(r'/video/([0-9]{1,30})(?:/|$)',urlsplit(link).path)
    if match:return 'https://www.tiktok.com/share/video/'+match.group(1)
    return link


def order_video_links(*sources:dict)->list[str]:
    links=[]
    for source in sources:
        for item in source.get('awemes') or []:
            if isinstance(item,dict):
                link=public_video_link(item.get('url')) or public_video_link(item.get('awemeId'))
                if link:links.append(link)
        for key in ('awemeIds','replyLinks','likeLinks'):
            values=source.get(key)
            if isinstance(values,list):
                links.extend(link for value in values if (link:=public_video_link(value)))
    return list(dict.fromkeys(links))


def result_username(item:dict)->str:
    value=item.get('text')
    if item.get('kind')=='username' and isinstance(value,str) and re.fullmatch(r'@?[A-Za-z0-9_]{1,32}',value):
        return '@'+value.lstrip('@')
    return ''


def export_csv_row(item:dict,fields:tuple[str,...])->dict:
    kinds = {'comment':'Комментарий','reply':'Ответ','video':'Видео','like':'Лайк','username':'Пользователь'}
    values = {
        'kind': kinds.get(str(item.get('kind')), 'Запись'),
        'awemeId': public_result_id(item.get('awemeId')),
        'cid': public_result_id(item.get('cid')),
        'text': str(item.get('text') or '')[:1800],
        'outcome': result_status(item.get('outcome')),
        'url': public_result_link(item.get('url')),
    }
    if 'username' in fields:
        values['username'] = result_username(item).lstrip('@')
    for key in ('status','confirmed','goal','failed','mentions'):
        if key in fields and type(item.get(key)) is int:
            values[key] = item[key]
    timestamp = item.get('at')
    if isinstance(timestamp,str):
        try:
            values['at'] = datetime.fromisoformat(timestamp).isoformat()
        except ValueError:
            pass
    return {key:csv_safe(value) for key,value in values.items()}


class ProximaApp(MediaOrderFormMixin, LocalTemplatesMixin, AccountConsoleMixin, ProxyConsoleMixin, AdminPanelMixin, BotApp):
    def __init__(self, config, db, tg, api):
        super().__init__(config, db, tg, api)
        self.db.owner_id = config.owner_id
        for saved in self.db.conn.execute('SELECT user_id,settings_json FROM user_preferences').fetchall():
            prefs=json.loads(saved['settings_json'])
            if 'last_videos' in prefs:
                prefs.pop('last_videos')
                self.db.set_preferences(saved['user_id'],prefs)
        self.live_cards=LiveCards(self)
        self.account_imports=AccountImportUI(self)
        self.pool_checks=PoolChecks(self)

    def pack_owner_route(self,callback:str)->str:
        route='route:'+blake2s(callback.encode(),digest_size=12).hexdigest()
        self.db.save_owner_route(self.cfg.owner_id,route,callback)
        return route

    def owner_keyboard(self,*rows)->dict:
        return keyboard(*rows,pack=self.pack_owner_route)

    @property
    def schedule_zone(self):
        return ZoneInfo(self.cfg.timezone_name)

    def restricted_modes(self) -> frozenset[str]:
        modes = set(self.cfg.restricted_modes) | OWNER_ONLY
        for mode, restricted in self.db.order_mode_access().items():
            if mode in MODES and mode not in OWNER_ONLY:
                if restricted:
                    modes.add(mode)
                else:
                    modes.discard(mode)
        return frozenset(modes)

    def set_mode_restricted(self, mode: str, restricted: bool) -> None:
        if mode not in MODES or mode in OWNER_ONLY:
            raise ValueError('Доступ к этому виду заказа изменить нельзя.')
        self.db.set_order_mode_access(self.cfg.owner_id, mode, restricted)

    def mode_restricted(self,uid:int,mode:str)->bool:
        if mode == 'source_reply':
            mode = 'reply_links'
        return not self.owner(uid) and (mode in MEDIA_TEMPLATE_MODES or mode in self.restricted_modes())

    def allow_photos(self,uid:int)->bool:
        return self.owner(uid) or (hasattr(self.db,'can_use_photos') and self.db.can_use_photos(uid))

    async def show_home(self,uid:int)->None:
        is_owner=self.owner(uid)
        active=self.db.is_active(uid,self.cfg.owner_id)
        metrics=await self.get_main_menu_data(uid)
        text=self.format_main_menu(metrics)
        quick=[btn for btn in (
            ('⌕ Собрать комментарии','new:fetch',None),('♡ Лайки комментариев','new:commentlike',None),
            ('▷ Сбор видео','new:feedwarm',None),('▷ Публикации · Видео / фото','posts',None),
        ) if (is_owner if btn[1]=='posts' else not self.mode_restricted(uid,btn[1][4:]))]
        pool_switch = getattr(self, 'can_switch_cabinet', None)
        pool_row = [[('▦ Рабочие пулы', 'ws:menu', None)]] if pool_switch and pool_switch(uid) else []
        buttons=keyboard(
            [('＋ Новая задача','create','success')],
            [('▣ '+('Все задачи' if is_owner else 'Мои задачи'),'tasks',None),('▧ Мои шаблоны','templates',None)],
            *(tuple(quick[i:i+2]) for i in range(0,len(quick),2)),
            [('◇ '+('Аккаунты' if is_owner else 'Мои аккаунты'),'accounts',None),('◈ Прокси','proxies',None)],
            [('▧ Инструменты','tools',None),('◇ Подписка','access',None if active else 'primary')],
            *pool_row,
            *(([('▣ Панель владельца','admin',None)],) if is_owner else ())
        )
        await self.tg.rich(uid,home_rich(metrics),text,buttons)

    async def tools_menu(self,uid:int)->None:
        rich,plain=screen('▧ Инструменты',subtitle='Результаты, расписание и готовые тексты')
        await self.tg.rich(uid,rich,plain,keyboard(
            [('▥ Статистика','stats','primary'),('◷ Расписание','schedules',None)],
            [('▧ Мои шаблоны','templates',None)],
            [('↶ Обзор','home',None)]))

    async def create(self,uid:int)->None:
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        if not self.has_order_pool(uid):
            return await self.say(uid,f'<b>◇ Нет назначенных аккаунтов</b>\n\nПередай администратору свой ID.\n<code>{uid}</code>',
                                  keyboard([('↶ Обзор','home',None)]))
        draft=self.db.draft(uid)
        can_resume=bool(draft and (draft['mode'] in MODES or draft['mode'] in ('task_copy','source_reply','media_order')))
        rows=[]
        if can_resume:
            rows.append([('↪ Продолжить черновик','wiz_continue','primary')])
        rows.extend([(f'{MODE_SYMBOLS[mode]} {spec[0]}','new:'+mode,'primary' if mode=='publish' and not can_resume else None)]
                    for mode,spec in MODES.items() if not self.mode_restricted(uid,mode))
        if self.owner(uid):
            rows.append([('▷ Видео','upload:video',None),('▧ Фотоальбом','owner_edit:album',None)])
        rows.append([('▧ Мои шаблоны','templates',None)])
        rows.append([('↶ Обзор','home',None)])
        rich,plain=screen('＋ Новый заказ',subtitle='Что нужно сделать?',stage=1)
        def visible_row(items):
            return tuple(item for item in items if not self.mode_restricted(uid,item[1].split(':',1)[1]))
        pub_rows=[r for r in (
            visible_row([('Комментарии и ответы','new:publish','primary' if not can_resume else None)]),
            visible_row([('По ссылкам','new:reply_links',None),('По ключевым словам','new:keyword_reply',None)]),
        ) if r]
        if pub_rows:
            rich+='<h3>Комментарии и ответы</h3>'+''.join(button_row(*r) for r in pub_rows)
        collect_rows=[r for r in (
            visible_row([('Комментарии','new:fetch',None),('Видео','new:feedwarm',None)]),
            visible_row([('Лайки комментариев','new:commentlike',None),('Юзернеймы','new:usernames',None)]),
        ) if r]
        if collect_rows:
            rich+='<h3>Сбор и реакции</h3>'+''.join(button_row(*r) for r in collect_rows)
        if self.owner(uid):
            rich+=disclosure('Публикации и обслуживание',button_row(('Загрузить видео','upload:video',None),('Фотоальбом','owner_edit:album',None))+
                             button_row(('Проверка аккаунтов','new:check',None)))
        rich+='<footer>Добавь материалы, выбери объём и проверь заказ перед отправкой в очередь.</footer>'
        plain+='\n\nВыбери вид заказа. Затем добавь материалы, укажи объём и проверь результат.'
        full=keyboard(*rows)
        compact=keyboard(*([rows[0]] if can_resume else []),[('▧ Мои шаблоны','templates',None),('↶ Обзор','home',None)])
        if isinstance(self.tg,Telegram):
            await self.tg.rich(uid,rich,plain,compact,fallback_keyboard=full)
        else:await self.tg.rich(uid,rich,plain,full)

    async def begin(self,uid:int,mode:str,*,auto_name:bool=False)->None:
        if mode not in MODES or self.mode_restricted(uid,mode):return
        if mode=='check':return await self.owner_show(uid,'accounts_check')
        if not self.db.is_active(uid,self.cfg.owner_id) or not self.has_order_pool(uid):return await self.create(uid)
        if auto_name:
            title=MODES[mode][0]+' · '+datetime.now(self.schedule_zone).strftime('%d.%m %H:%M')
            step=self.wizard_steps(mode)[1]
            self.save_wizard_step(uid,mode,step,{'name':title,'auto_name':True})
        else:
            self.save_wizard_step(uid,mode,'name',{})
        await self.show_wizard(uid)

    def matching_order_templates(self,uid:int,mode:str)->list[dict]:
        result=[]
        for record in self.db.templates(uid):
            item=self.db.template(uid,record['template_id'])
            if item and (template_mode(item)==mode or (mode in ('reply_links','source_reply') and template_values(item).get('replies'))):result.append(item)
        return result

    async def order_template_picker(self,uid:int,page:int=0)->None:
        draft=self.db.draft(uid)
        if not draft or draft['mode'] not in MODES:return
        items=self.matching_order_templates(uid,draft['mode'])
        page=max(0,min(page,(len(items)-1)//10))
        token=self.wizard_token(draft)
        rows=[[(f'▧ {item["name"][:48]}',f'wiz:tpl_{item["template_id"]}:{token}',None)]
              for item in items[page*10:(page+1)*10]]
        nav=[]
        if page:nav.append(('← Ранее',f'wiz:tplpage_{page-1}:{token}',None))
        if (page+1)*10<len(items):nav.append(('→ Далее',f'wiz:tplpage_{page+1}:{token}',None))
        rows.extend([nav,[('↶ К заказу',f'wiz:template_back:{token}',None)]])
        hint=('Выбери шаблон. Уже введённые ссылки сохранятся. Объём подтвердишь отдельно.' if items
              else 'Подходящих шаблонов пока нет. Можно продолжить ввод вручную.')
        rich,plain=screen('▧ Шаблон для заказа',subtitle=MODES[draft['mode']][0],caption=hint)
        await self.tg.rich(uid,rich,plain,keyboard(*rows))

    async def apply_order_template(self,uid:int,tid:str,draft:dict)->None:
        item=self.db.template(uid,tid)
        if not item or (template_mode(item)!=draft['mode'] and not (draft['mode'] in ('reply_links','source_reply') and template_values(item).get('replies'))):
            return await self.say(uid,'Шаблон недоступен для этого вида заказа.',keyboard(
                [('↶ К заказу','wiz:template_back:'+self.wizard_token(draft),None)]))
        current=draft['data']
        values=template_values(item)
        asked=set(values.get('ask_fields') or [])
        for field in MODES[draft['mode']][1:]:
            if field=='options':continue
            # A template supplies materials; targets already entered for this order win.
            if field in ('videos','reply_links','like_links') or field in asked or values.get(field) in (None,[],{},''):
                if field in current and current[field] not in (None,[],{},'') and field not in values.get('disabled_fields',[]):
                    if field=='warm_json':
                        values[field]={**(values.get(field) or {}),**current[field]}
                    else:
                        values[field]=current[field]
                    asked.discard(field)
        values['options']={**(values.get('options') or {}),**(current.get('options') or {})}
        for field in ('name','auto_name','fire_at'):
            if field in current:values[field]=current[field]
        values['ask_fields']=list(asked)
        remaining=template_input_steps(draft['mode'],values)
        values.pop('ask_fields',None)
        values['template_steps']=remaining
        self.save_wizard_step(uid,draft['mode'],remaining[0],values)
        await self.show_wizard(uid)

    def has_order_pool(self,uid:int)->bool:
        return self.owner(uid) or bool(self.db.account_ids(uid))

    async def _owner_review_scope(self,uid:int)->tuple[list[str],str,str|None,str]:
        """Resolve owner's preferred order account pool (all workspace, none, folder, or assigned)."""
        pref = self.db.preferences(uid).get('order_account_folder')
        mode = 'all' if pref in (None,'','all') else pref
        if mode=='assigned':
            ids = self.db.account_ids(uid)
            if not ids:
                return [],'Назначенный пул',None,'Назначенный пул пуст. Выбери общий пул кабинета или папку в «Папках кабинета».'
            return ids,'Назначенный пул',None,''
        try:
            if mode=='all':
                accounts = {aid: account async for aid,account in self.workspace_accounts()}
                if not accounts:
                    local_ids = self.db.account_ids(uid)
                    if local_ids:
                        return local_ids,'Назначенный пул',None,''
            elif mode=='none' or ACCOUNT_FOLDER_ID.fullmatch(mode):
                accounts = await self.api.accounts_in_folder(mode)
            else:
                return [],'Папки кабинета',None,'Выбранная папка некорректна. Выбери пул в списке папок кабинета.'
        except ApiError as exc:
            return [],'Папки кабинета',None,'Не удалось загрузить пул аккаунтов. '+safe_api_error(exc.code)
        except ValueError:
            return [],'Папки кабинета',None,'Выбранная папка некорректна. Выбери пул в списке папок кабинета.'
        ids = sorted(accounts)
        if not ids:
            empty_msg = ('В кабинете нет доступных аккаунтов. Проверь список аккаунтов или папки кабинета.' if mode=='all'
                         else 'Аккаунтов без папки сейчас нет. Выбери другой пул в «Папках кабинета».' if mode=='none'
                         else 'Папка недоступна или пуста. Выбери другой пул в «Папках кабинета».')
            return [],'Папки кабинета',None,empty_msg
        if not any(session_state(account)=='active' for account in accounts.values()):
            inactive_msg = ('В кабинете нет аккаунтов с активной сессией. Проверь аккаунты или выбери другой пул.' if mode=='all'
                            else 'Среди аккаунтов без папки нет активных сессий. Проверь аккаунты или выбери другой пул.' if mode=='none'
                            else 'В папке нет аккаунтов с активной сессией. Выбери другую папку или проверь аккаунты.')
            return [],'Папки кабинета',None,inactive_msg
        review_folder = '' if mode=='all' else mode
        label = ('Все аккаунты кабинета' if mode=='all'
                 else 'Аккаунты без папки' if mode=='none'
                 else f'Папка кабинета · {mode}')
        return ids,label,review_folder,''

    async def review_order_accounts(self,uid:int,draft:dict)->tuple[list[str],str]|None:
        review_folder: str | None = None
        if self.owner(uid):
            ids,label,review_folder,issue = await self._owner_review_scope(uid)
            if issue:
                pop_ids = draft['data'].pop('review_account_ids',None)
                pop_folder = draft['data'].pop('review_account_folder',None)
                if pop_ids is not None or pop_folder is not None:
                    self.db.save_draft(uid,draft['mode'],'review',draft['data'])
                await self.say(uid,issue,keyboard([('▤ Папки кабинета','ac:folders:0',None)],
                                                  [('↻ Повторить','wiz_continue','primary')]))
                return None
        else:
            ids = self.db.account_ids(uid)
            label = 'Аккаунты'
        if not ids:
            await self.say(uid,'Свободных аккаунтов с активной сессией сейчас нет. Открой список аккаунтов или обнови проверку заказа.',
                           keyboard([('↻ Повторить','wiz_continue','primary')],[('↶ Аккаунты','accounts',None)]))
            return None
        changed = (draft['data'].get('review_account_ids') != ids
                   or (self.owner(uid) and draft['data'].get('review_account_folder') != review_folder)
                   or (not self.owner(uid) and 'review_account_folder' in draft['data']))
        if changed:
            draft['data'].update(review_account_ids=ids,wizard_token=token_hex(6))
            if self.owner(uid):
                draft['data']['review_account_folder'] = review_folder
            else:
                draft['data'].pop('review_account_folder',None)
            self.db.save_draft(uid,draft['mode'],'review',draft['data'])
        return ids,label

    def confirmed_order_accounts(self,uid:int,draft:dict)->list[str]:
        ids = draft['data'].get('review_account_ids')
        if not ids:
            raise ValueError('Открой проверку заказа, чтобы обновить состав аккаунтов.')
        if not self.owner(uid) and ids!=self.db.account_ids(uid):
            raise ValueError('Назначенные аккаунты изменились. Проверь заказ и подтверди его снова.')
        return ids

    def confirmed_order_folder(self,uid:int,draft:dict)->str|None:
        if not self.owner(uid):
            return None
        folder = draft['data'].get('review_account_folder')
        return folder if valid_task_folder(folder,allow_workspace=True) else None

    @staticmethod
    def wizard_steps(mode:str)->tuple[str,...]:
        return ('name',*MODES[mode][1:],'when','review')

    def save_wizard_step(self,uid:int,mode:str,step:str,data:dict)->None:
        self.db.save_draft(uid,mode,step,{**data,'wizard_token':token_hex(6)})

    def wizard_token(self,draft:dict)->str:
        data=draft['data']
        if not data.get('wizard_token'):
            data['wizard_token']=token_hex(6)
            self.db.save_draft(draft['user_id'],draft['mode'],draft['step'],data)
        return data['wizard_token']

    @staticmethod
    def wizard_optional(mode:str,step:str,data:dict)->bool:
        if step in ('options','when','fetch_count','fetch_replies','fetch_keywords'):
            return True
        if mode!='publish':
            return False
        comments,replies=publication_blocks(mode,data)
        if step=='comments':
            if 'replies' in data.get('disabled_fields',[]):return False
            return not data.get('review_edit') or (replies and reply_quantity(data)>0)
        return step=='replies' and comments

    async def wizard_field(self,uid:int,mode:str,step:str,*,error:str='')->None:
        draft=self.db.draft(uid)
        if not draft:return
        token=self.wizard_token(draft)
        data=draft['data']
        if step=='options':return await self.order_form_field(uid,mode,data,token,error=error)
        order=self.wizard_steps(mode)
        progress=('Материалы · 1 из 3 · '+STEP_LABELS[step] if step not in ('name','when') else STEP_LABELS[step])
        buttons=[]
        if step not in ('name','when'):
            count=len(self.matching_order_templates(uid,mode))
            if count:buttons.append([(f'▧ Выбрать шаблон · {count}','wiz:templates:'+token,'primary')])
        if step=='name':buttons.append([('✓ Название автоматически','wiz:auto_name:'+token,'primary')])
        if step=='fetch_count':
            selected=data.get('fetch_count',100)
            buttons.append([(str(count),f'wiz:fetch_{count}:{token}','primary' if count==selected else None)
                            for count in (100,1000)])
        if step=='fetch_replies':
            with_replies=data.get('fetch_replies',False)
            buttons.append([('Без ответов','wiz:fetch_without_replies:'+token,None if with_replies else 'primary'),
                            ('С ответами','wiz:fetch_with_replies:'+token,'primary' if with_replies else None)])
        if step=='likes':
            selected=data.get('likes',15)
            buttons.append([(str(n),'wiz:likes_'+str(n)+':'+token,'primary' if n==selected else None) for n in (5,10,15,20)])
        key='fire_at' if step=='when' else step
        keep=data.get(key)
        if step not in ('fetch_count','fetch_replies') and keep and (step!='when' or keep>time.time()+60):
            buttons.append([('✓ Оставить введённое','wiz:keep:'+token,'primary')])
        if self.wizard_optional(mode,step,data) and step not in ('fetch_count','fetch_replies'):
            label={'options':'→ Без доп. настроек','when':'✓ По очереди',
                   'fetch_count':'✓ Лимит 100','fetch_replies':'Только комментарии'}.get(step,'→ Пропустить')
            buttons.append([(label,'wiz:skip:'+token,'success' if step=='when' else None)])
        buttons.append([('↶ К видам задач' if step=='name' else '↶ Назад','wiz:back:'+token,None),
                        ('× Отменить','wiz:cancel:'+token,None)])
        prompt=PROMPTS[step]
        if step=='when':prompt=prompt.format(zone=h(self.cfg.timezone_name))
        if step=='videos' and mode=='usernames':
            prompt+='\n\nПоиск проверит комментарии и ответы под этими видео на упоминания Telegram-юзернеймов.'
        if step=='videos' and mode=='keyword_reply':
            prompt+='\n\nБот будет искать комментарии по твоим фразам под этими видео.'
        if step=='fetch_replies' and keep is not None:
            prompt+='\n\nРанее выбрано · '+('С ответами' if keep else 'Только комментарии')
        elif keep:
            value=(self.format_date(keep) if step=='when' else f'{number(len(keep))} строк' if isinstance(keep,list)
                   else f'{number(len(keep))} параметров' if isinstance(keep,dict) else
                   'Все доступные' if step=='fetch_count' and keep=='all' else str(keep))
            prompt+='\n\nРанее введено · <code>'+h(value)+'</code>'
        if error:prompt+='\n\n<b>! Проверь ввод</b>\n'+h(error)
        plain=f'<b>{MODE_SYMBOLS[mode]} {h(MODES[mode][0])}</b>\n{h(progress)}\n\n'+prompt
        rich,_=screen(MODE_SYMBOLS[mode]+' '+MODES[mode][0],stage=1 if step not in ('name','when') else None)
        rich+='<h3>'+h(STEP_LABELS[step])+'</h3><p>'+prompt.replace('\n\n','</p><p>').replace('\n','<br>')+'</p>'
        if step in ('comments','replies'):
            note='Это варианты текста. Количество публикаций выберешь на следующем этапе.'
            rich+='<footer>'+note+'</footer>'
            plain+='\n\n'+note
        await self.tg.rich(uid,rich,plain,keyboard(*buttons))

    async def order_form_field(self,uid:int,mode:str,data:dict,token:str,*,error:str='',prefix:str='wiz')->None:
        fields=order_fields(mode,data)
        selected=data.get('form_field')
        if selected=='advanced':selected=None
        field=next((item for item in fields if item.key==selected),None)
        route=lambda verb:f'{prefix}:{verb}:{token}'
        title='↪ Ответы на собранное' if mode=='source_reply' else MODE_SYMBOLS[mode]+' '+MODES[mode][0]
        if field:
            text='<b>'+h(title)+'</b>\n<b>Объём и действия</b>\n\n'
            text+='<b>'+h(field.label)+'</b>\n'+h(field.hint)+f'\nДопустимо · <b>{field.minimum}–{field.maximum}</b>'
            text+='\n\nСейчас · <b>'+h(field_text(field,field_value(field,data)))+'</b>'
            buttons=[(str(n),route('value_'+str(n)),None) for n in field_presets(field)]
            rows=[buttons[i:i+4] for i in range(0,len(buttons),4)]
            rows.append([('↶ К объёму заказа',route('form_back'),None)])
            if error:text+='\n\n<b>Проверь ввод</b>\n'+h(error)
            rich,_=screen(title,subtitle='Своё значение',stage=None if prefix=='lt' else 2)
            rich+='<h3>'+h(field.label)+'</h3><p>Введи целое число · <b>'+str(field.minimum)+'–'+str(field.maximum)+'</b>.</p>'
            rich+='<p>Сейчас · <b>'+h(field_text(field,field_value(field,data)))+'</b></p>'
            if field.hint:rich+='<footer>'+h(field.hint)+'</footer>'
            if error:rich+='<blockquote><b>Проверь ввод</b><br>'+h(error)+'</blockquote>'
            return await self.tg.rich(uid,rich,text,keyboard(*rows))
        rich,plain=screen(title,subtitle='Объём и действия'+(' · Шаблон' if prefix=='lt' else ''),stage=None if prefix=='lt' else 2)
        if mode=='source_reply' and prefix!='lt':
            count=data.get('target_count') or (data.get('options') or {}).get('targetCount') or 100
            rich+='<p>Выбрано комментариев для ответов · <b>'+h(count)+'</b></p>'
            plain+='\nВыбрано комментариев для ответов · '+h(count)
        primary=[f for f in fields if f.key in VOLUME_KEYS]
        secondary=[f for f in fields if f.key not in VOLUME_KEYS]
        def controls(items,*,short:bool=False):
            html=''
            for item in items:
                value=field_text(item,field_value(item,data))
                label=({'commentsPerVideo':'На каждое видео','repliesPerComment':'На каждый комментарий',
                        'likesPerComment':'Лайков каждому','likesPerReply':'Лайков каждому'}.get(item.key,item.label) if short else item.label)
                html+='<p>'+h(label)+' · <b>'+h(value)+'</b></p>'
                choices=[]
                if not item.boolean:
                    for n in field_presets(item):
                        choices.append((str(n),route('pick_'+item.key+'_'+str(n)),'primary' if field_value(item,data)==n else None))
                choices.append(('Переключить' if item.boolean else 'Своё',route('form_'+item.key),'link'))
                html+=button_row(*choices)
            return html
        if mode=='publish':
            main=[f for f in primary if f.key in ('commentsPerVideo','likesPerComment')]
            replies=[f for f in primary if f.key in ('repliesPerComment','likesPerReply')]
            if main:rich+='<h3>Основные комментарии</h3>'+controls(main,short=True)
            if replies:rich+='<h3>Ответы на комментарии</h3>'+controls(replies,short=True)
        else:rich+=controls(primary)
        if mode=='commentlike':
            rich+='<p>Лайков на каждый комментарий · <b>'+h(data.get('likes','—'))+'</b></p>'
            plain+='\nЛайков на комментарий · '+h(data.get('likes','—'))
        if mode=='fetch':
            for label,value in order_settings_rows(mode,order_settings(mode,data)):
                rich+='<p>'+h(label)+' · <b>'+h(value)+'</b></p>'
                plain+='\n'+h(label)+' · '+h(value)
        for item in primary:plain+='\n<b>'+h(item.label)+'</b> · '+h(field_text(item,field_value(item,data)))
        summary=' · '.join(item.label+': '+field_text(item,field_value(item,data)) for item in secondary)
        if secondary:
            overview='Параметры · стандартные и дополнительные'
            rich+='<details'+(' open' if data.get('form_more') else '')+'><summary>'+h(overview)+'</summary>'+controls(secondary)+'</details>'
            plain+='\n\n'+h(summary)
        note='Выбери значения кнопками или отправь одним сообщением: '+volume_example(mode,data)+' — по одной строке.' if fields else 'Проверь и подтверди показанный объём.'
        if mode=='publish' and publication_blocks(mode,data)[0]:
            quantities={f.key:field_value(f,data) for f in fields}
            reply_total=quantities.get('commentsPerVideo',50)*quantities.get('repliesPerComment',0)
            plan='На каждое видео: '+str(quantities.get('commentsPerVideo',50))+' основных · '+('до '+str(reply_total)+' ответов.' if reply_total else 'без ответов.')
            rich+='<footer>'+h(plan)+'</footer>'
            plain+='\n\n'+h(plan)
        elif mode=='source_reply' and prefix!='lt':
            reply_field=next((f for f in fields if f.key=='repliesPerComment'),None)
            if reply_field:
                plan='Ответов по плану · '+str(int(count)*field_value(reply_field,data))
                rich+='<footer>'+h(plan)+'</footer>'
                plain+='\n\n'+h(plan)
        help_body='<p>'+h(note)+'</p>'
        plain+='\n\n'+h(note)
        comments,replies=publication_blocks(mode,data)
        if replies:
            reply_note={
                'source_reply':'Ответы будут опубликованы под выбранными собранными комментариями.',
                'reply_links':'Ответы будут опубликованы под комментариями по твоим ссылкам.',
                'keyword_reply':'Ответы будут опубликованы под найденными по фразам комментариями.',
            }.get(mode,'Ответы публикуются под основными комментариями.')
            reply_note+=(' 0 — без ответов.' if comments else ' Для заказа только на ответы нужно хотя бы по одному ответу.')+' Число вариантов текста не задаёт объём.'
            help_body+='<p>'+h(reply_note)+'</p>'
            plain+='\n\n'+h(reply_note)
        if fields:rich+=disclosure('Ввод одним сообщением',help_body)
        rows=[]
        visible=fields
        buttons=[('✎ '+item.label+' · '+field_text(item,field_value(item,data)),route('form_'+item.key),None) for item in visible]
        # Basic Telegram fallback keeps the same editing actions in compact rows.
        fallback_rows=[buttons[i:i+2] for i in range(0,len(buttons),2)]
        rows.append([('✓ '+('Сохранить параметры' if prefix=='lt' else 'Подтвердить объём · к проверке'),route({'wiz':'quick','source':'skip','lt':'keep'}[prefix]),'primary')])
        if prefix=='wiz':rows.append([('◷ Запланировать',route('skip'),None)])
        if secondary:rows.append([('⚙ Параметры',route('form_more'),None)])

        if prefix=='lt':rows.append([('Выбирать при создании заказа',route('skip'),None)])
        rows.append([('↶ Назад',route('back'),None),('Отменить',route('cancel'),None)])
        if error:
            rich+='<p><b>Проверь ввод</b> '+h(error)+'</p>'
            plain+='\n\nПроверь ввод: '+h(error)
        # Rich embeds editors beside their values; fallback needs keyboard editors.
        if isinstance(self.tg,Telegram):
            await self.tg.rich(uid,rich,plain,keyboard(*rows),fallback_keyboard=keyboard(*fallback_rows,*rows))
        else:
            await self.tg.rich(uid,rich,plain,keyboard(*fallback_rows,*rows))

    async def order_form_action(self,uid:int,mode:str,data:dict,verb:str,*,prefix:str='wiz')->bool:
        try:updated=edit_order_form(mode,data,verb)
        except ValueError:return True
        if updated is None:return False
        data=updated
        self.save_wizard_step(uid,mode,'options',data)
        if prefix=='source':await self.source_reply_field(uid)
        else:await self.show_wizard(uid)
        return True

    async def show_wizard(self,uid:int)->None:
        draft=self.db.draft(uid)
        if not draft or draft['mode'] not in MODES:return await self.create(uid)
        if draft['mode']=='check':return await self.begin(uid,'check')
        if draft['step']=='review':return await self.preview(uid)
        await self.wizard_field(uid,draft['mode'],draft['step'])

    async def wizard_action(self,uid:int,action:str)->None:
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        draft=self.db.draft(uid)
        if draft and draft['mode']=='media_order' and action=='wiz_continue':
            return await self.media_order_screen(uid)
        if draft and draft['mode']=='source_reply' and action=='wiz_continue':
            return await self.source_reply_field(uid)
        if draft and draft['mode']=='task_copy':
            data=draft['data']
            token=self.wizard_token(draft)
            if action=='wiz:submit:'+token:
                return await self.queue_existing(uid,data['job'],data['name'],token)
            if action=='wiz:cancel:'+token:
                self.db.clear_draft(uid)
                return await self.create(uid)
            return await self.copy_preview(uid)
        if not draft or draft['mode'] not in MODES:return await self.create(uid)
        if action=='wiz_continue':return await self.show_wizard(uid)
        _,_,suffix=action.partition(':')
        verb,_,token=suffix.partition(':')
        if not token or token!=draft['data'].get('wizard_token'):
            return await self.say(uid,'<b>↻ Кнопка устарела</b>\nОткрой текущий шаг, чтобы продолжить.',
                                  keyboard([('↪ Открыть черновик','wiz_continue','primary')]))
        mode,step,data=draft['mode'],draft['step'],draft['data']
        if verb=='templates':return await self.order_template_picker(uid)
        if verb=='template_back':return await self.show_wizard(uid)
        if verb.startswith('tplpage_') and verb[8:].isdigit():
            return await self.order_template_picker(uid,int(verb[8:]))
        if verb.startswith('tpl_'):
            return await self.apply_order_template(uid,verb[4:],draft)
        if step=='options' and await self.order_form_action(uid,mode,data,verb):return
        if verb=='auto_name' and step=='name':
            title=MODES[mode][0]+' · '+datetime.now(self.schedule_zone).strftime('%d.%m %H:%M')
            return await self.input_draft(uid,title)
        if verb=='form_advanced':return await self.show_wizard(uid)
        if verb=='quick' and step=='options':
            data=complete_order_form(mode,data)
            data.pop('form_field',None)
            data.pop('template_steps',None)
            data.pop('review_edit',None)
            self.save_wizard_step(uid,mode,'review',data)
            return await self.preview(uid)
        if verb in ('fetch_100','fetch_1000') and step=='fetch_count':
            return await self.next_step(uid,mode,step,{**data,'fetch_count':int(verb[6:])})
        if step=='likes' and verb in ('likes_5','likes_10','likes_15','likes_20'):
            return await self.input_draft(uid,verb[6:])
        if step=='fetch_replies' and verb in ('fetch_with_replies','fetch_without_replies'):
            return await self.next_step(uid,mode,step,{**data,'fetch_replies':verb=='fetch_with_replies'})
        if verb=='cancel':
            self.db.clear_draft(uid)
            return await self.create(uid)
        if verb=='back':
            if data.pop('review_edit',False):
                self.save_wizard_step(uid,mode,'review',data)
                return await self.preview(uid)
            steps=self.wizard_steps(mode)
            index=steps.index(step)
            if not index or (index==1 and data.get('auto_name')):return await self.create(uid)
            self.save_wizard_step(uid,mode,steps[index-1],data)
            return await self.show_wizard(uid)
        if verb=='skip':return await self.wizard_skip(uid)
        if verb=='keep' and step!='review':
            key='fire_at' if step=='when' else step
            if data.get(key) and (step!='when' or data[key]>time.time()+60):
                return await self.next_step(uid,mode,step,data)
        if step=='review':
            if verb=='edit_targets':
                target={'reply_links':'reply_links','commentlike':'like_links'}.get(mode,'videos')
                if target in self.wizard_steps(mode):
                    self.save_wizard_step(uid,mode,target,{**data,'review_edit':True})
                    return await self.show_wizard(uid)
            if REVIEW_EDIT_STEPS.get(verb) in self.wizard_steps(mode):
                self.save_wizard_step(uid,mode,REVIEW_EDIT_STEPS[verb],{**data,'review_edit':True})
                return await self.show_wizard(uid)
            if verb=='fetch_edit' and mode=='fetch':
                self.save_wizard_step(uid,mode,'fetch_count',data)
                return await self.show_wizard(uid)
            if verb.startswith('photo_') and self.allow_photos(uid):
                setting=verb[6:]
                if setting=='off' or setting in photo_choices(mode,data):
                    self.save_wizard_step(uid,mode,'review',{**data,'photo_mode':setting})
                    return await self.preview(uid)
            if verb=='submit':return await self.submit(uid)
            if verb=='template':return await self.save_draft_template(uid)

    async def next_step(self,uid:int,mode:str,step:str,data:dict)->None:
        if step=='options':
            data=complete_order_form(mode,data)
            data.pop('form_field',None)
            data.pop('template_steps',None)
            self.save_wizard_step(uid,mode,'when',data)
            return await self.show_wizard(uid)
        if step in FETCH_OPTION_KEYS and data.get('options'):
            data={**data,'options':{key:value for key,value in data['options'].items()
                                    if key not in FETCH_OPTION_KEYS[step]}}
        order=self.wizard_steps(mode)
        if 'template_steps' in data:
            remaining=data['template_steps']
            if remaining and remaining[0]==step:remaining.pop(0)
            next_step=remaining[0] if remaining else 'review'
            if not remaining:data.pop('template_steps')
        else:
            next_step='review' if data.pop('review_edit',False) else order[order.index(step)+1]
        self.save_wizard_step(uid,mode,next_step,data)
        await self.show_wizard(uid)

    async def _input_draft_extended(self,uid:int,text:str)->None:
        d=self.db.draft(uid)
        if not d:return
        if d['mode'] not in MODES:
            return await self.aux_input(uid,d,text)
        mode,step,data=d['mode'],d['step'],d['data']
        if step=='review':return await self.preview(uid)
        try:
            if step=='name':
                if not 1<=len(text.strip())<=60:raise ValueError('Название: 1–60 символов')
                data['name']=text.strip()
            elif step=='videos':
                targets = parse_targets(text)
                data[step] = validate_username_targets(targets) if mode == 'usernames' else targets
            elif step=='fetch_count':
                value=parse_fetch_count(text)
                if value not in (100,1000):raise ValueError('Выбери 100 или 1000.')
                data[step]=value
            elif step=='fetch_keywords':data[step]=limited_lines(text,100,100)
            elif step=='fetch_replies':raise ValueError('Выбери, нужно ли собирать ответы, кнопкой ниже.')
            elif step=='reply_links':
                data[step]=validate_reply_links(text.splitlines())
            elif step=='like_links':
                data[step]=validate_like_links(limited_lines(text,300,700))
            elif step=='keywords':data[step]=limited_lines(text,100,100)
            elif step in ('comments','replies'):
                if mode=='publish' and data.get('form_ready') and not data.get(step):
                    volume_key,likes_key={
                        'comments':('commentsPerVideo','likesPerComment'),
                        'replies':('repliesPerComment','likesPerReply'),
                    }[step]
                    options=data.get('options') or {}
                    if options.get(volume_key)==0:
                        data['options']={key:value for key,value in options.items() if key not in (volume_key,likes_key)}
                data[step]=limited_lines(text,100,700)
                data['disabled_fields']=[field for field in data.get('disabled_fields',[]) if field!=step]
            elif step=='likes':
                x=int(text.strip())
                if not 1<=x<=100:raise ValueError('Укажи 1–100 лайков')
                data[step]=x
            elif step=='warm_json':
                data[step] = edit_video_search_terms(text, data.get(step))
            elif step=='options':
                selected=next((item for item in order_fields(mode,data) if item.key==data.get('form_field')),None)
                if selected:
                    data=set_field(data,selected,parse_field(selected,text))
                    data.pop('form_field',None)
                    self.save_wizard_step(uid,mode,step,data)
                    return await self.show_wizard(uid)
                if not text.lstrip().startswith('{'):
                    data=parse_volume_message(mode,data,text)
                    self.save_wizard_step(uid,mode,step,data)
                    return await self.show_wizard(uid)
                raise ValueError('Выбери количество кнопкой или введи числа с подписями. JSON больше не используется.')
            elif step=='when':
                try:at=datetime.strptime(text.strip(),'%d.%m.%Y %H:%M').replace(tzinfo=self.schedule_zone)
                except ValueError:raise ValueError('Формат даты: ДД.ММ.ГГГГ ЧЧ:ММ') from None
                ts=int(at.timestamp())
                if at.utcoffset() != at.replace(fold=1).utcoffset():
                    raise ValueError('Это местное время попадает на перевод часов. Выбери другой час.')
                if not time.time()+60<=ts<=time.time()+365*86400:raise ValueError('Время должно быть через 1 минуту – 365 дней')
                data['fire_at']=ts
            else:raise ValueError('Шаг не поддерживается')
            await self.next_step(uid,mode,step,data)
        except (ValueError,TypeError) as exc:
            await self.wizard_field(uid,mode,step,error=str(exc))

    async def wizard_skip(self,uid:int):
        d=self.db.draft(uid)
        if not d or d['mode'] not in MODES:return
        mode,step,data=d['mode'],d['step'],d['data']
        if not self.wizard_optional(mode,step,data):return
        if step=='fetch_count':data['fetch_count']=100
        elif step=='fetch_replies':data['fetch_replies']=False
        elif step=='fetch_keywords':data[step]=[]
        elif step=='when':data.pop('fire_at',None)
        else:
            if step!='options':data[step]=[] if step in ('comments','replies') else {}
        await self.next_step(uid,mode,step,data)

    async def preview(self,uid:int)->None:
        d=self.db.draft(uid)
        if not d or d['mode'] not in MODES or d['step']!='review':return
        mode,data=d['mode'],d['data']
        if mode=='publish' and not any(publication_blocks(mode,data)):
            step='replies' if 'comments' in data.get('disabled_fields',[]) else 'comments'
            self.save_wizard_step(uid,mode,step,{**data,'review_edit':True})
            return await self.wizard_field(uid,mode,step,error='Добавь тексты комментариев или ответов.')
        data=complete_order_form(mode,data)
        comments,replies=publication_blocks(mode,data)
        if replies and not comments and not reply_quantity(data):
            self.save_wizard_step(uid,mode,'options',{**data,'review_edit':True})
            return await self.wizard_field(uid,mode,'options',error='Для заказа только на ответы укажи хотя бы один ответ на комментарий.')
        d['data']=data
        self.db.save_draft(uid,mode,'review',data)
        if mode=='keyword_reply' and not data.get('videos'):
            self.save_wizard_step(uid,mode,'videos',{**data,'review_edit':True})
            return await self.show_wizard(uid)
        if mode == 'feedwarm' and not video_search_terms(data.get('warm_json')):
            self.save_wizard_step(uid,mode,'warm_json',{**data,'review_edit':True})
            return await self.wizard_field(uid,mode,'warm_json',error='Укажи тему поиска видео.')
        if mode == 'usernames':
            try:
                validate_username_targets(data.get('videos'))
            except ValueError as exc:
                self.save_wizard_step(uid,mode,'videos',{**data,'review_edit':True})
                return await self.wizard_field(uid,mode,'videos',error=str(exc))
        pool = await self.review_order_accounts(uid,d)
        if pool is None:return
        ids,pool_label = pool
        token=self.wizard_token(d)
        entries=[('Название',data['name']),('Тип',MODES[mode][0]),(pool_label,len(ids))]
        material_fields=['videos','like_links','reply_links','keywords']
        if comments:material_fields.append('comments')
        if replies and reply_quantity(data)>0:material_fields.append('replies')
        for key in material_fields:
            if key in data:entries.append(({'videos':'Видео','comments':'Тексты комментариев','replies':'Тексты ответов','like_links':'Ссылки на лайки','reply_links':'Ссылки на ответы','keywords':'Ключи'}[key],len(data[key])))
        if data.get('likes'):entries.append(('Лайков на комментарий',data['likes']))
        try:
            settings=order_settings(mode,data)
        except ValueError as exc:
            return await self.say(uid,'<b>! Проверь настройки заказа</b>\n'+h(exc),keyboard(
                [('↶ Назад','wiz:back:'+token,None),('× Отменить','wiz:cancel:'+token,'danger')]))
        entries.extend(order_settings_rows(mode,settings) if mode=='fetch' else form_rows(mode,data))
        if mode == 'usernames':
            entries.append(('Где искать','Комментарии и ответы'))
        entries=[row for row in entries if row[0] not in ('Задержка между действиями, сек.','ИИ-уникализация','Аккаунтов на видео','Лимит ответов на видео')]
        if data.get('fire_at'):entries.append(('Запуск',datetime.fromtimestamp(data['fire_at'],self.schedule_zone).strftime('%d.%m.%Y %H:%M %Z')))
        else:entries.append(('Запуск','Когда освободится очередь кабинета'))
        if not self.dispatch_active():entries.append(('Отправка','Приостановлена администратором'))
        # Secondary settings are disclosed below the main summary.
        photo_buttons=[]
        if self.allow_photos(uid) and (choices:=photo_choices(mode,data)):
            current=data.get('photo_mode','off')
            if current not in ('off',*choices):
                current=data['photo_mode']='off'
                self.db.save_draft(uid,mode,'review',data)
            entries.append(('Фото из материалов',PHOTO_LABELS.get(current,PHOTO_LABELS['off'])))
            photo_buttons=[('▧ '+PHOTO_LABELS[setting],f'wiz:photo_{setting}:{token}',None)
                           for setting in ('off',*choices) if setting!=current]
        if mode=='feedwarm':
            try:
                warm=video_search_settings(data.get('warm_json'),account_count=len(ids))
            except ValueError as exc:
                return await self.say(uid,'<b>! Проверь параметры поиска</b>\n'+h(exc),keyboard(
                    [('✎ Изменить тему','wiz:edit_search:'+token,None),('✎ Объём','wiz:edit_options:'+token,None)],
                    [('× Отменить','wiz:cancel:'+token,None)]))
            topics = video_search_terms(warm)
            entries.append(('Темы поиска',' · '.join(topic[:60] for topic in topics[:3])))
            entries.append(('Режим','Только сбор видео'))
            try:
                forecast=await self.api.call('POST','/v1/tasks/feedwarm/forecast',payload=warm,retries=0)
                bytes_est=forecast.get('forecastBytes')
                if type(bytes_est) is int and bytes_est>=0:
                    entries.append(('Прогноз трафика',f'{bytes_est/1048576:.1f} МиБ'))
                    if forecast.get('warn'):entries.append(('Предупреждение','Возможен недостаток трафика'))
                else:
                    entries.append(('Прогноз трафика','не удалось получить'))
            except ApiError:entries.append(('Прогноз трафика','не удалось получить'))
        secondary_labels={field.label for field in order_fields(mode,data) if field.key not in VOLUME_KEYS}
        secondary=[item for item in entries if item[0] in secondary_labels]
        entries=[item for item in entries if item[0] not in secondary_labels and item[0] not in ('Тексты комментариев','Тексты ответов','Ключи','Тип','Название')]
        limit_min=task_execution_limit_seconds({'awemeIds':data.get('videos') or [],'replyLinks':data.get('reply_links') or [],'likeLinks':data.get('like_links') or []})//60
        limit_start='с начала публикаций' if MODE_KIND[mode] in ('publish','keyword_reply') else 'с начала выполнения'
        rich,plain=screen('▤ Проверка заказа',entries,subtitle=data['name'],stage=3,
                          caption=f'После подтверждения заказ встанет в очередь. Лимит выполнения — {minutes_label(limit_min)} {limit_start}. Ожидание в очереди и подготовка аккаунтов не учитываются.')
        if mode=='publish' and data.get('videos'):
            count=len(data['videos'])
            plan=[]
            if comments:plan.append('Основных комментариев: до '+str(count*settings.get('commentsPerVideo',0)))
            if replies and reply_quantity(data)>0:
                if comments:plan.append('Ответов по плану: '+str(count*settings.get('commentsPerVideo',50)*settings.get('repliesPerComment',0)))
                else:plan.append('По '+str(settings.get('repliesPerComment',0))+' ответов на каждый найденный основной комментарий')
            if plan:
                note=' · '.join(plan)+'. Фактический результат зависит от выполнения.'
                rich+='<footer>'+h(note)+'</footer>'
                plain+='\n\n'+h(note)
        if secondary:
            block,_=table_html('Параметры',secondary)
            rich+='<details><summary>Дополнительные параметры</summary>'+block+'</details>'
            plain+='\n\n'+ ' · '.join(h(label)+': '+h(value) for label,value in secondary)
        for field in material_fields:
            values=data.get(field,[])
            if values:
                rich+=f'<details><summary>{h(STEP_LABELS[field])} · {len(values)}</summary><ul>'
                rich+=''.join(f'<li>{h(value[:160])}</li>' for value in values[:3])+'</ul>'
                plain+='\n\n<b>'+h(STEP_LABELS[field])+'</b>\n'+'\n'.join(h(value[:160]) for value in values[:3])
                if len(values)>3:
                    rich+='<footer>Показаны первые 3 строки.</footer>'
                    plain+='\nПоказаны первые 3 строки.'
                rich+='</details>'
        markup=keyboard(
            [('✓ Поставить в очередь','wiz:submit:'+token,'success')],
            [('✎ Объём заказа','wiz:edit_options:'+token,None),('◷ Время запуска','wiz:edit_when:'+token,None)],
            [('✎ Видео / ссылки','wiz:edit_targets:'+token,None)] if mode!='feedwarm' else [],
            [('✎ Название','wiz:edit_name:'+token,None)],
            [('✎ Тексты комментариев','wiz:edit_comments:'+token,None)] if mode=='publish' else [],
            [('✎ Тексты ответов','wiz:edit_replies:'+token,None)] if mode in ('publish','reply_links','keyword_reply') else [],
            [('✎ Лайков на комментарий','wiz:edit_likes:'+token,None)] if mode=='commentlike' else [],
            [('✎ Ключевые фразы','wiz:edit_keywords:'+token,None)] if mode=='keyword_reply' else [],
            [('⚙ Изменить параметры сбора','wiz:fetch_edit:'+token,None)] if mode=='fetch' else [],
            [('⌕ Изменить тему сбора','wiz:edit_search:'+token,None)] if mode=='feedwarm' else [],
            *[[button] for button in photo_buttons],
            [('▧ Сохранить шаблон','wiz:template:'+token,None)],
            [('↶ Назад','wiz:back:'+token,None),('× Отменить','wiz:cancel:'+token,'danger')])
        if isinstance(self.tg,Telegram):
            rich+='<details><summary>Изменить материалы · сохранить шаблон</summary>'
            for row in markup['inline_keyboard'][2:-1]:
                rich+='<tg-button-row>'+''.join('<tg-button type="callback_data" style="link" data="'+h(button['callback_data'])+'">'+h(button['text'])+'</tg-button>' for button in row)+'</tg-button-row>'
            rich+='</details>'
            compact={'inline_keyboard':[markup['inline_keyboard'][0],markup['inline_keyboard'][1],markup['inline_keyboard'][-1]]}
            await self.tg.rich(uid,rich,plain,compact,fallback_keyboard=markup)
        else:
            await self.tg.rich(uid,rich,plain,markup)

    async def _submit_extended(self,uid:int)->None:
        d=self.db.draft(uid)
        if not d or d['mode'] not in MODES or d['step']!='review':return await self.say(uid,'Черновик отсутствует.')
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        mode,data=d['mode'],d['data']
        if self.mode_restricted(uid,mode):
            return await self.say(uid,'Этот вид заказа доступен только владельцу.')
        if mode=='check':return await self.begin(uid,'check')
        data=complete_order_form(mode,data)
        if not self.allow_photos(uid):data={**data,'photo_mode':'off'}
        try:
            comments,replies=publication_blocks(mode,data)
            if replies and not comments and not data['options'].get('repliesPerComment'):
                raise ValueError('Для заказа только на ответы укажи хотя бы один ответ на комментарий.')
            payload=await build_payload(self.api,mode,data,self.confirmed_order_accounts(uid,d),defer_resolution=True)
            folder=self.confirmed_order_folder(uid,d)
            if folder is not None:
                payload['accountFolder']=folder
            task_id=self.db.queue_task(uid,data['name'],MODE_KIND[mode],payload,
                    max_daily=self.cfg.max_tasks_per_day,max_open=self.cfg.max_open_tasks,
                    fire_at=int(data.get('fire_at') or 0),draft_token=self.wizard_token(d))
            self.db.log(uid,'task_created',task_id)
            await self.task(uid,task_id)
        except (ApiError,ValueError) as exc:
            text=safe_api_error(exc.code) if isinstance(exc,ApiError) else h(exc)
            await self.say(uid,'<b>! Заказ не создан</b>\n'+text+'\n\nЧерновик сохранён.',keyboard(
                [('↶ К проверке','wiz_continue','primary')],[('↶ Мои задачи','tasks',None)]))

    async def tasks(self,uid:int,page:int=0)->None:
        if not self.owner(uid):
            return await super().tasks(uid,page)
        controls = [[('▣ Задачи из бота','bot_tasks:0',None),('＋ Создать','create','success')],
                    [('↶ Обзор','home',None)]]
        size = 10
        try:
            result = await self.api.task_page(page*size,size)
        except ApiError as exc:
            return await self.say(uid,'Не удалось загрузить задачи. '+self.account_error_message(uid,exc),
                                  keyboard([('↻ Повторить',f'tasks_page:{page}','primary')],*controls))
        heading = f'<b>▣ Все задачи</b> · стр. {page+1}'
        lines, buttons = [], []
        for item in result['items']:
            tid = item['id']
            if not SAFE_ID.fullmatch(tid):
                continue
            name = self.workspace_task_name(item)[:70]
            state = snapshot_state(item,'unknown') or 'unknown'
            status = 'Завершено с ошибками' if state=='completed' and item.get('state')=='completed_with_errors' else state_label(state)
            lines.append(f'{state_symbol(state)} <b>{h(name)}</b>\n└ {h(status)} · <code>{h(tid)}</code>')
            buttons.append([(f'{state_symbol(state)} {name[:45]}','tokora_task:'+tid,None)])
        nav = pagination('tasks_page:',page,result['more'])
        plain=heading+'\n\n'+('\n\n'.join(lines) or 'На этой странице задач нет.')
        rich,_=screen('▣ Все задачи',subtitle='Страница '+str(page+1))
        for item in result['items']:
            if not SAFE_ID.fullmatch(item['id']):continue
            state=snapshot_state(item,'unknown') or 'unknown'
            status='Завершено с ошибками' if state=='completed' and item.get('state')=='completed_with_errors' else state_label(state)
            rich+='<p><b>'+h(self.workspace_task_name(item)[:70])+'</b><br>'+h(state_symbol(state)+' '+status)+'</p>'
        if not buttons:rich+='<p>На этой странице заказов нет.</p>'
        await self.tg.rich(uid,rich,plain,self.owner_keyboard(*buttons,nav,*controls))

    def workspace_task_name(self,result:dict)->str:
        local=self.db.conn.execute('SELECT display_name FROM tasks WHERE upstream_id=?',(result.get('id'),)).fetchone()
        if local:
            return local['display_name']
        name=re.sub(r'^\[PX-[0-9a-f]{32}\]\s*','',str(result.get('name') or ''),count=1)
        return name or str(result.get('id') or 'Заказ')

    async def read_workspace_task(self,tid:str)->dict:
        observed_at = now()
        result = await self.api.task(tid)
        state = snapshot_state(result,'unknown')
        if result.get('id') != tid or state is None:
            raise ApiError('invalid_response')
        settling = state in TERMINAL_STATES and settlement_deadline(result)>observed_at
        return {'task_id':tid, 'display_name':self.workspace_task_name(result)[:100],
                'upstream_id':tid,'workspace':True,
                'kind':result.get('kind'), 'state':state, 'settle_poll_at':settling,
                'snapshot_json':json.dumps(result,ensure_ascii=False)}

    def render_workspace_task(self,card:dict)->tuple[str,str,dict]:
        tid = card['task_id']
        report = [('▥ Отчёт','tokora_report:'+tid,'primary')] if card['kind'] in PUBLICATION_REPORT_KINDS else []
        rich = task_rich(card,(('Раздел','Все задачи'),))
        plain = task_view(card)+'\n\nРаздел · все задачи'
        controls=[]
        if card['state'] in ('running','paused'):
            action='pause' if card['state']=='running' else 'resume'
            controls.append(('Ⅱ Пауза' if action=='pause' else '▷ Продолжить',f'wt:{action}:{tid}',None))
            controls.append(('■ Остановить',f'wt:stopask:{tid}','danger'))
        if workspace_task_updates_live(card):
            caption = f'↻ Карточка обновляется примерно каждые {POLL_SECONDS} секунд.'
            rich += '<footer>'+caption+'</footer>'
            plain += '\n\n'+caption
        return rich,plain,self.owner_keyboard(
            controls,[('↻ Обновить','tokora_task:'+tid,None)],report,
            [('▤ Результаты','entries:'+tid,None),('⇩ Экспорт','exports:'+tid,None)],
            [('◇ Аккаунты задания','task_accounts:'+tid+':0',None)] if card['kind']!='import' else [],
            [('▥ Итоги сбора','collected:'+tid,None)] if card['kind']=='fetch' else [],
            [('✓ Проверить публикации',f'wt:verify:{tid}',None)] if card['kind']=='publish'
            and card['state'] in ('running','paused','completed','stopped') else [],
            [('▷ Видео заказа',f'videos:{tid}',None)] if card['kind'] in ('publish','keyword_reply','fetch','usernames','feedwarm','commentlike') else [],
            [('↶ Все задачи','tasks',None)])

    async def workspace_control(self,uid:int,action:str)->None:
        if not self.owner(uid):return
        parts=action.split(':')
        if len(parts)!=3:return
        _,verb,value=parts
        if verb=='videos':return await self.order_videos(uid,value)
        if verb=='stop':
            draft=self.db.draft(uid)
            if not draft or draft['mode']!='workspace_control' or draft['data'].get('token')!=value:
                return await self.say(uid,'Подтверждение устарело. Открой карточку заказа.')
            tid=draft['data']['task_id']
            self.db.clear_draft(uid)
        else:
            tid=value
        if not SAFE_ID.fullmatch(tid):return
        back=self.owner_keyboard([('↶ К заказу','tokora_task:'+tid,None)])
        try:
            card=await self.read_workspace_task(tid)
            state=card['state']
            if verb=='stopask' and state in ('running','paused'):
                token=token_hex(6)
                self.db.save_draft(uid,'workspace_control','confirm',{'token':token,'task_id':tid})
                return await self.say(uid,'Остановить заказ <b>'+h(card['display_name'])+'</b>?',
                                      self.owner_keyboard([('■ Да, остановить','wt:stop:'+token,'danger')],
                                                          [('↶ К заказу','tokora_task:'+tid,None)]))
            allowed={'stop':('running','paused'),'pause':('running',),'resume':('paused',),
                     'verify':('running','paused','completed','stopped')}
            if verb=='verify' and card['kind']!='publish':return
            if verb not in allowed or state not in allowed[verb]:
                return await self.live_cards.open_workspace_task(uid,card)
            result=await self.api.call('POST','/v1/tasks/'+pth_id(tid)+'/'+verb,payload={},retries=0)
            if result.get('id')!=tid or snapshot_state(result) is None:
                raise ApiError('invalid_response')
            if verb=='stop':result={**result,'stopRequested':True}
            local=self.db.conn.execute('SELECT task_id FROM tasks WHERE upstream_id=?',(tid,)).fetchone()
            if local:self.save_verified_snapshot(self.db.task(local['task_id']),result)
            card.update(state=snapshot_state(result),snapshot_json=json.dumps(result,ensure_ascii=False))
            self.db.log(uid,'workspace_'+verb,tid)
            await self.live_cards.open_workspace_task(uid,card)
        except ApiError as exc:
            await self.say(uid,self.account_error_message(uid,exc),back)

    async def workspace_task(self,uid:int,tid:str,*,report:bool=False)->None:
        if not self.owner(uid) or not SAFE_ID.fullmatch(tid):
            return
        back = [('↶ Все задачи','tasks',None)]
        card_button = [('↶ Карточка задачи','tokora_task:'+tid,None)]
        try:
            card = await self.read_workspace_task(tid)
            if report and card['kind'] in PUBLICATION_REPORT_KINDS:
                rich,plain = report_view(card,await self.api.publication(tid))
                return await self.tg.rich(uid,rich,plain,self.owner_keyboard(card_button,back))
            await self.live_cards.open_workspace_task(uid,card)
        except ApiError as exc:
            if report and exc.code=='job_running':
                return await self.say(uid,'Подробный отчёт будет доступен после завершения заказа. Текущая статистика — в карточке.',
                                      self.owner_keyboard(card_button,back))
            message = 'Задача больше не найдена.' if exc.status==404 and not report else self.account_error_message(uid,exc)
            await self.say(uid,message,self.owner_keyboard(card_button if report else [],back))

    async def workspace_task_accounts(self,uid:int,tid:str,page:int)->None:
        if not self.owner(uid) or not SAFE_ID.fullmatch(tid):return
        back=[('↶ К заказу','tokora_task:'+tid,None)]
        try:
            card=await self.read_workspace_task(tid)
            if card['kind']=='import':
                return await self.say(uid,'Для импорта нет пула выполнения.',self.owner_keyboard(back))
            size=12
            data=await self.api.task_account_page(tid,page*size,size)
            if page and page*size>=data['total']:
                page=max(0,(data['total']-1)//size)
                data=await self.api.task_account_page(tid,page*size,size)
        except ApiError as exc:
            return await self.say(uid,self.account_error_message(uid,exc),self.owner_keyboard(back))
        snap=json.loads(card['snapshot_json'])
        counts=[('Сейчас в составе',number(data['total']))]
        for key,label in (('accountsAssigned','Назначено при запуске'),('accountsWorking','В работе сейчас')):
            if type(snap.get(key)) is int:counts.append((label,number(snap[key])))
        rich,plain=table_html('◇ Аккаунты задания · стр. '+str(page+1),
            [('Заказ',card['display_name']),*counts],
            caption='Это текущий состав пула. Для момента запуска сервис показывает только количество аккаунтов.')
        rows=[]
        for account in data['accounts']:
            aid=account['accountId']
            username=public_account_username(account.get('profile'))
            status=ACCOUNT_STATUS[session_state(account)]
            rows.append((username or '—',aid,status))
        if rows:
            rich+=rich_table(('Аккаунт','ID','Сессия'),rows)
            plain+='\n\n'+'\n'.join(f'{h(username)} · <code>{h(aid)}</code> · {h(status)}' for username,aid,status in rows)
        else:
            rich+='<p>В текущем составе нет аккаунтов.</p>'
            plain+='\n\nВ текущем составе нет аккаунтов.'
        prefix='task_accounts:'+tid+':'
        nav=pagination(prefix,page,(page+1)*size<data['total'])
        await self.tg.rich(uid,rich,plain,self.owner_keyboard(nav,[('↻ Обновить',prefix+str(page),None)],back))

    async def task(self,uid:int,task_id:str)->None:
        t=self.db.task(task_id,uid)
        if not t:return await self.say(uid,'Задача не найдена.')
        await self.live_cards.open(uid,t,self.render_task(t))

    def render_task(self,t:dict)->tuple[str,str,dict]:
        task_id=t['task_id']
        state=t['state']
        kind=t['kind']
        started=bool(t['upstream_id'])
        rows=[]
        if started:
            secondary=('▥ Отчёт','report:'+task_id,None) if kind in PUBLICATION_REPORT_KINDS else ('⇩ Экспорт','exports:'+task_id,None)
            rows.append([('▤ Результаты','entries:'+task_id,'primary'),secondary])
        if kind=='publish' and started and state in ('completed','stopped','running','paused'):
            rows.append([('✓ Проверить статусы','verify:'+task_id,None)])
        if state in ('running','paused') and started:
            control=('Ⅱ Пауза','pause:'+task_id,None) if state=='running' else ('▷ Продолжить','resume:'+task_id,'success')
            rows.append(([control] if self.owner(t['user_id']) else [])+[('■ Остановить','stopconfirm:'+task_id,'danger')])
        if state=='completed' and kind=='fetch' and not self.mode_restricted(t['user_id'],'source_reply'):
            rows.append([('↪ Ответить на собранное','source_reply:'+task_id,'success')])
        if started and kind=='fetch':
            rows.append([('▥ Итоги сбора','collected:'+task_id,None)])
        if kind in ('publish','keyword_reply','fetch','usernames','feedwarm','commentlike'):
            rows.append([('▷ Видео заказа','videos:'+task_id,None)])
        if empty_video_search(t) and not self.mode_restricted(t['user_id'],'feedwarm'):
            rows.append([('⌕ Изменить тему поиска','new:feedwarm','primary')])
        if started and state in ('completed','stopped') and kind=='publish':
            followups = []
            if not self.mode_restricted(t['user_id'],'reply_links'):
                followups.append(('↪ Доответить','replyinfo:'+task_id,None))
            if not self.mode_restricted(t['user_id'],'commentlike'):
                followups.append(('♡ Долайкать','likeinfo:'+task_id,'primary'))
            if followups:
                rows.append(followups)
        mode=task_order_mode(kind,json.loads(t['payload_json'])) if state in ('completed','failed') else kind
        if state in ('completed','failed') and self.is_reusable(t) and not self.mode_restricted(t['user_id'],mode):
            reuse=[('▧ В шаблоны','template_task:'+task_id,None)]
            if state=='completed':reuse.append(('＋ Повторить','repeat:'+task_id,'success'))
            elif self.can_replace_order_links(t):reuse.insert(0,('✎ Заменить ссылки','repeat:'+task_id,'primary'))
            rows.append(reuse)
        elif self.failed_collection_reply(t) and not self.mode_restricted(t['user_id'],'source_reply'):
            rows.append([('↻ Проверить и повторить','repeat:'+task_id,'primary')])
        if state=='queued':rows.append([('× Отменить заказ','cancel:'+task_id,'danger')])
        if state in ('completed','stopped','failed','canceled'):
            rows.append([('Скрыть из списка','hide_task:'+task_id,None)])
        rows.append([('↶ Мои задачи','tasks',None),('↻ Обновить','refresh:'+task_id,None)])
        txt=task_view(t)
        details=()
        if t.get('fire_at'):
            date=datetime.fromtimestamp(t['fire_at'],self.schedule_zone).strftime('%d.%m.%Y %H:%M %Z')
            txt+='\n└ Запланировано: <code>'+h(date)+'</code>'
            details=(('Запланировано',date),)
        rich=task_rich(t,details)
        if state=='queued' and not self.dispatch_active():
            caption='Запуск очереди отключён администратором.'
            txt+='\n\n'+caption
            rich+='<footer>'+caption+'</footer>'
        elif task_updates_live(t):
            txt+='\n\n↻ Карточка обновляется автоматически.'
            rich+='<footer>↻ Карточка обновляется автоматически.</footer>'
        return rich,txt,keyboard(*rows)

    @staticmethod
    def is_reusable(task:dict) -> bool:
        payload=json.loads(task['payload_json'])
        return task['kind']!='check' and not (payload.get('sourceJob') or payload.get('likeSourceJob'))

    @staticmethod
    def can_replace_order_links(task: dict) -> bool:
        return (task['state'] == 'failed' and not task['upstream_id']
                and task['error_code'] in ('invalid_order_targets', 'short_link_unresolved', 'invalid_target',
                                           'invalid_reply_links', 'too_many_reply_links'))

    @staticmethod
    def failed_collection_reply(task: dict) -> dict | None:
        if task['state'] != 'failed' or task['upstream_id'] or task['error_code'] not in ('invalid_target', 'invalid_order_targets'):
            return None
        payload = json.loads(task['payload_json'])
        return payload if is_collection_reply(payload) else None

    async def templates(self,uid:int,page:int=0)->None:
        records=self.db.templates(uid)
        size=10
        page=max(0,min(page,(len(records)-1)//size))
        rows=[[('＋ Создать шаблон','tpl_new','success')]]
        own_count=sum(item.get('owned',True) for item in records)
        rich,txt=screen('▧ Мои шаблоны',subtitle='Готовые тексты для следующего заказа',caption=f'Своих шаблонов {own_count} из 50 · Страница {page+1}')
        for item in records[page*size:(page+1)*size]:
            label=TEMPLATE_LABELS.get(item['mode'],'Заказ')
            suffix=' · для команды' if item.get('is_shared') and item.get('owned',True) else (' · командный' if not item.get('owned',True) else '')
            rows.append([('▧ '+item['name'][:45]+suffix,'tpl:'+item['template_id'],None)])
            rich+=button_row((item['name'][:60],'tpl:'+item['template_id'],'link'))+'<footer>'+h(label+suffix)+'</footer>'
            txt+='\n\n<b>'+h(item['name'])+'</b>\n'+h(label+suffix)
        if not records:
            rich+='<p>Шаблонов пока нет. Создай первый, чтобы использовать готовые тексты в новых заказах.</p>'
            txt+='\n\nШаблонов пока нет. Создай первый, чтобы использовать готовые тексты в новых заказах.'
        nav=pagination('templates_page:',page,len(records)>(page+1)*size)
        rows.extend([nav,[('＋ Новая задача','create',None)],[('↶ Инструменты','tools',None)]])
        compact=keyboard(rows[0],nav,rows[-2],rows[-1])
        if isinstance(self.tg,Telegram):await self.tg.rich(uid,rich,txt,compact,fallback_keyboard=keyboard(*rows))
        else:await self.tg.rich(uid,rich,txt,keyboard(*rows))

    async def save_draft_template(self,uid:int):
        d=self.db.draft(uid)
        if not d or d['mode'] not in MODES or d['step']!='review':return
        data={key:value for key,value in d['data'].items()
              if key not in ('wizard_token','fire_at','review_account_ids','review_account_folder')}
        await self.save_order_template(uid,data['name'],d['mode'],{'draft':data},'wiz_continue')

    async def save_order_template(self,uid:int,name:str,mode:str,payload:dict,back:str)->None:
        values=template_values({'mode':mode,'name':name,'payload':json.dumps(payload)})
        for field in ('videos','reply_links','like_links'):
            values.pop(field,None)
        asked=set(values.get('ask_fields') or [])
        asked.add('options')
        if mode in MODES:
            asked.update(field for field in ('videos','reply_links','like_links') if field in self.wizard_steps(mode))
        values['ask_fields']=sorted(asked)
        self.db.save_draft(uid,'local_template','review',{'mode':mode,'template_id':None,'values':values,'token':token_hex(6)})
        await self.local_template_field(uid)

    async def from_template(self,uid:int,tid:str):
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        item=self.db.template(uid,tid)
        if not item:return await self.say(uid,'Шаблон не найден.')
        data=json.loads(item['payload'])
        if item['mode'] in MEDIA_TEMPLATE_MODES:
            return await self.media_template_drafts(uid,tid)
        if item['mode']=='source_reply':
            return await self.source_template_orders(uid,tid)
        mode=template_mode(item)
        job=data.get('job',{})
        job_mode=task_order_mode(job.get('kind',mode),job)
        if self.mode_restricted(uid,mode) or self.mode_restricted(uid,job_mode):
            return await self.say(uid,'Этот вид заказа доступен только владельцу.')
        if mode=='check' or job_mode=='check':
            return await self.begin(uid,'check')
        if mode not in MODES:
            return await self.say(uid,'Этот вид заказа недоступен.')
        if not self.has_order_pool(uid):return await self.create(uid)
        if 'draft' in data:
            draft=template_values(item)
            for key in ('fire_at','review_account_ids','review_account_folder','wizard_token','review_edit','template_steps'):
                draft.pop(key,None)
            for field in ('videos','reply_links','like_links'):draft.pop(field,None)
            draft['name']=item['name'][:60]
            remaining=template_input_steps(mode,draft)
            draft.pop('ask_fields',None)
            if remaining:draft['template_steps']=remaining
            self.save_wizard_step(uid,mode,remaining[0] if remaining else 'review',draft)
            return await self.show_wizard(uid)
        if 'job' in data:
            return await self.prepare_copy(uid,data['job'],item['name'])

    async def prepare_copy(self,uid:int,job:dict,name:str):
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        if job.get('sourceJob') or job.get('likeSourceJob'):
            return await self.say(uid,'Запусти повтор из карточки исходной задачи.')
        mode=task_order_mode(job.get('kind',''),job)
        if self.mode_restricted(uid,mode):
            return await self.say(uid,'Этот вид заказа доступен только владельцу.')
        if mode not in MODES:return await self.say(uid,'Этот вид заказа нельзя скопировать.')
        if not self.has_order_pool(uid):return await self.create(uid)
        data=template_values({'mode':mode,'name':name,'payload':json.dumps({'job':job})})
        if not self.allow_photos(uid):data.pop('photo_mode',None)
        remaining=template_input_steps(mode,data)
        if remaining:data['template_steps']=remaining
        self.save_wizard_step(uid,mode,remaining[0] if remaining else 'review',data)
        await self.show_wizard(uid)

    async def copy_preview(self,uid:int)->None:
        draft=self.db.draft(uid)
        if not draft or draft['mode']!='task_copy':return
        data=draft['data'];job=data['job']
        mode=task_order_mode(job['kind'],job)
        if self.mode_restricted(uid,mode):
            return await self.say(uid,'Этот вид заказа доступен только владельцу.')
        pool = await self.review_order_accounts(uid,draft)
        if pool is None:return
        ids,pool_label = pool
        entries=[('Название',data['name']),('Вид заказа',MODES[mode][0]),
                 (pool_label,len(ids))]
        for key,label in (('awemeIds','Видео'),('replyLinks','Ссылки для ответов'),('likeLinks','Ссылки для лайков')):
            if job.get(key):entries.append((label,len(job[key])))
        for key,label in (('commentTexts','Тексты комментариев'),('replyTexts','Тексты ответов')):
            if job['assets'].get(key):entries.append((label,len(job['assets'][key])))
        if job.get('likesPerComment'):entries.append(('Лайков на комментарий',job['likesPerComment']))
        entries.extend(order_settings_rows(mode,job.get('settings') or {}))
        if job.get('usePhoto'):
            entries.append(('Фото из материалов','Включены'))
        entries.append(('Запуск','Когда подойдёт очередь' if self.dispatch_active() else 'Отправка приостановлена администратором'))
        rich,plain=table_html('▤ Проверка копии заказа',entries)
        token=self.wizard_token(draft)
        await self.tg.rich(uid,rich,plain,keyboard(
            [('✓ Поставить в очередь','wiz:submit:'+token,'success')],
            [('× Отменить','wiz:cancel:'+token,'danger')]))

    async def queue_existing(self,uid:int,job:dict,name:str,token:str):
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        if self.mode_restricted(uid,task_order_mode(job.get('kind',''),job)):
            return await self.say(uid,'Этот вид заказа доступен только владельцу.')
        draft = self.db.draft(uid)
        if not draft or draft['mode']!='task_copy':return
        try:
            ids=self.confirmed_order_accounts(uid,draft)
            if not ids:return await self.create(uid)
            validate_task_options(job.get('settings'))
            if 'feedWarm' in job:validate_feedwarm(job['feedWarm'],account_count=len(ids))
            payload={key:value for key,value in job.items() if key!='accountFolder'}
            payload['assets']={**job['assets'],'accountIds':ids}
            folder=self.confirmed_order_folder(uid,draft)
            if folder is not None:
                payload['accountFolder']=folder
            tid=self.db.queue_task(uid,name,job['kind'],payload,
                max_daily=self.cfg.max_tasks_per_day,max_open=self.cfg.max_open_tasks,draft_token=token)
            await self.task(uid,tid)
        except ValueError as exc:
            await self.say(uid,'<b>! Копия не создана</b>\n'+h(exc),keyboard(
                [('↶ К проверке','wiz_continue',None)],[('↶ Мои задачи','tasks',None)]))

    async def schedules(self,uid:int):
        tasks=self.db.recent_tasks(uid,100)
        tasks=[t for t in tasks if t.get('fire_at') and t['state'] in ('queued','reconcile')]
        txt='<b>◷ Расписание</b>\n\nЧасовой пояс: '+h(self.cfg.timezone_name)+'\n\n'
        rows=[]
        for t in tasks[:20]:
            txt+=('├ <code>'+t['task_id']+'</code> · '+h(datetime.fromtimestamp(t['fire_at'],self.schedule_zone).strftime('%d.%m %H:%M'))+' · '+h(t['display_name'])+'\n')
            rows.append([(f'◷ {t["task_id"]} · {t["display_name"][:25]}','task:'+t['task_id'],None)])
        if not tasks:txt+='Запланированных задач нет.'
        rows.append([('＋ Создать задачу','create','success')]);rows.append([('↶ Инструменты','tools',None)])
        await self.say(uid,txt[:3900],keyboard(*rows))

    async def settings(self,uid:int):
        if not self.owner(uid):
            return await self.say(uid,'<b>⚙︎ Настройки</b>\n\n'
                f'├ Часовой пояс · <code>{h(self.cfg.timezone_name)}</code>\n'
                f'└ Telegram ID · <code>{uid}</code>',keyboard(
                [('◇ Подписка','access',None),('▧ Инструменты','tools',None)],
                [('↶ Обзор','home',None)]))
        await self.say(uid,'<b>⚙︎ Настройки кабинета</b>\n\nОбщие параметры для новых заказов.',keyboard(
            [('▤ Текущие настройки','owner_show:settings','primary')],
            [('✎ Изменить настройки','owner_edit:settings',None)],
            [('◷ Окно подтверждения публикаций','owner_edit:settlement',None)],
            [('▧ Пресеты','owner_show:presets',None),('＋ Сохранить пресет','owner_edit:preset_new',None)],
            [('↶ Обзор','home',None)]))

    async def posts(self,uid:int):
        if not self.owner(uid):
            return await self.say(uid,'<b>▷ Публикации · Видео / фото</b>\n\n'
                'Видео, альбомы и общую очередь ведёт администратор команды.',
                keyboard([('▣ Мои задачи','tasks','primary')],[('↶ Обзор','home',None)]))
        await self.say(uid,'<b>▷ Публикации · Видео / фото</b>\n\nПодготовь черновик, создай план и запусти публикацию.',keyboard(
            [('▧ Черновики','owner_show:drafts','primary')],
            [('↑ Загрузить видео','upload:video','success'),('＋ Фотоальбом','owner_edit:album',None)],
            [('＋ Новая публикация','owner_show:drafts',None),('▤ Последний план','owner_show:plan_latest',None)],
            [('▣ Очередь публикаций','owner_show:post_queue',None)],
            [('⚙︎ Настройки','owner_show:posts_settings',None),('▧ Шаблоны подписей','owner_show:post_templates',None)],
            [('↶ Обзор','home',None)]))

    async def materials(self,uid:int):
        if not self.owner(uid):
            return await self.say(uid,'<b>▤ Материалы</b>\n\n'
                'Тексты для твоих задач отправляются локально внутри задания. '
                'Общие материалы кабинета других участников недоступны.',keyboard(
                [('▧ Мои шаблоны','templates','primary')],[('＋ Новая задача','create',None)],[('↶ Инструменты','tools',None)]))
        await self.say(uid,'<b>▤ Общие материалы</b>\n\n'
            'Только администратор может изменять глобальные материалы.',keyboard(
            [('▣ Комментарии','owner_show:asset_comments',None),('▣ Ответы','owner_show:asset_replies',None)],
            [('✎ Заменить комментарии','owner_edit:asset_comments',None)],
            [('✎ Заменить ответы','owner_edit:asset_replies',None)],
            [('▣ Фото комментариев','owner_show:photos',None),('▣ Фото ответов','owner_show:photos_replies',None)],
            [('↑ Загрузить фотографию','upload:photo','success')],
            [('↶ Обзор','home',None)]))

    async def owner_show(self,uid:int,key:str,page:int=0)->None:
        if not self.owner(uid):return
        if key=='accounts_check':
            status = await read_workspace_account_check(self.api, self.db)
            return await self.live_cards.open_account_check(uid,status)
        if key=='proxies_check':
            return await self.show_proxy_check(uid)
        if key=='proxies':
            return await self.proxies(uid)
        endpoints={
          'settings':'/v1/settings','presets':'/v1/presets',
          'posts_settings':'/v1/posts/settings','drafts':'/v1/posts/drafts',
          'plan_latest':'/v1/posts/plan/latest','post_queue':'/v1/posts','queue_state':'/v1/posts/queue',
          'post_templates':'/v1/posts/templates',
          'photos':'/v1/photos','photos_replies':'/v1/photos','asset_comments':'/v1/assets/commentTexts',
          'asset_replies':'/v1/assets/replyTexts', 'stats':'/v1/statistics',
          'schedule_upstream':'/v1/schedules', 'posts_summary':'/v1/posts/summary',
        }
        if key not in endpoints:return
        size = {'post_queue':15, 'schedule_upstream':20}.get(key)
        params = {'target':'replies'} if key=='photos_replies' else None
        if size is not None:
            params = {'offset':page*size, 'limit':size}
        try:result=await self.api.call('GET',endpoints[key],params=params)
        except ApiError as exc:
            if key!='plan_latest' or exc.status!=404:raise
            result={}
        if key in ('asset_comments','asset_replies'):
            content=result.get('lines',[])
            visible, page, nav = owner_list_page(content, key, page, 15)
            txt='<b>▤ '+h('Комментарии' if key=='asset_comments' else 'Ответы')+'</b>\n'
            txt+=f'Записей: <code>{len(content)}</code> · стр. {page+1}\n\n'
            txt+='\n'.join('├ '+h(str(x)[:120]) for x in visible)
            return await self.say(uid,txt,keyboard(nav,[('✎ Заменить','owner_edit:'+key,'primary')],[('↶ Материалы','materials',None)]))
        if key in ('settings','posts_settings'):
            label='Настройки кабинета' if key=='settings' else 'Настройки публикаций'
            entries=[('Окно подтверждения · мин' if k=='settleMinutes' else k,str(v)[:130])
                     for k,v in result.items() if not any(s in k.lower() for s in ('key','cookie','password','token','proxy'))]
            rich,plain=table_html('⚙ '+label,entries[:35])
            settlement=[('◷ Изменить окно подтверждения','owner_edit:settlement',None)] if key=='settings' else []
            return await self.tg.rich(uid,rich,plain,keyboard([('✎ Изменить','owner_edit:'+key,'primary')],settlement,
                [('↶ Настройки','settings' if key=='settings' else 'posts',None)]))
        if key in ('stats','posts_summary'):
            if key=='stats':
                rows=[]
                for name,label in (('today','Сегодня'),('day','24 часа'),('week','7 дней'),('all','Всего')):
                    totals=result.get(name) or {}
                    rows.append((label,totals.get('comments',0),totals.get('replies',0),totals.get('moderation',0)))
                rich='<h2>▤ Статистика кабинета</h2>'+rich_table(('Период','Комментарии','Ответы','Модерация'),rows)
                plain='<b>▤ Статистика кабинета</b>\n'+'\n'.join(
                    f'├ {h(label)} · {h(comments)} / {h(replies)} / {h(moderation)}'
                    for label,comments,replies,moderation in rows)
                return await self.tg.rich(uid,rich,plain,keyboard([('↶ Панель','admin',None)]))
            values=[(k,v) for k,v in result.items() if isinstance(v,(str,int,float,bool))]
            rich,plain=table_html('▤ '+key,values[:45])
            return await self.tg.rich(uid,rich,plain,keyboard([('↶ Обзор','home',None)]))
        if key=='drafts':
            items=select_items(result)
            visible, page, nav = owner_list_page(items, key, page, 25)
            rows=[]
            for item in visible:
                oid=str(item.get('id',''))
                if SAFE_ID.fullmatch(oid):rows.append([(f'▧ {str(item.get("name") or oid)[:36]}','draft:'+oid,None)])
            rows.extend([nav,[('↑ Видео','upload:video','success'),('＋ Фотоальбом','owner_edit:album',None)]])
            rows.append([('↶ Публикации','posts',None)])
            return await self.say(uid,'<b>▧ Черновики</b>\n\n'
                f'Всего: <code>{number(result.get("total",len(items)))}</code> · стр. {page+1}\n'
                'Выбери черновик или создай новый.',self.owner_keyboard(*rows))
        if key=='post_queue':
            items=select_items(result,('items','tasks','posts','queue'))
            total = result.get('total', len(items))
            nav = pagination('owner_show:post_queue:', page, (page+1)*size < total)
            text=f'<b>▣ Очередь публикаций</b> · стр. {page+1}\n'
            text+=f'Всего: <code>{number(total)}</code> · '
            text+='на паузе\n\n' if result.get('paused') else 'активна\n\n'
            for item in items[:15]:
                state=item.get('outcome') or item.get('state','')
                text+='├ <code>'+h(item.get('id',''))+'</code> · '+h(POST_STATUS_LABELS.get(state,'Статус уточняется'))+'\n'
            if not items:text+='На этой странице записей нет.\n'
            rows=[[(f'▷ {str(x.get("name") or x.get("id"))[:36]}', 'post:'+str(x['id']),None)]
                  for x in items[:15] if SAFE_ID.fullmatch(str(x.get('id') or ''))]
            rows += [nav,[('▥ Состояние очереди','owner_show:queue_state',None)],
                   [('Ⅱ Пауза','owner_go:posts_pause',None),('▷ Продолжить','owner_go:posts_resume','success')],
                   [('↻ Обновить статусы','owner_go:posts_refresh','primary')],
                   [('× Отменить ожидающие','owner_go:posts_cancel_queued','danger')],
                   [('× Очистить историю','owner_go:posts_clear_history','danger')],
                   [('↶ Публикации','posts',None)] ]
            return await self.say(uid,text,self.owner_keyboard(*rows))
        if key=='queue_state':
            rows = [
                ('Состояние', 'На паузе' if result.get('paused') else 'Активна'),
                ('Записей', number(result.get('total'))),
                ('В работе / лимит', f'{number(result.get("active"))} / {number(result.get("activeLimit"))}'),
                ('Ждут повтора', number(result.get('retrying'))),
                ('Подписка кабинета', 'Активна' if result.get('subscriptionActive') else 'Не активна'),
            ]
            retry_at = result.get('nextRetryAt')
            if type(retry_at) is int and 0 < retry_at < 10**14:
                rows.append(('Следующий повтор', self.format_date(retry_at//1000)))
            rich,plain = table_html('▥ Состояние очереди публикаций', rows)
            return await self.tg.rich(uid,rich,plain,keyboard(
                [('↻ Обновить','owner_show:queue_state','primary')],
                [('↶ Очередь публикаций','owner_show:post_queue',None)]))
        if key=='plan_latest':
            if not result or not result.get('id'):
                return await self.say(uid,'План пока не создан.',keyboard([('◷ Создать план','owner_edit:post_plan','success')]))
            pid=str(result['id'])
            if not SAFE_ID.fullmatch(pid):return
            return await self.show_plan(uid,pid,result)
        if key=='post_templates':
            items=select_items(result)
            visible, page, nav = owner_list_page(items, key, page, 25)
            rows=[]
            for v in visible:
                oid=str(v.get('id',''))
                if SAFE_ID.fullmatch(oid):rows.append([(f'▧ {str(v.get("name") or oid)[:42]}','posttpl:'+oid,None)])
            rows.extend([nav,[('＋ Создать','owner_edit:post_template','success')]])
            rows.append([('↶ Публикации','posts',None)])
            return await self.say(uid,f'<b>▧ Шаблоны подписей</b>\nВсего: {len(items)} · стр. {page+1}',self.owner_keyboard(*rows))
        if key=='presets':
            items=preset_items(result)
            visible, page, nav = owner_list_page(items, key, page, 25)
            rows=[]
            for i,v in enumerate(visible, page*25):
                name=str(v.get('name') or '')
                # Index is resolved against a freshly fetched list at click time.
                rows.append([(f'⚙︎ {name[:38]} · '+('встроенный' if v.get('builtin') else 'личный'),'preset:'+str(i),None)])
            rows.extend([nav,[('＋ Сохранить','owner_edit:preset_new','success')],[('↶ Настройки','settings',None)]])
            return await self.say(uid,f'<b>▧ Пресеты API</b>\n\nПресетов: <code>{len(items)}</code> · стр. {page+1}\n'
               'Выбери пресет для применения или удаления.',keyboard(*rows))
        if key in ('photos','photos_replies'):
            items=select_items(result)
            visible, page, nav = owner_list_page(items, key, page, 25)
            rows=[]
            for item in visible:
                oid=str(item.get('id',''))
                if SAFE_ID.fullmatch(oid):rows.append([(f'▣ {oid[:20]} · {item.get("width","")}×{item.get("height","")}',
                                                       'photo:'+('r:' if key=='photos_replies' else 'c:')+oid,None)])
            rows.extend([nav,[('↑ Добавить','upload:photo','success')],[('↶ Материалы','materials',None)]])
            return await self.say(uid,f'<b>▣ Фото</b>\nВсего: <code>{len(items)}</code> · стр. {page+1}',self.owner_keyboard(*rows))
        if key=='schedule_upstream':
            items=select_items(result)
            nav = pagination('owner_show:schedule_upstream:', page, result.get('more') is True)
            lines = '\n'.join('├ '+h(x.get('id',''))+' · '+h(x.get('state','')) for x in items)
            return await self.say(uid,f'<b>◷ Задания в расписании API</b> · стр. {page+1}\n\n'+
                (lines or 'На этой странице записей нет.'),keyboard(nav,[('↶ Обзор','home',None)]))

    async def show_plan(self,uid:int,pid:str,result:dict|None=None):
        if not self.owner(uid):return
        v=result or await self.api.call('GET','/v1/posts/plan/'+pth_id(pid))
        if v.get('id')!=pid:raise ApiError('invalid_response')
        await self.live_cards.open_check(uid,'media_plan',self.render_post_plan(v),v.get('state')=='checking',pid)

    def render_post_plan(self,v:dict)->tuple[str,str,dict]:
        pid=v['id']
        labels={'id':'ID плана','state':'Статус','postsTotal':'Публикаций','queued':'В очереди',
                'accepted':'Подошли','skipped':'Пропущено','checked':'Проверено','candidates':'Кандидатов','error':'Ошибка'}
        fields=[(label,SERVICE_NAME.sub('сервис',str(v[key])) if key=='error' else v[key])
                for key,label in labels.items() if key in v and v[key] not in (None,'')]
        states={'checking':'Проверяется','ready':'Готов к запуску','queued':'В очереди',
                'failed':'Ошибка','canceled':'Отменён','expired':'Срок истёк'}
        state=v.get('state')
        if post_plan_expired(v) and not v.get('queued'):
            state='expired'
        fields=[(label,states.get(state,state) if label=='Статус' else value) for label,value in fields]
        if state=='checking':
            fields.append(('Следующий шаг','Обнови план после проверки аккаунтов'))
        elif state=='ready' and not v.get('postsTotal'):
            fields.append(('Следующий шаг','Проверь черновики и доступные аккаунты'))
        rich,plain=table_html('◷ План публикаций',fields)
        launch=[('✓ Запустить план','plan_confirm:'+pid,'success')] if post_plan_ready(v) else []
        return rich,plain,self.owner_keyboard(launch,
            [('↻ Обновить план','plan:'+pid,None)],
            [('× Удалить план','plan_delete_confirm:'+pid,'danger')],
            [('↶ Публикации','posts',None)])

    async def create_post_plan(self,uid:int,body:dict)->None:
        if not self.owner(uid):return
        self._allowed_fields(body,'PostPlanRequest')
        result=await self.api.call('POST','/v1/posts/plan',payload=body,retries=0)
        draft=self.db.draft(uid)
        if draft and draft['mode']=='owner_edit' and draft['step']=='post_plan':self.db.clear_draft(uid)
        self.db.log(uid,'owner_edit','post_plan')
        pid=str(result.get('id') or '')
        if SAFE_ID.fullmatch(pid):return await self.show_plan(uid,pid,result)
        await self.say(uid,'План принят. Проверь его в разделе «Последний план».',
                       keyboard([('◷ Последний план','owner_show:plan_latest','primary')]))

    async def draft_info(self,uid,oid:str):
        if not self.owner(uid):return
        d=await self.api.call('GET','/v1/posts/drafts/'+pth_id(oid))
        pending=self.db.draft(uid)
        if pending and pending['mode']=='upload' and pending['step']=='album_'+oid:
            self.db.clear_draft(uid)
        labels={'name':'Название','kind':'Тип','description':'Описание','hashtags':'Хештеги',
                'visibility':'Видимость','comments':'Комментарии','activeTasks':'Активных задач','sizeBytes':'Размер, Б',
                'durationMs':'Длительность, мс','selected':'Выбран'}
        fields=[(label,('Да' if d[key] else 'Нет') if isinstance(d[key],bool) else str(d[key])[:160])
                for key,label in labels.items() if key in d and d[key] not in (None,'')]
        album=d.get('kind')=='photo'
        photos=d.get('photos')
        if album and isinstance(photos,list):
            fields.append(('Фото в альбоме',len(photos)))
        rich,plain=table_html('▧ Черновик',fields)
        add_photo=[('＋ Добавить фото','upload:album_'+oid,'primary')] if album else []
        plan=[('◷ Создать план для черновика','draft_plan:'+oid,'success')] if not (album and photos==[]) else []
        await self.tg.rich(uid,rich,plain,self.owner_keyboard(
            [('✎ Изменить','owner_edit:draft_'+oid,'primary')],
            [('▧ Применить шаблон подписи','caption_pick:'+oid+':0',None)],
            plan,
            add_photo,
            [('× Удалить','draft_delete_confirm:'+oid,'danger')],
            [('↶ Черновики','owner_show:drafts',None)]))

    async def owner_edit(self,uid:int,key:str):
        if not self.owner(uid):return
        if key in ('settings','posts_settings','post_plan'):
            return await self.say(uid,'Этот ввод больше не используется. Создай заказ или выбери черновик публикации: параметры задаются кнопками.',keyboard([('↶ Обзор','home',None)]))
        if key.startswith('draft_'):
            return await self.begin_media_order(uid,key[6:])
        if key=='post_template':
            self.db.save_draft(uid,'caption_template','name',{})
            return await self.say(uid,'<b>▧ Новая подпись</b>\n\nВведи название шаблона, до 60 символов.',keyboard([('Отменить','discard',None)]))
        text={
         'settlement':'Введи длительность окна проверки публикаций в минутах. Настройка применяется к новым заказам.',
         'preset_new':'Отправь название пресета настроек (до 64 символов).',
         'album':'Отправь название нового фотоальбома (до 80 символов).',
         'asset_comments':'Отправь новые строки комментариев текстом либо TXT-документом. Это полностью заменит общие материалы.',
         'asset_replies':'Отправь новые строки ответов текстом либо TXT-документом. Это полностью заменит общие материалы.',
        }.get(key)
        if not text:return
        self.db.save_draft(uid,'owner_edit',key,{})
        labels={'settings':'Настройки кабинета','settlement':'Окно подтверждения публикаций',
                'posts_settings':'Настройки публикаций','preset_new':'Новый пресет',
                'album':'Новый фотоальбом','post_plan':'Новый план','post_template':'Шаблон подписи',
                'asset_comments':'Материалы · комментарии','asset_replies':'Материалы · ответы'}
        await self.say(uid,'<b>✎ '+h(labels.get(key,'Редактировать черновик'))+'</b>\n\n'+text,
                       keyboard([('↶ Отмена','discard',None)]))

    async def aux_input(self,uid:int,d:dict,text:str):
        if not self.owner(uid):return
        mode,step=d['mode'],d['step']
        if mode!='owner_edit':return
        if step in ('settings','posts_settings','post_plan','post_template') or step.startswith('draft_'):
            return await self.owner_edit(uid,step)
        try:
            if step=='settlement':
                if not re.fullmatch(r'[0-9]{1,6}',text.strip()):
                    raise ValueError('Введи целое число минут от 0 до 999 999.')
                await self.api.call('PATCH','/v1/settings',payload={'settleMinutes':int(text)},retries=0)
                message='Окно подтверждения обновлено.'
            elif step in ('asset_comments','asset_replies'):
                content=limited_lines(text,300,1000)
                path='/v1/assets/'+('commentTexts' if step=='asset_comments' else 'replyTexts')
                await self.api.call('PUT',path,payload={'lines':content},retries=0)
                message='Материалы обновлены: '+str(len(content))
            elif step=='preset_new':
                name=text.strip()
                if not 1<=len(name)<=64:raise ValueError('Название: до 64 символов')
                await self.api.call('POST','/v1/presets',payload={'name':name},retries=0)
                message='Пресет сохранён.'
            elif step=='album':
                name=text.strip()
                if not 1<=len(name)<=80:raise ValueError('Название: до 80 символов')
                result=await self.api.call('POST','/v1/posts/drafts',payload={'kind':'photo','name':name},retries=0)
                oid=str(result.get('id',''))
                if SAFE_ID.fullmatch(oid):
                    self.db.log(uid,'owner_edit',step)
                    return await self.upload_intro(uid,'album_'+oid,notice=f'<b>✓ Фотоальбом создан</b>\n{h(name)}')
                message='Фотоальбом создан · '+h(oid)
            else:raise ValueError('Этот способ ввода больше не используется. Открой карточку заново.')
            self.db.log(uid,'owner_edit',step)
            self.db.clear_draft(uid)
            await self.say(uid,'<b>✓ Готово</b>\n'+message,keyboard([('↶ Обзор','home',None)]))
        except (ApiError,ValueError) as exc:
            msg=safe_api_error(exc.code) if isinstance(exc,ApiError) else h(exc)
            await self.say(uid,'<b>Действие не завершено</b>\n'+msg+'\nВвод можно исправить или /cancel.')

    @staticmethod
    def _allowed_fields(obj:dict,schema_name:str,partial:bool=False):
        doc=load_spec()
        schema=(doc['paths']['/v1/posts/drafts/{id}']['patch']['requestBody']['content']['application/json']['schema'] if schema_name=='PostDraftPatch' else doc['components']['schemas'][schema_name])
        extras=set(obj)-set(schema.get('properties',{}))
        if extras:raise ValueError('Неизвестные поля: '+', '.join(sorted(extras)))
        if not partial and not set(schema.get('required',[])).issubset(obj):
            raise ValueError('Отсутствуют обязательные поля: '+', '.join(sorted(set(schema['required'])-set(obj))))
        check=schema if not partial else {**schema,'required':[]}
        if not valid_body(check,obj):raise ValueError('Некорректный тип поля.')

    @staticmethod
    def account_check_active(status:dict)->bool:
        return check_status(status) == 'active'

    def render_account_check(self,status:dict)->tuple[str,str,dict]:
        active=self.account_check_active(status)
        raw=str(status.get('state') or '').lower()
        state='В работе' if active else {
            'done':'Завершена','finished':'Завершена','completed':'Завершена','idle':'Ожидание запуска',
            'unchecked':'Ожидание запуска',
            'stopped':'Остановлена','canceled':'Остановлена','cancelled':'Остановлена',
            'error':'Ошибка','failed':'Ошибка'}.get(raw,'Состояние уточняется')
        rows=[('Статус',state),('Проверено',f'{number(status.get("checked"))} / {number(status.get("total"))}')]
        fields=(('valid','Активны'),('expired','Сессии истекли'),('failed','Ошибки'),
                ('waiting','Ожидают'),('webOnly','Только веб'),('averageMs','Среднее время · мс'))
        rows.extend((label,number(status[key])) for key,label in fields if type(status.get(key)) is int)
        if isinstance(status.get('errors'),dict):
            reasons=error_breakdown([{'code':code,'count':amount} for code,amount in status['errors'].items()])
            if reasons:
                rows.append(('Причины',' · '.join(f'{label} {number(total)}' for label,total in reasons.items())))
        caption='Обновляется автоматически.' if active else 'Проверка охватывает весь кабинет.'
        rich,plain=table_html('✓ Проверка аккаунтов кабинета',rows,caption)
        control=('■ Остановить','owner_go:accounts_check_stop','danger') if active else (
            '✓ Проверить все','owner_go:accounts_check','primary')
        return rich,plain,keyboard([control],[('↻ Обновить','owner_show:accounts_check',None)],
                                   [('↶ Аккаунты','accounts',None)])

    async def post_info(self,uid:int,oid:str,result:dict|None=None):
        if not self.owner(uid):return
        v=result if result is not None else await self.api.call('GET','/v1/posts/'+pth_id(oid))
        if v.get('id')!=oid:
            raise ApiError('invalid_response')
        await self.live_cards.open_check(uid,'media_post',self.render_post_card(v),
            v.get('outcome') in ('queued','uploading','submitting','pending','moderation'),oid)

    def render_post_card(self,v:dict)->tuple[str,str,dict]:
        oid=v['id']
        rows=[(label,str(v.get(key) if v.get(key) is not None else '—')[:150]) for key,label in (
            ('id','ID'),('name','Название'),('outcome','Статус'),('attempts','Попытки'),('accountName','Аккаунт'))]
        rows=[(label,POST_STATUS_LABELS.get(v.get('outcome'),'Статус уточняется') if label=='Статус' else value)
              for label,value in rows]
        if v.get('errorCode'):
            rows.append(('Ошибка',safe_api_error(v['errorCode'])))
        rows.append(('Тип','Фотоальбом' if v.get('kind')=='photo' else 'Видео'))
        if v.get('maxAttempts'):
            rows.append(('Попыток / лимит',f'{number(v.get("attempts"))} / {number(v["maxAttempts"])}'))
        stats=v.get('stats')
        if isinstance(stats,dict):
            rows.extend((label,number(stats[key])) for key,label in
                        (('views','Просмотры'),('likes','Лайки'),('comments','Комментарии'),('shares','Репосты'))
                        if type(stats.get(key)) is int)
        if v.get('cancelRequested'):
            rows.append(('Остановка','Запрошена, ожидается подтверждение'))
        if link:=public_result_link(v.get('postUrl')):
            rows.append(('Ссылка на пост',link))
        rich,plain=table_html('▣ Публикация',rows)
        outcome=v.get('outcome')
        refresh=[('↻ Перепроверить','post_go:refresh:'+oid,None)] if outcome in (
            'pending','moderation','published','unconfirmed','unknown') else []
        retry=[('↻ Повторить','post_retry_confirm:'+oid,'danger')] if outcome in ('failed','unknown') else []
        cancel=[('■ Отменить','post_go:cancel:'+oid,'danger')] if (
            outcome in ('queued','uploading') and not v.get('cancelRequested')) else []
        if v.get('outcome') in ('queued','uploading','submitting','pending','moderation'):
            rich+='<footer>Карточка обновляется автоматически.</footer>'
            plain+='\n\nКарточка обновляется автоматически.'
        return rich,plain,self.owner_keyboard(
            refresh,retry,cancel,
            [('↻ Обновить карточку','post:'+oid,None)],
            [('↶ Очередь публикаций','owner_show:post_queue',None)])

    async def result_order(self,uid:int,tid:str)->dict|None:
        is_owner=self.owner(uid)
        job=self.db.task(tid,None if is_owner else uid)
        if job or not is_owner:return job
        if SAFE_ID.fullmatch(tid):return await self.read_workspace_task(tid)
        return None

    @staticmethod
    def result_callback(uid:int,job:dict,action:str)->str:
        if job.get('workspace'):prefix='tokora_'+action
        elif job['user_id']!=uid:prefix='adm:'+('job' if action=='task' else action)
        else:prefix=action
        return prefix+':'+job['task_id']

    async def order_videos(self,uid:int,tid:str,page:int=0)->None:
        if not SAFE_ID.fullmatch(tid):return
        markup=self.owner_keyboard if self.owner(uid) else keyboard
        try:
            task=await self.result_order(uid,tid)
            if not task:return await self.say(uid,'Заказ не найден.')
            back=[('↶ К заказу',self.result_callback(uid,task,'task'),None)]
            payload=json.loads(task.get('payload_json') or '{}')
            if task.get('workspace'):
                local=self.db.conn.execute('SELECT payload_json FROM tasks WHERE upstream_id=?',(tid,)).fetchone()
                if local:payload=json.loads(local['payload_json'])
            snapshot=json.loads(task.get('snapshot_json') or '{}')
            links=order_video_links(snapshot,payload)
            if not links and task.get('upstream_id') and not task.get('workspace'):
                snapshot=await self.api.task(task['upstream_id'])
                if snapshot.get('id')!=task['upstream_id']:raise ApiError('invalid_response')
                links=order_video_links(snapshot,payload)
            if not links:
                source=payload.get('sourceJob') or payload.get('likeSourceJob') or snapshot.get('sourceJob') or snapshot.get('likeSourceJob')
                if isinstance(source,str) and SAFE_ID.fullmatch(source):
                    original=await self.api.task(source)
                    if original.get('id')!=source:raise ApiError('invalid_response')
                    links=order_video_links(original)
            if not links and task['kind']=='fetch' and task.get('upstream_id'):
                result=await self.api.call('GET','/v1/tasks/'+pth_id(task['upstream_id'])+'/collected/videos')
                links=order_video_links({'awemes':result.get('items') or []})
            size=10
            page=max(0,min(page,(len(links)-1)//size))
            text='<b>▷ Видео заказа</b>\n'+h(task['display_name'])+'\n\n'
            if links:
                text+=f'Всего · {len(links)} · стр. {page+1}\n\n'
                text+='\n'.join(f'<a href="{h(link)}">↗ Открыть видео {index}</a>'
                                for index,link in enumerate(links[page*size:(page+1)*size],page*size+1))
            else:
                text+='Ссылок на видео пока нет. Если заказ ищет видео во время выполнения, открой «Результаты» после их появления.'
            await self.say(uid,text,markup(pagination('videos:'+tid+':',page,len(links)>(page+1)*size),
                [('↻ Обновить','videos:'+tid+':'+str(page),None)],back))
        except ApiError as exc:
            await self.say(uid,self.account_error_message(uid,exc),markup(
                [('↻ Повторить','videos:'+tid+':'+str(page),None)]))

    async def task_entries(self,uid:int,tid:str,offset:int=0):
        job=await self.result_order(uid,tid)
        if not job or not job['upstream_id']:return await self.say(uid,'Результаты пока недоступны.')
        size=10
        markup=self.owner_keyboard if self.owner(uid) else keyboard
        back=[('↶ К задаче',self.result_callback(uid,job,'task'),None)]
        try:
            v=await self.api.call('GET','/v1/tasks/'+pth_id(job['upstream_id'])+'/entries',
                                  params={'offset':offset,'limit':size})
        except ApiError as exc:
            return await self.say(uid,safe_api_error(exc.code),markup(back))
        items=v.get('items',[])[:size]
        content=['<b>▤ Результаты</b>',h(job['display_name'])+' · <code>'+h(tid)+'</code>',
                 f'{number(v.get("total",len(items)))} записей · стр. {offset//size+1}']
        if task_has_replies(job) or (job['kind']=='publish' and any(item.get('kind')=='reply' for item in items)):
            content.append('\n'+h(REPLY_CHECK_NOTE))
        for i,item in enumerate(items,offset+1):
            if item.get('kind')=='like':
                text=f'Лайков подтверждено · {number(item.get("confirmed"))} / {number(item.get("goal"))}'
                if item.get('failed'):
                    text+=f' · ошибок {number(item["failed"])}'
            else:
                username=result_username(item) if job['kind']=='usernames' else ''
                text=h(username or str(item.get('text') or '—')[:145])
            status=result_status(item.get('outcome'))
            content.append('\n<b>'+str(i)+'.</b> '+text+'\n└ '+status)
            if job['kind']=='feedwarm' and (link:=public_result_link(item.get('url')) or public_video_link(item.get('awemeId'))):
                content.append('<a href="'+h(link)+'">↗ Открыть видео</a>')
        no_videos = not items and offset == 0 and not v.get('more') and empty_video_search(job, entry_total=v.get('total'))
        if not items:
            content.append('\nНичего не нашлось. Измени тему поиска и создай новый сбор.' if no_videos else '\nРезультатов на этой странице пока нет.')
        rows=[]
        if no_videos:rows.append([('⌕ Изменить тему поиска','new:feedwarm','primary')])
        nav=[]
        if offset:nav.append(('← Ранее','entries:'+tid+':'+str(max(0,offset-size)),None))
        if v.get('more'):nav.append(('→ Далее','entries:'+tid+':'+str(offset+size),'primary'))
        actions=[('⇩ Экспорт','exports:'+tid,'primary')]
        if job['kind'] in PUBLICATION_REPORT_KINDS:
            actions.insert(0,('▥ Отчёт',self.result_callback(uid,job,'report'),None))
        rows.extend([nav,actions,back])
        await self.say(uid,'\n'.join(content),markup(*rows))

    async def exports(self,uid:int,tid:str):
        job=await self.result_order(uid,tid)
        if not job or not job['upstream_id']:return await self.say(uid,'Пока нет результатов.')
        files=[('⇩ Записи · CSV','export_csv:'+tid,'primary')]
        if job['kind']=='fetch':
            files.append(('⇩ Тексты · TXT','export_texts:'+tid,None))
        elif job['kind'] not in ('usernames','commentlike'):
            files.append(('⇩ Ссылки · TXT','export_links:'+tid,None))
        rows=[files]
        if job['kind'] in PUBLICATION_REPORT_KINDS:
            label='▥ Отчёт по лайкам' if job['kind']=='commentlike' else '▥ Отчёт по текстам'
            rows.append([(label,self.result_callback(uid,job,'report'),None)])
        rows.append([('↶ К результатам','entries:'+tid,None)])
        markup=self.owner_keyboard if self.owner(uid) else keyboard
        await self.say(uid,'<b>⇩ Экспорт задачи</b>\n\nВсе доступные записи. Большие выгрузки придут несколькими файлами.',markup(*rows))

    async def collected_videos(self,uid:int,tid:str):
        task=await self.result_order(uid,tid)
        if not task or task['kind']!='fetch' or not task['upstream_id']:
            return await self.say(uid,'Данные о видео недоступны.')
        result=await self.api.call('GET','/v1/tasks/'+pth_id(task['upstream_id'])+'/collected/videos')
        rows=[('Видео',len(result.get('items') or [])),('Комментариев',number(result.get('comments'))),
              ('Ответов',number(result.get('replies')))]
        rich,plain=table_html('▣ Видео сбора · '+tid,rows)
        if result.get('items'):
            rich+='<table bordered striped compact><tr><th>ID видео</th><th>Комм.</th><th>Ответы</th></tr>'
            for item in result['items'][:20]:
                video=public_result_id(item.get('awemeId')) or '—'
                comments,replies=number(item.get('comments')),number(item.get('replies'))
                link=public_video_link(video)
                label=f'<a href="{h(link)}">{video}</a>' if link else '—'
                rich+=f'<tr><td>{label}</td><td>{comments}</td><td>{replies}</td></tr>'
                plain+=f'\n├ {label} · {comments} / {replies}'
            rich+='</table>'
            if len(result['items'])>20:rich+='<footer>Показаны первые 20 видео.</footer>'
        markup=self.owner_keyboard if self.owner(uid) else keyboard
        await self.tg.rich(uid,rich,plain,markup([('▷ Видео заказа','videos:'+tid,None)],
            [('↶ Задача',self.result_callback(uid,task,'task'),None)]))

    async def export_entries(self, upstream_id:str):
        path = '/v1/tasks/'+pth_id(upstream_id)+'/entries'
        offset = 0
        total = None
        while True:
            params = {'offset':offset, 'limit':EXPORT_ENTRIES_PAGE_SIZE}
            try:
                page = await self.api.call('GET',path,params=params,background=True)
            except ApiError as exc:
                if exc.status != 429 or exc.retry_after > 60:
                    raise
                await asyncio.sleep(max(1, exc.retry_after))
                page = await self.api.call('GET',path,params=params,background=True)
            items = page.get('items')
            more = page.get('more', False)
            if (not isinstance(items,list) or len(items)>EXPORT_ENTRIES_PAGE_SIZE or any(not isinstance(item,dict) for item in items)
                    or page.get('offset',offset)!=offset or type(more) is not bool or (more and not items)):
                raise ApiError('invalid_response')
            if offset == 0 and type(page.get('total')) is int:
                total = page['total']
                if total < len(items) or (more and total == len(items)):
                    raise ApiError('invalid_response')
            if total is not None:
                items = items[:total-offset]
            for item in items:
                yield item
            offset += len(items)
            if total is not None and offset >= total:
                return
            if not more:
                if total is not None and offset < total:
                    raise ApiError('invalid_response')
                return

    async def export_task_links(self,job:dict):
        upstream_id=job['upstream_id']
        if job['kind'] in NATIVE_LINK_EXPORT_KINDS:
            try:
                data,_=await self.api.download('/v1/tasks/'+pth_id(upstream_id)+'/links')
                lines=data.decode('utf-8-sig').splitlines()
            except UnicodeError:
                raise ApiError('invalid_response') from None
            except ApiError as exc:
                if exc.status!=404:
                    raise
            else:
                links=[link for line in lines if (link:=public_result_link(line.strip()))]
                if links:
                    for link in links:
                        yield link
                    return
        async for item in self.export_entries(upstream_id):
            link = public_result_link(item.get('url'))
            if not link and job['kind'] == 'feedwarm':
                link = public_video_link(item.get('awemeId'))
            if link:
                yield link

    async def export_file(self,uid:int,tid:str,mode:str):
        job = await self.result_order(uid,tid)
        if not job or not job['upstream_id'] or (mode=='texts' and job['kind']!='fetch'):
            return await self.say(uid,'Нет доступа к данным.')
        markup=self.owner_keyboard if self.owner(uid) else keyboard
        await self.say(uid,'Готовлю выгрузку. Большие задачи могут занять несколько минут.',markup(
            [('↶ Экспорт','exports:'+tid,None),('↶ К результатам','entries:'+tid,None)]))
        csv_mode = mode == 'csv'
        fields = ('kind','awemeId','cid','text','outcome','status','confirmed','url','at')
        if job['kind']=='usernames':
            fields += ('username','mentions')
        if job['kind']=='commentlike':
            fields += ('goal','failed')
        buffer = io.StringIO(newline='')
        writer = csv.DictWriter(buffer,fieldnames=fields)
        header = b''
        if csv_mode:
            writer.writeheader()
            header = b'\xef\xbb\xbf'+buffer.getvalue().encode('utf-8')
        stem = f'proxima_{tid}_'+('entries' if csv_mode else mode)
        suffix = '.csv' if csv_mode else '.txt'
        with ExportParts(stem+suffix,header=header,max_bytes=EXPORT_PART_BYTES) as export:
            has_rows=False
            try:
                if mode=='links':
                    seen_links=set()
                    async for link in self.export_task_links(job):
                        if link not in seen_links:
                            seen_links.add(link)
                            export.append((link+'\n').encode('utf-8'))
                            has_rows=True
                else:
                    async for item in self.export_entries(job['upstream_id']):
                        if csv_mode:
                            buffer.seek(0)
                            buffer.truncate()
                            writer.writerow(export_csv_row(item,fields))
                            line=buffer.getvalue()
                        else:
                            value=item.get('text')
                            if item.get('kind') not in ('comment','reply') or not isinstance(value,str) or not value.strip():
                                continue
                            line=' '.join(value.splitlines()).strip()+'\n'
                        export.append(line.encode('utf-8'))
                        has_rows=True
            except ApiError as exc:
                return await self.say(uid,'Выгрузка не завершилась. '+safe_api_error(exc.code),markup(
                    [('↶ Экспорт','exports:'+tid,None)]))
            if not has_rows:
                return await self.say(uid,'Пока нет данных для этой выгрузки.',markup(
                    [('↶ Экспорт','exports:'+tid,None)]))
            await export.send(self.tg,uid,'Proxima Soft · экспорт')
        self.db.log(uid,'export',tid)

    async def upload_intro(self,uid:int,kind:str,*,notice:str=''):
        if not self.owner(uid):return
        if kind=='cookies':return await self.account_imports.start(uid,uid)
        if kind=='proxies':
            return await self.proxy_cli_help(uid)
        if kind not in ('video','photo','photo_comments','photo_replies') and not kind.startswith('album_'):return
        if kind.startswith('album_') and not SAFE_ID.fullmatch(kind[6:]):return
        if kind=='photo':
            return await self.say(uid,'<b>↑ Добавить фото</b>\n\nКуда загрузить?',keyboard(
                [('▤ Комментарии','upload:photo_comments','primary'),('↪ Ответы','upload:photo_replies',None)],
                [('↶ Материалы','materials',None)]))
        if kind=='video':hint='Отправь видео MP4 / MOV до 20 МиБ обычным видео или документом.'
        elif kind.startswith('album_'):hint='Отправь JPEG, PNG или WebP до 15 МиБ. Можно отправить несколько фото подряд. Когда закончишь, открой черновик.'
        else:hint='JPEG / PNG / WebP до 15 МиБ. Отправь как фото или документ.'
        self.db.save_draft(uid,'upload',kind,{})
        back=[('✓ К черновику','draft:'+kind[6:],'primary')] if kind.startswith('album_') else []
        await self.say(uid,(notice or '<b>↑ Загрузка медиа</b>')+'\n\n'+hint,self.owner_keyboard(back,[('↶ Отмена','discard',None)]))

    async def handle_upload(self,uid:int,msg:dict)->bool:
        d=self.db.draft(uid)
        if not d:return False
        if d['mode']=='account_import':
            await self.account_imports.receive(uid,msg.get('document'))
            return True
        if (d['mode'] in MODES or d['mode']=='local_template'
                or (d['mode']=='source_reply' and d['step']=='replies')) and msg.get('document'):
            doc=msg['document']
            if not str(doc.get('file_name') or '').lower().endswith('.txt'):
                await self.say(uid,'Нужен обычный TXT-файл в UTF-8.');return True
            try:
                reply_file=d['step']=='reply_links' and (d['mode']=='reply_links' or d['mode']=='local_template')
                # Valid UTF-8 links and blank lines fit under Telegram's download ceiling.
                limit=MAX_REPLY_INPUT_LINES*(MAX_REPLY_LINK_LENGTH+2)*3+3 if reply_file else 100*1024
                data,_=await self.tg.fetch_upload(doc['file_id'],limit)
                text=data.decode('utf-8-sig')
            except (ValueError,TelegramError):
                await self.say(uid,'Не удалось прочитать UTF-8 или файл слишком большой.');return True
            await self.input_draft(uid,text)
            return True
        if self.owner(uid) and d['mode']=='owner_edit' and d['step'] in ('asset_comments','asset_replies') and msg.get('document'):
            doc=msg['document']
            if not str(doc.get('file_name') or '').lower().endswith('.txt'):
                await self.say(uid,'Нужен TXT-файл.');return True
            try:
                content,_=await self.tg.fetch_upload(doc['file_id'],100*1024)
                await self.aux_input(uid,d,content.decode('utf-8-sig'))
            except (ValueError,TelegramError):await self.say(uid,'Не удалось прочитать UTF-8 TXT до 100 КБ.')
            return True
        if not self.owner(uid) or d['mode']!='upload':return False
        kind=d['step']
        photo=msg.get('photo')
        doc=msg.get('document')
        media=msg.get('video') if kind=='video' else (photo[-1] if photo else None)
        f=doc or media
        if not f:
            hint='Отправь видео MP4 / MOV.' if kind=='video' else 'Отправь фото JPEG, PNG или WebP.'
            await self.say(uid,hint);return True
        album_id=kind[6:] if kind.startswith('album_') else ''
        limit=20*1024*1024 if kind=='video' else 15*1024*1024
        try:
            content,path=await self.tg.fetch_upload(f['file_id'],limit)
            # Validate bytes; do not trust extension/content-type from Telegram.
            image_type=''
            if content.startswith(b'\xff\xd8\xff'):image_type='image/jpeg'
            elif content.startswith(b'\x89PNG\r\n\x1a\n'):image_type='image/png'
            elif content.startswith(b'RIFF') and content[8:12]==b'WEBP':image_type='image/webp'
            is_video=content[4:8]==b'ftyp' and len(content)>12
            if kind=='video' and not is_video:raise ValueError('Файл не распознан как MP4/MOV')
            if kind!='video' and not image_type:raise ValueError('Нужен JPEG, PNG или WebP')
            if kind=='video':
                name=(f.get('file_name') or 'video')[:70]
                result=await self.api.upload('/v1/posts/drafts/upload',content,params={'kind':'video','name':name})
            elif album_id:
                result=await self.api.upload('/v1/posts/drafts/'+pth_id(album_id)+'/photos',content,image_type)
            else:
                target='replies' if kind=='photo_replies' else 'comments'
                result=await self.api.upload('/v1/photos',content,image_type,params={'target':target})
            oid=album_id or result.get('id') or (result.get('draft') or {}).get('id') or ''
            if not album_id:
                self.db.clear_draft(uid)
            self.db.log(uid,'upload_media',kind)
            if album_id:
                photos=result.get('photos')
                info=f'Фото в альбоме · {len(photos)}. ' if isinstance(photos,list) else ''
                await self.say(uid,'<b>✓ Фото добавлено</b>\n'+info+'Можешь отправить следующее.',
                               self.owner_keyboard([('✓ К черновику','draft:'+album_id,'primary')]))
                return True
            if kind=='video':
                rows=[[('↶ Черновики','owner_show:drafts',None)]]
                if SAFE_ID.fullmatch(str(oid)):
                    rows.insert(0,[('▧ Открыть черновик','draft:'+str(oid),'primary')])
            else:
                rows=[[('↶ Материалы','materials',None)]]
            await self.say(uid,'<b>✓ Файл принят</b>\n'+('ID: <code>'+h(oid)+'</code>' if oid else 'Операция принята API.'),
                            self.owner_keyboard(*rows))
        except (ValueError,ApiError,TelegramError) as exc:
            detail=(safe_api_error(exc.code) if isinstance(exc,ApiError) else
                    'Telegram не смог передать файл.' if isinstance(exc,TelegramError) else h(exc))
            await self.say(uid,'Ошибка: '+detail)
        return True

    async def owner_go(self,uid:int,key:str,nonce:str|None=None):
        if not self.owner(uid):return
        if key=='proxies_check':
            return await self.start_proxy_check(uid)
        if key=='proxies_check_stop':
            return await self.prompt_stop_proxy_check(uid)
        if key not in ADMIN_ACTIONS and key not in ('accounts_check','accounts_check_stop'):return
        stop_data = None
        if key in DESTRUCTIVE:
            if nonce is None:
                confirmation = {}
                if key=='accounts_check_stop':
                    status = await read_workspace_account_check(self.api,self.db)
                    if not self.account_check_active(status):
                        return await self.live_cards.open_account_check(uid,status)
                    confirmation.update(marker=self.db.account_check_marker(),started_at=status.get('startedAt'))
                nonce = self.save_owner_confirmation(uid,key,**confirmation)
                return await self.say(uid,'<b>Подтверждение</b>\n\nДействие <code>'+h(key)+'</code> может изменить общий кабинет.',keyboard(
                    [('× Да, выполнить',f'owner_confirm:{key}:{nonce}','danger')],
                    [('↶ Отмена','owner_cancel:'+nonce,None)]))
            confirmation = self.consume_owner_confirmation(uid,nonce,action=key)
            if confirmation is None:
                return
            if key=='accounts_check_stop':
                stop_data = confirmation
                if 'marker' not in stop_data or 'started_at' not in stop_data:
                    return await self.owner_show(uid,'accounts_check')
        if key in ('accounts_check','accounts_check_stop'):
            try:
                if key=='accounts_check':
                    if self.pool_checks.running_id or self.account_imports.running_id:
                        raise ValueError('Сейчас идёт проверка пула или импорт. Дождись завершения.')
                    result = await start_workspace_account_check(self.api,self.db)
                else:
                    result = await stop_workspace_account_check(
                        self.api,self.db,marker=stop_data['marker'],started_at=stop_data['started_at'])
            except ValueError as exc:
                return await self.say(uid,h(exc),keyboard([('↶ Аккаунты','accounts',None)]))
            self.db.log(uid,key)
            return await self.live_cards.open_account_check(uid,result)
        method,path,payload=ADMIN_ACTIONS[key]
        await self.api.call(method,path,payload=payload,retries=0)
        self.db.log(uid,key)
        await self.say(uid,'✓ Операция выполнена.\n'+h(key),keyboard([('↶ Администратор','admin',None)]))

    def save_owner_confirmation(self,uid:int,action:str,oid:str='',**details)->str:
        nonce = token_hex(6)
        self.db.save_draft(uid,'owner_confirmation','confirm',{'action':action,'id':oid,'nonce':nonce,**details})
        return nonce

    def consume_owner_confirmation(self,uid:int,nonce:str,*,action:str|None=None)->dict|None:
        draft=self.db.draft(uid)
        if not (draft and draft['mode']=='owner_confirmation' and draft['step']=='confirm'
                and time.time()-draft['updated_at']<=600):
            return None
        data = draft['data']
        if data.get('nonce')!=nonce or (action is not None and data.get('action')!=action):
            return None
        self.db.clear_draft(uid)
        return data

    async def owner_action(self,uid:int,action:str,oid:str):
        if not self.owner(uid):return
        q=pth_id(oid)
        if action in ('acc_check','acc_profile'):
            return await self.account_console_change(uid,oid,action[4:])
        if action=='acc_delete':
            try:
                with self.db.account_action(oid, require_unassigned=True):
                    if await scan_active_tasks(self.api,first_only=True,retries=0):
                        raise ValueError('В кабинете работает задание. Дождись завершения перед удалением.')
                    status = await read_workspace_account_check(self.api,self.db)
                    if check_status(status) != 'idle':
                        raise ValueError('Дождись окончания проверки аккаунтов и обнови её статус.')
                    await self.api.call('DELETE','/v1/accounts/'+q,retries=0)
            except ValueError as exc:
                return await self.say(uid,h(exc),keyboard([('↶ Аккаунты','accounts',None)]))
            self.db.log(uid,action,oid)
            return await self.say(uid,'✓ Аккаунт удалён из кабинета.',keyboard([('↶ Аккаунты','accounts',None)]))
        action_map={
            'draft_delete':('DELETE','/v1/posts/drafts/'+q,None),
            'plan_delete':('DELETE','/v1/posts/plan/'+q,None),
            'plan_launch':('POST','/v1/posts/plan/'+q+'/launch',{}),
            'photo_delete':('DELETE','/v1/photos/'+q,None),
            'post_cancel':('POST','/v1/posts/'+q+'/cancel',{}),
            'post_refresh':('POST','/v1/posts/'+q+'/refresh',{}),
            'post_retry':('POST','/v1/posts/'+q+'/retry',{'confirm':True}),
            'post_resolve':('POST','/v1/posts/'+q+'/resolve',{'resolution':'not_published'}),
        }
        if action not in action_map:return
        method,path,payload=action_map[action]
        if action=='plan_launch':
            plan=await self.api.call('GET','/v1/posts/plan/'+q,retries=0)
            if plan.get('id')!=oid:
                return await self.say(uid,'Не удалось подтвердить ID плана. Обнови карточку.',
                                      self.owner_keyboard([('↻ Обновить план','plan:'+oid,None)]))
            if not post_plan_ready(plan):
                return await self.show_plan(uid,oid,plan)
        try:
            result=await self.api.call(method,path,payload=payload,retries=0)
        except ApiError as exc:
            if action=='plan_launch':
                message=safe_api_error(exc.code)
                if exc.status==0 or exc.status>=500:
                    message='Запуск плана не подтверждён. Проверь очередь перед повтором.'
                return await self.say(uid,message,self.owner_keyboard(
                    [('▣ Очередь публикаций','owner_show:post_queue','primary')],[('↶ К плану','plan:'+oid,None)]))
            if action.startswith('post_'):
                return await self.say(uid,safe_api_error(exc.code),self.owner_keyboard(
                    [('↶ К публикации','post:'+oid,None)]))
            raise
        self.db.log(uid,action,oid)
        if action=='plan_launch':
            queued=result.get('queued')
            if result.get('planId')!=oid or type(queued) is not int or queued<0:
                message='Запуск плана не подтверждён. Проверь очередь перед повтором.'
            elif queued==0:
                message='Новых публикаций в очередь не добавлено.'
            else:
                message='<b>✓ Публикации поставлены в очередь</b>\n'+f'Добавлено · <code>{number(queued)}</code>'
            return await self.say(uid,message,
                                  keyboard([('▣ Очередь публикаций','owner_show:post_queue','primary')],
                                           [('↶ Публикации','posts',None)]))
        if action.startswith('post_'):
            if result.get('id')!=oid:
                return await self.say(uid,'Результат действия не подтверждён. Обнови карточку публикации.',
                                      self.owner_keyboard([('↶ К публикации','post:'+oid,None)]))
            return await self.post_info(uid,oid,result)
        await self.say(uid,'✓ Операция принята.',keyboard([('↶ Обзор','home',None)]))

    async def admin_api(self,uid:int,text:str)->None:
        """Read-only OpenAPI explorer with the legacy flat argument format."""
        if not self.owner(uid):return
        parts=text.split(maxsplit=2)
        if len(parts)==1:
            return await self.say(uid,'<b>OpenAPI · просмотр</b>\n'
                '<code>/api operationId {"id":"..."}</code>\n\n'
                'Поддерживаются GET-методы с JSON-ответом.')
        selected=next((operation for _,method,operation in declared_operations()
                       if method=='get' and operation['operationId']==parts[1]),None)
        if not selected:return await self.say(uid,'Метод не найден или не разрешён для чтения.')
        try:
            args=json.loads(parts[2]) if len(parts)>2 else {}
            if not isinstance(args,dict):raise ValueError('Параметры должны быть JSON-объектом.')
            path_keys={p['name'] for p in selected.get('parameters',[]) if p['in']=='path'}
            plan=operation_plan(parts[1],{'path':{k:args[k] for k in path_keys if k in args},
                                         'query':{k:v for k,v in args.items() if k not in path_keys}})
            await self.show_api_result(uid,plan)
        except (ValueError,ApiError) as exc:
            await self.say(uid,'Операция отклонена · '+(safe_api_error(exc.code) if isinstance(exc,ApiError) else h(exc)))

    async def show_api_result(self,uid:int,plan:dict)->None:
        result=await self.api.call('GET',plan['path'],params=plan['params'])
        hidden=('authorization','token','password','cookie','secret','address','proxy','url','egressip','credential')

        def scrub(value):
            if isinstance(value,dict):
                return {k:('***' if any(word in k.lower() for word in hidden) else scrub(item))
                        for k,item in value.items()}
            if isinstance(value,list):return [scrub(item) for item in value[:20]]
            return value[:300] if isinstance(value,str) else value

        body=json.dumps(scrub(result),ensure_ascii=False,indent=2).replace('"tokoraLines"','"poolLines"')
        body=SERVICE_NAME.sub('сервис',body)
        await self.say(uid,'<b>▤ '+h(plan['operation'])+'</b>\n<pre>'+h(body[:3350])+'</pre>')

    async def admins(self,uid:int)->None:
        await self.admin_dashboard(uid)

    async def _command_extended(self,uid:int,text:str)->bool:
        cmd=text.split(maxsplit=1)[0].split('@')[0].lower()
        if cmd=='/api':
            if self.owner(uid):await self.admin_api(uid,text)
            return True
        if cmd=='/schedules':await self.schedules(uid);return True
        if cmd=='/templates':await self.templates(uid);return True
        if cmd=='/posts':await self.posts(uid);return True
        if cmd=='/materials':await self.materials(uid);return True
        if cmd in ('/accounts','/pool'):
            await self.accounts(uid)
            return True
        if cmd=='/proxies':
            await self.proxies(uid)
            return True
        if cmd=='/myid':await self.say(uid,'Твой Telegram ID: <code>'+str(uid)+'</code>');return True
        if cmd=='/accountcheck' and self.owner(uid):
            part=text.split()
            if len(part)!=2:return await self.say(uid,'Использование: /accountcheck ACCOUNT_ID') or True
            await self.owner_action(uid,'acc_check',part[1]);return True
        if cmd=='/accountrefresh' and self.owner(uid):
            part=text.split()
            if len(part)!=2:return await self.say(uid,'Использование: /accountrefresh ACCOUNT_ID') or True
            await self.owner_action(uid,'acc_profile',part[1]);return True
        if cmd=='/accountproxy' and self.owner(uid):
            part=text.split()
            if len(part)!=3:return await self.say(uid,'Использование: /accountproxy ACCOUNT_ID PROXY_ID (или - для снятия)') or True
            aid,px=part[1],part[2]
            if not SAFE_ID.fullmatch(aid) or (px!='-' and not SAFE_ID.fullmatch(px)):
                await self.say(uid,'Некорректные идентификаторы.');return True
            await self.set_account_proxy(uid,aid,None if px=='-' else px)
            return True
        if cmd in ('/postretry','/postrefresh','/postcancel') and self.owner(uid):
            part=text.split()
            if len(part)!=2:return await self.say(uid,'Использование: '+h(cmd)+' POST_ID') or True
            if cmd=='/postretry':
                await self.say(uid,'Повтор может создать дубль публикации. Подтвердить?',self.owner_keyboard(
                    [('↻ Повторить публикацию','post_retry_confirm:'+part[1],'danger')],[('↶ Отмена','posts',None)]))
            else:await self.owner_action(uid,'post_refresh' if cmd=='/postrefresh' else 'post_cancel',part[1])
            return True
        return await super().command(uid,text)

    async def _callback_extended(self,uid:int,query:dict)->None:
        action=query.get('data','')
        # Callbacks from old messages are untrusted: all handlers below check
        # current subscriptions and resource ownership again.
        if action.startswith('bot_tasks:'):
            page = action[len('bot_tasks:'):]
            if self.owner(uid) and re.fullmatch(r'[0-9]{1,7}',page):
                await super().tasks(uid,int(page))
            return
        if action.startswith(('tokora_task:','tokora_report:')):
            kind,tid = action.split(':',1)
            return await self.workspace_task(uid,tid,report=kind=='tokora_report')
        if action.startswith('task_accounts:'):
            fields=action.split(':')
            if len(fields)==3 and re.fullmatch(r'[0-9]{1,7}',fields[2]):
                return await self.workspace_task_accounts(uid,fields[1],int(fields[2]))
            return
        if action.startswith('wt:'):
            return await self.workspace_control(uid,action)
        if action.startswith('mo:'):
            return await self.media_order_callback(uid,action)
        if action.startswith('wiz:') or action in ('wiz_continue','wiz_skip','wiz_saved_videos','save_draft_template','submit'):
            return await self.wizard_action(uid,action)
        if action=='settings':return await (self.settings(uid) if self.owner(uid) else self.show_home(uid))
        if action=='materials':return await (self.materials(uid) if self.owner(uid) else self.show_home(uid))
        if action=='tools':return await self.tools_menu(uid)
        if action=='templates':return await self.templates(uid)
        if action.startswith('templates_page:') and action[15:].isdigit():
            return await self.templates(uid,int(action[15:]))
        if action.startswith('source_tpl_page:'):
            fields=action.split(':')
            if len(fields)==3 and fields[2].isdigit() and len(fields[2])<=6:
                return await self.source_template_orders(uid,fields[1],int(fields[2]))
        if action.startswith('source_tpl_use:'):
            fields=action.split(':')
            if len(fields)==3:return await self.use_source_template(uid,fields[1],fields[2])
        if action=='schedules':return await self.schedules(uid)
        if action=='posts':return await self.posts(uid)
        if action=='export_menu':return await self.stats(uid)
        if action=='admin_health':return await self.health(uid)
        if action.startswith('accounts_filter:') and self.owner(uid):
            fields=action.split(':')
            if len(fields)==3 and fields[1] in ('active','expired','error','unknown') and re.fullmatch(r'[0-9]{1,7}',fields[2]):
                return await self.accounts(uid,int(fields[2]),fields[1])
            return
        if action=='noop':return
        if action.startswith('new:'):
            if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
            return await self.begin(uid,action[4:],auto_name=True)
        if action.startswith('owner_show:'):
            key, sep, page = action[11:].partition(':')
            if not sep or re.fullmatch(r'[0-9]{1,7}',page):
                return await self.owner_show(uid,key,int(page) if sep else 0)
            return
        if action.startswith('owner_edit:'):
            return await self.owner_edit(uid,action[11:])
        if action.startswith('owner_go:'):
            return await self.owner_go(uid,action[9:])
        if action.startswith('owner_confirm:'):
            key, sep, nonce = action.removeprefix('owner_confirm:').partition(':')
            if sep and key in DESTRUCTIVE:
                return await self.owner_go(uid,key,nonce)
            return
        if action.startswith('owner_cancel:') and self.owner(uid):
            confirmation = self.consume_owner_confirmation(uid,action.removeprefix('owner_cancel:'))
            if confirmation is None:
                return
            if confirmation['action']=='posttpl_delete':
                return await self.owner_show(uid,'post_templates')
            return await self.show_home(uid)
        if action.startswith('upload:'):
            return await self.upload_intro(uid,action[7:])
        if action.startswith('account:'):
            return await self.account_info(uid,action[8:])
        if action.startswith('draft:'):
            return await self.draft_info(uid,action[6:])
        if action.startswith('draft_plan:') and self.owner(uid):
            oid=action[11:]
            if SAFE_ID.fullmatch(oid):return await self.begin_media_order(uid,oid)
            return
        if action.startswith('caption_') and self.owner(uid):
            return await self.caption_action(uid,action)
        if action.startswith('post:'):
            return await self.post_info(uid,action[5:])
        if action.startswith('photo:') and self.owner(uid):
            tag,oid=action[6:8],action[8:]
            if tag in ('c:','r:') and SAFE_ID.fullmatch(oid):
                await self.say(uid,'<b>▣ Фото</b>\nID: <code>'+h(oid)+'</code>',self.owner_keyboard(
                    [('× Удалить','photo_delete_confirm:'+('r:' if tag=='r:' else 'c:')+oid,'danger')],[('↶ Фото','owner_show:photos_replies' if tag=='r:' else 'owner_show:photos',None)]))
            return
        if action.startswith('plan:'):
            return await self.show_plan(uid,action[5:])
        if action.startswith('tpl:'):
            return await self.template_card(uid,action[4:])
        if action.startswith('tpl_use:'):return await self.from_template(uid,action[8:])
        if action.startswith('tpl_share:') and self.owner(uid):
            tid=action[10:]
            item=self.db.template(uid,tid,owned_only=True)
            if item and self.db.set_template_shared(uid,tid,not item.get('is_shared')):
                self.db.log(uid,'template_share_'+('off' if item.get('is_shared') else 'on'),tid)
            return await self.template_card(uid,tid)
        if action.startswith('tpl_delete_confirm:'):
            tid=action[19:]
            if not self.db.template(uid,tid,owned_only=True):return
            return await self.say(uid,'Удалить шаблон <code>'+h(tid)+'</code>?',keyboard(
                [('× Удалить','tpl_delete:'+tid,'danger')],[('↶ Отмена','templates',None)]))
        if action.startswith('tpl_delete:'):
            self.db.delete_template(uid,action[11:]);return await self.templates(uid)
        if action.startswith('template_task:'):
            tid=action[14:]
            t=self.db.task(tid,uid)
            if not t or not self.is_reusable(t):return
            job=json.loads(t['payload_json'])
            mode=task_order_mode(t['kind'],job)
            if self.mode_restricted(uid,mode):
                return await self.say(uid,'Этот вид заказа доступен только владельцу.')
            job.pop('requestId',None)
            job.pop('name',None)
            job.pop('accountFolder',None)
            job.get('assets',{}).pop('accountIds',None)
            return await self.save_order_template(uid,t['display_name'],mode,{'job':job},'task:'+tid)
        if action.startswith('repeat:'):
            tid=action[7:]
            t=self.db.task(tid,uid)
            if not t:return
            if payload := self.failed_collection_reply(t):
                return await self.retry_collection_reply(uid, payload)
            if not self.is_reusable(t):return
            if self.can_replace_order_links(t):
                return await self.prepare_copy(uid, json.loads(t['payload_json']), t['display_name'])
            if t['state']!='completed':return
            return await self.prepare_copy(uid,json.loads(t['payload_json']),'Повтор · '+t['display_name'][:45])
        if action.startswith('entries:'):
            a=action.split(':')
            if len(a) not in (2,3):return
            if len(a)==3 and not re.fullmatch(r'[0-9]{1,12}',a[2]):return
            offset=int(a[2]) if len(a)==3 else 0
            return await self.task_entries(uid,a[1],offset)
        if action.startswith('exports:'):
            return await self.exports(uid,action[8:])
        if action.startswith('collected:'):
            return await self.collected_videos(uid,action[10:])
        if action.startswith('videos:'):
            fields=action.split(':')
            if len(fields)==2:return await self.order_videos(uid,fields[1])
            if len(fields)==3 and re.fullmatch(r'[0-9]{1,7}',fields[2]):
                return await self.order_videos(uid,fields[1],int(fields[2]))
            return
        if action.startswith('hide_task:'):
            tid=action[10:]
            t=self.db.task(tid,uid)
            if not t or t['state'] not in ('completed','stopped','failed','canceled'):return
            return await self.say(uid,'Убрать задачу из твоего списка? Она сохранится в истории администратора.',
                keyboard([('× Убрать','hide_confirm:'+tid,'danger')],[('↶ Задача','task:'+tid,None)]))
        if action.startswith('hide_confirm:'):
            self.db.hide_task(uid,action[13:])
            return await super().tasks(uid)
        for prefix,mode in [('export_csv:','csv'),('export_links:','links'),('export_texts:','texts')]:
            if action.startswith(prefix):return await self.export_file(uid,action[len(prefix):],mode)
        if action.startswith(('pause:','resume:')):
            if not self.owner(uid):return await self.say(uid,'Паузой общей очереди управляет владелец.')
            action_kind,tid=action.split(':',1)
            t=self.db.task(tid,uid)
            if t and await self.change_task_execution(uid,t,action_kind):return await self.task(uid,tid)
            return
        if action.startswith('post_go:'):
            fields=action.split(':',2)
            if len(fields)==3 and fields[1] in ('cancel','refresh','resolve'):
                return await self.owner_action(uid,'post_'+fields[1],fields[2])
            return
        if action.startswith('acc_go:'):
            fields=action.split(':',2)
            if len(fields)==3 and fields[1] in ('check','profile'):
                return await self.owner_action(uid,'acc_'+fields[1],fields[2])
            return
        # Confirmation must be explicit for globally destructive actions.
        for prefix,internal in [
          ('draft_delete_confirm:','draft_delete'),('plan_delete_confirm:','plan_delete'),
          ('acc_delete_confirm:','acc_delete'),('photo_delete_confirm:','photo_delete'),
          ('post_retry_confirm:','post_retry'),('plan_confirm:','plan_launch')]:
            if action.startswith(prefix) and self.owner(uid):
                oid=action[len(prefix):]
                if internal=='photo_delete':
                    if not (oid.startswith(('r:','c:')) and SAFE_ID.fullmatch(oid[2:])):return
                elif not SAFE_ID.fullmatch(oid):return
                label='Подтвердить изменение общего кабинета'
                detail='ID плана или записи · <code>'+h(oid)+'</code>'
                if internal=='plan_launch':
                    plan=await self.api.call('GET','/v1/posts/plan/'+pth_id(oid),retries=0)
                    if plan.get('id')!=oid:
                        return await self.say(uid,'Не удалось подтвердить ID плана. Обнови карточку.',
                                              self.owner_keyboard([('↻ Обновить план','plan:'+oid,None)]))
                    if not post_plan_ready(plan):
                        return await self.show_plan(uid,oid,plan)
                    label='Поставить публикации в очередь'
                    detail='Будет поставлено в очередь · <b>'+number(plan['postsTotal'])+'</b> публикаций.'
                elif internal=='post_retry':
                    detail+='\nПри повторной публикации существует риск дубля.'
                nonce = self.save_owner_confirmation(uid,internal,oid)
                return await self.say(uid,'<b>'+h(label)+'</b>\n\n'+detail,self.owner_keyboard(
                    [('✓ Подтверждаю',f'owner_do:{internal}:{nonce}','danger' if internal!='plan_launch' else 'success')],
                    [('↶ Отмена','owner_cancel:'+nonce,None)]))
        if action.startswith('owner_do:') and self.owner(uid):
            key, _, nonce = action.removeprefix('owner_do:').partition(':')
            confirmation = self.consume_owner_confirmation(uid,nonce,action=key)
            if confirmation is None:
                return
            oid = confirmation['id']
            if key=='photo_delete':
                target,oid=oid[:2],oid[2:]
                if target not in ('r:','c:') or not SAFE_ID.fullmatch(oid):return
                await self.api.call('DELETE','/v1/photos/'+pth_id(oid),params={'target':'replies' if target=='r:' else 'comments'},retries=0)
                self.db.log(uid,'photo_delete',oid)
                return await self.say(uid,'✓ Фото удалено.')
            return await self.owner_action(uid,key,oid)
        return await super().callback(uid,query)

    async def handle(self,update:dict)->None:
        msg=update.get('message') or {}
        callback=update.get('callback_query') or {}
        chat=(msg or callback.get('message') or {}).get('chat',{})
        if chat.get('type')=='private':
            actor=(msg or callback).get('from') or {}
        else:
            actor={}
        if actor.get('id'):
            self.db.touch_user(int(actor['id']),actor.get('first_name',''),actor.get('last_name',''),actor.get('username',''))
        if chat.get('type')=='private' and (msg.get('photo') or msg.get('document') or msg.get('video')):
            uid=int(msg['from']['id'])
            self.db.register(uid)
            if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
            if await self.handle_upload(uid,msg):return
        return await super().handle(update)

    async def command(self,uid:int,text:str)->bool:
        if await self.admin_command(uid,text):return True
        cmd=text.split(maxsplit=1)[0].lower().split('@')[0]
        if cmd=='/cancel' and self.owner(uid):self.account_imports.clear_preview()
        if cmd=='/promo':
            fields=text.split()
            if len(fields)!=2 or not re.fullmatch(r'[a-fA-F0-9]{16}',fields[1]):
                await self.say(uid,'Введи <code>/promo КОД</code>.')
                return True
            expiry=self.db.redeem_promo(uid,fields[1])
            await self.say(uid,'✓ Промокод активирован. Подписка до '+self.format_date(expiry)+'.' if expiry else
                           'Промокод недействителен или уже использован.',keyboard([('↶ Обзор','home',None)]))
            return True
        if cmd=='/op':
            if not self.owner(uid):return True
            parts=text.split(maxsplit=2)
            if len(parts)<2:
                await self.say(uid,'<b>▣ Расширенные операции OpenAPI</b>\n\n'
                    'Только администратор. Команда:\n'
                    '<code>/op getPostsById {"path":{"id":"POST_ID"}}</code>\n\n'
                    'Для изменения общего кабинета требуется отдельное подтверждение. '\
                    'Запуск задач и расписания выполняются через обычный интерфейс.')
                return True
            try:
                data=json.loads(parts[2]) if len(parts)>2 else {}
                plan=operation_plan(parts[1],data)
                if plan['method']=='GET':
                    await self.show_api_result(uid,plan)
                else:
                    import secrets
                    code=secrets.token_hex(4)
                    self.db.save_draft(uid,'api_op','confirm',{'nonce':code,'plan':plan})
                    await self.say(uid,'<b>Подтверди операцию API</b>\n\n'
                        'Метод: <code>'+h(plan['method'])+'</code>\n'
                        'Операция: <code>'+h(plan['operation'])+'</code>\n'
                        'Путь: <code>'+h(plan['path'])+'</code>\n\n'
                        'Изменения затронут общий кабинет и могут быть необратимы.',keyboard(
                           [('✓ Подтверждаю','op_confirm:'+code,'danger')],[('× Отмена','discard',None)]))
            except (ValueError,ApiError) as exc:
                await self.say(uid,'Операция отклонена: '+(safe_api_error(exc.code) if isinstance(exc,ApiError) else h(exc)))
            return True
        return await self._command_extended(uid,text)

    async def callback(self,uid:int,query:dict)->None:
        message_id = (query.get('message') or {}).get('message_id')
        if type(message_id) is int:
            await self.live_cards.leave_message(uid,message_id)
        data=str(query.get('data',''))
        if len(data.encode('utf-8'))>64:return
        if data.startswith('route:'):
            if not self.owner(uid):return
            data=self.db.owner_route(uid,data)
            if not data:
                return await self.say(uid,'<b>↻ Этот экран устарел</b>\nОткрой нужный раздел заново.',
                                      keyboard([('↶ Панель владельца','admin',None)]))
            query={**query,'data':data}
        if await self.admin_callback(uid,data):return
        if await self.account_console_callback(uid,data):return
        if await self.proxy_console_callback(uid,data):return
        if await self.pool_checks.callback(uid,data):return
        if await self.local_template_callback(uid,data):return
        if data.startswith('acctimp:'):return await self.account_imports.callback(uid,data)
        if data=='promo':
            return await self.say(uid,'<b>◇ Промокод</b>\n\nОтправь <code>/promo КОД</code>.',
                                  keyboard([('↶ Подписка','access',None)]))
        if data=='promo_create':
            if not self.owner(uid):return
            self.db.save_draft(uid,'promo_create','days',{})
            return await self.say(uid,'<b>◇ Создать одноразовый промокод</b>\n\nВведи срок подписки от 1 до 36500 дней.',
                                  keyboard([('× Отмена','discard','danger')]))
        if data.startswith('op_confirm:'):
            if not self.owner(uid):return
            d=self.db.draft(uid)
            if not d or d['mode']!='api_op' or d['step']!='confirm' or d['data']['nonce']!=data[11:]:return
            if time.time()-d['updated_at']>600:
                self.db.clear_draft(uid)
                return await self.say(uid,'Подтверждение истекло. Открой операцию снова.')
            plan=d['data']['plan']
            # The operation path and request shape were validated by /op
            # and persisted locally; callbacks never provide arbitrary paths.
            spec_plan=plan
            # The plan was validated and persisted by /op. Only this owner can confirm.
            self.db.clear_draft(uid)
            operation = next((op for _,_,op in declared_operations() if op['operationId']==plan['operation']),None)
            if operation is None or operation_channel(operation) != 'op/cli':
                return await self.say(uid,'Операция доступна через её отдельный экран или серверную CLI.')
            try:
                await self.api.call(spec_plan['method'],spec_plan['path'],payload=spec_plan['payload'],params=spec_plan['params'],retries=0)
                self.db.log(uid,'op_'+spec_plan['operation'],spec_plan['path'])
                return await self.say(uid,'✓ Операция выполнена: <code>'+h(plan['operation'])+'</code>')
            except ApiError as exc:
                return await self.say(uid,'Результат не подтверждён: '+safe_api_error(exc.code)+
                    '\nПри сетевом сбое не повторяй операцию вслепую — сначала проверь состояние.')
        if data.startswith('preset:') and self.owner(uid):
            try:i=int(data[7:])
            except ValueError:return
            return await self.preset_actions(uid,i)
        if data.startswith('preset_apply:'):
            return await self.preset_apply_or_delete(uid,data[13:])
        if data.startswith('preset_delete_confirm:') and self.owner(uid):
            nonce=data[22:]
            d=self.db.draft(uid)
            if d and d['mode']=='preset_action' and d['data']['nonce']==nonce:
                return await self.say(uid,'Удалить пресет <b>'+h(d['data']['name'])+'</b>?',keyboard(
                    [('× Удалить','preset_delete:'+nonce,'danger')],[('↶ Отмена','owner_show:presets',None)]))
            return
        if data.startswith('preset_delete:'):
            return await self.preset_apply_or_delete(uid,data[14:],remove=True)
        if data.startswith('posttpl:') and self.owner(uid):
            return await self.post_template_info(uid,data[8:])
        if data.startswith('posttpl_delete_confirm:') and self.owner(uid):
            oid=data[23:]
            if not SAFE_ID.fullmatch(oid):return
            nonce = self.save_owner_confirmation(uid,'posttpl_delete',oid)
            return await self.say(uid,'Удалить шаблон подписи <code>'+h(oid)+'</code>?',self.owner_keyboard(
                [('× Удалить','posttpl_delete:'+nonce,'danger')],[('↶ Отмена','owner_cancel:'+nonce,None)]))
        if data.startswith('posttpl_delete:') and self.owner(uid):
            confirmation = self.consume_owner_confirmation(uid,data.removeprefix('posttpl_delete:'),action='posttpl_delete')
            if confirmation is None:
                return
            oid = confirmation['id']
            await self.api.call('DELETE','/v1/posts/templates/'+pth_id(oid),retries=0)
            self.db.log(uid,'posttpl_delete',oid)
            return await self.say(uid,'✓ Шаблон подписи удалён.')
        if data.startswith('source_reply:'):
            if self.mode_restricted(uid,'source_reply'):
                return await self.say(uid,'Этот вид заказа доступен только владельцу.')
            tid=data[len('source_reply:'):]
            source=self.db.task(tid,uid)
            if not self.is_reply_source(source):
                return await self.say(uid,'Доступен только завершённый собственный сбор.')
            if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
            self.save_wizard_step(uid,'source_reply','target_count',{'source_task':tid})
            return await self.source_reply_field(uid)
        if data.startswith('source:') or data in ('source_reply_submit','source_reply_skip'):
            return await self.source_reply_action(uid,data)
        return await self._callback_extended(uid,query)

    async def input_draft(self,uid:int,text:str)->None:
        caption=self.db.draft(uid)
        if caption and caption['mode']=='caption_template':
            if not self.owner(uid):return
            data=caption['data'];step=caption['step']
            limits={'name':60,'description':2200,'hashtags':500}
            if not text.strip() or len(text)>limits[step]:
                return await self.say(uid,'Введи непустой текст, максимум '+str(limits[step])+' символов.')
            data[step]='' if step=='hashtags' and text.strip()=='-' else text.strip()
            if step!='hashtags':
                next_step='description' if step=='name' else 'hashtags'
                self.db.save_draft(uid,'caption_template',next_step,data)
                return await self.say(uid,'Введи текст подписи.' if next_step=='description' else 'Введи хештеги или «-» без хештегов.')
            try:await self.api.call('POST','/v1/posts/templates',payload=data,retries=0)
            except ApiError as exc:return await self.say(uid,safe_api_error(exc.code))
            self.db.clear_draft(uid)
            return await self.say(uid,'✓ Шаблон подписи создан.',keyboard([('▧ Подписи','owner_show:post_templates',None)]))
        if await self.media_order_input(uid,text):return
        if await self.local_template_input(uid,text):return
        if await self.account_console_input(uid,text):return
        if await self.admin_input(uid,text):return
        d=self.db.draft(uid)
        if d and d['mode']=='task_copy':return await self.copy_preview(uid)
        if d and d['mode']=='promo_create':
            if not self.owner(uid):return
            try:
                code=self.db.issue_promo(uid,int(text.strip()))
            except ValueError:
                return await self.say(uid,'Укажи целое число от 1 до 36500.')
            self.db.clear_draft(uid)
            rich=(f'<h2>✓ Промокод создан</h2><p><code>/promo {code}</code></p>'
                  f'<tg-button-row><tg-button type="copy_text" text="/promo {code}">▤ Скопировать команду</tg-button></tg-button-row>'
                  '<footer>Действует 7 дней. Активируется один раз.</footer>')
            plain=(f'<b>✓ Промокод создан</b>\n\n<code>/promo {code}</code>'
                   '\n\nДействует 7 дней. Активируется один раз.')
            return await self.tg.rich(uid,rich,plain,keyboard([('↶ Панель','admin',None)]))
        if d and d['mode']=='source_reply':
            if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
            data=d['data']
            try:
                if d['step']=='target_count':
                    count=int(text.strip())
                    if not 1 <= count <= MAX_COLLECTED_REPLY_TARGETS:
                        raise ValueError(f'Укажи количество от 1 до {MAX_COLLECTED_REPLY_TARGETS}.')
                    data['target_count']=count
                    data.pop('target_links',None)
                    data.pop('target_ids',None)
                    data.pop('target_cids',None)
                    data['options']={key:value for key,value in (data.get('options') or {}).items() if key!='targetCount'}
                    next_step='review' if data.pop('review_edit',False) else 'options' if data.get('replies') else 'replies'
                    self.save_wizard_step(uid,'source_reply',next_step,data)
                    return await self.source_reply_field(uid)
                if d['step']=='replies':
                    data['replies']=limited_lines(text,100,700)
                    self.save_wizard_step(uid,'source_reply','options',data)
                    return await self.source_reply_field(uid)
                if d['step']=='options':
                    selected=next((item for item in order_fields('source_reply',data) if item.key==data.get('form_field')),None)
                    if selected:
                        data=set_field(data,selected,parse_field(selected,text))
                        data.pop('form_field',None)
                        self.save_wizard_step(uid,'source_reply','options',data)
                        return await self.source_reply_field(uid)
                    data=parse_volume_message('source_reply',data,text)
                    self.save_wizard_step(uid,'source_reply','options',data)
                    return await self.source_reply_field(uid)
                return await self.source_reply_preview(uid)
            except ValueError as exc:return await self.source_reply_field(uid,error=str(exc))
        return await self._input_draft_extended(uid,text)

    async def source_reply_action(self,uid:int,action:str):
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        draft=self.db.draft(uid)
        if not draft or draft['mode']!='source_reply':return await self.tasks(uid)
        step,data=draft['step'],draft['data']
        _,_,suffix=action.partition(':')
        verb,_,token=suffix.partition(':')
        if not token or token!=data.get('wizard_token'):
            return await self.source_reply_field(uid)
        if verb.startswith('tplpage_') and step=='replies' and verb[8:].isdigit():
            return await self.source_reply_field(uid,page=int(verb[8:]))
        if verb.startswith('tpl_') and step=='replies':
            item=self.db.template(uid,verb[4:])
            values=template_values(item) if item else {}
            if not values.get('replies') or 'replies' in values.get('disabled_fields',[]):
                return await self.source_reply_field(uid,error='Шаблон ответов недоступен.')
            data['replies']=values['replies']
            self.save_wizard_step(uid,'source_reply','options',data)
            return await self.source_reply_field(uid)
        if step=='options' and await self.order_form_action(uid,'source_reply',data,verb,prefix='source'):return
        if verb=='cancel':
            self.db.clear_draft(uid)
            return await self.task(uid,data['source_task'])
        if verb=='keep_replies' and step=='replies' and data.get('replies'):
            self.save_wizard_step(uid,'source_reply','options',data)
            return await self.source_reply_field(uid)
        if verb=='back':
            previous={'replies':'target_count','options':'replies','review':'options'}.get(step)
            if step=='target_count' and data.pop('review_edit',False):previous='review'
            if previous is None:return
            self.save_wizard_step(uid,'source_reply',previous,data)
            return await self.source_reply_field(uid)
        if verb=='skip' and step=='options':
            data=complete_order_form('source_reply',data)
            data.pop('form_field',None)
            self.save_wizard_step(uid,'source_reply','review',data)
            return await self.source_reply_preview(uid)
        if verb=='submit' and step=='review':return await self.submit_source_reply(uid)
        if verb=='edit_targets' and step=='review':
            self.save_wizard_step(uid,'source_reply','target_count',{**data,'review_edit':True})
            return await self.source_reply_field(uid)
        if verb=='photo' and step=='review' and self.allow_photos(uid):
            setting='off' if data.get('photo_mode')=='replies' else 'replies'
            self.save_wizard_step(uid,'source_reply','review',{**data,'photo_mode':setting})
            return await self.source_reply_preview(uid)
        if verb=='template' and step=='review':
            saved={key:data[key] for key in ('replies','options','photo_mode') if key in data}
            return await self.save_order_template(uid,'Ответы на собранное','source_reply',saved,'source:review:'+token)
        if verb=='review' and step=='review':return await self.source_reply_preview(uid)

    async def source_template_orders(self,uid:int,tid:str,page:int=0)->None:
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        if self.mode_restricted(uid,'source_reply'):
            return await self.say(uid,'Этот вид заказа доступен только владельцу.')
        item=self.db.template(uid,tid)
        if not item or item['mode']!='source_reply':return await self.say(uid,'Шаблон не найден.')
        size=10
        sources=self.db.completed_collections(uid,size+1,page*size)
        rows=[[(f'▤ {source["display_name"][:38]} · {source["task_id"]}',
                f'source_tpl_use:{tid}:{source["task_id"]}',None)] for source in sources[:size]]
        rows.extend([pagination(f'source_tpl_page:{tid}:',page,len(sources)>size),
                     [('↶ Шаблоны','templates',None)]])
        hint='Выбери завершённый сбор комментариев.' if sources else 'На этой странице нет завершённых сборов.'
        rich,plain=screen('↪ Источник для ответов',subtitle=hint)
        await self.tg.rich(uid,rich,plain,keyboard(*rows))

    async def use_source_template(self,uid:int,tid:str,source_id:str)->None:
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        if self.mode_restricted(uid,'source_reply'):
            return await self.say(uid,'Этот вид заказа доступен только владельцу.')
        item=self.db.template(uid,tid)
        source=self.db.task(source_id,uid)
        if not item or item['mode']!='source_reply' or not self.is_reply_source(source):
            return await self.say(uid,'Шаблон или исходный сбор недоступен.')
        if not self.has_order_pool(uid):return await self.create(uid)
        data=json.loads(item['payload'])
        self.save_wizard_step(uid,'source_reply','target_count',{**data,'source_task':source_id})
        await self.source_reply_field(uid)

    @staticmethod
    def is_reply_source(source:dict|None)->bool:
        return bool(source and source['kind']=='fetch' and source['state']=='completed' and source['upstream_id'])

    async def retry_collection_reply(self, uid: int, payload: dict) -> None:
        if not self.db.is_active(uid, self.cfg.owner_id):
            return await self.access(uid)
        if self.mode_restricted(uid,'source_reply'):
            return await self.say(uid,'Этот вид заказа доступен только владельцу.')
        source = self.db.task_by_upstream(payload['sourceJob'], uid)
        if not self.is_reply_source(source):
            return await self.say(uid, 'Исходный сбор недоступен.')
        self.save_wizard_step(uid, 'source_reply', 'review', {
            'source_task': source['task_id'],
            'target_count': len(payload['targetIds']),
            'target_ids': payload['targetIds'],
            'replies': payload['assets']['replyTexts'],
            'options': payload.get('settings', {}),
            'photo_mode': 'replies' if self.allow_photos(uid) and payload.get('usePhoto') else 'off',
        })
        await self.source_reply_preview(uid)

    async def source_reply_field(self,uid:int,*,error:str='',page:int=0):
        draft=self.db.draft(uid)
        if not draft or draft['mode']!='source_reply':return
        step=draft['step']
        if step=='review':return await self.source_reply_preview(uid)
        token=self.wizard_token(draft)
        data=draft['data']
        if step=='options':
            return await self.order_form_field(uid,'source_reply',data,token,error=error,prefix='source')
        if step=='target_count':
            text='<b>↪ Ответить на собранные комментарии</b>\n\nНа сколько комментариев ответить? Введи число от 1 до 100 000. Учитываются только комментарии верхнего уровня.'
            rows=[[('↶ К сбору','source:cancel:'+token,None)]]
            if data.get('review_edit'):
                rows.insert(0,[('↶ К проверке','source:back:'+token,None)])
        elif step=='replies':
            text='<b>↪ Ответы на собранное</b>\nМатериалы · Тексты ответов\n\nОтправь тексты сообщением или TXT, один ответ на строку.'
            items=self.matching_order_templates(uid,'source_reply')
            size=10
            page=max(0,min(page,(len(items)-1)//size))
            rows=[[(f'▧ {item["name"][:40]}','source:tpl_'+item['template_id']+':'+token,None)]
                  for item in items[page*size:(page+1)*size]]
            nav=[]
            if page:nav.append(('← Ранее',f'source:tplpage_{page-1}:{token}',None))
            if (page+1)*size<len(items):nav.append(('→ Далее',f'source:tplpage_{page+1}:{token}',None))
            rows.append(nav)
            if data.get('replies'):rows.insert(0,[('✓ Оставить тексты','source:keep_replies:'+token,'primary')])
            rows.append([('↶ К количеству','source:back:'+token,None),('× Отменить','source:cancel:'+token,None)])
        else:
            text=('<b>↪ Ответы на собранное</b>\nШаг 2 из 2 · Настройки\n\n'
                  'Выбери количество ответов кнопками.')
            rows=[[('→ Стандартные','source:skip:'+token,'primary')],
                  [('↶ Назад','source:back:'+token,None),('× Отменить','source:cancel:'+token,'danger')]]
        if error:text+='\n\n<b>! Проверь ввод</b>\n'+h(error)
        rich,_=screen('↪ Ответы на собранное',stage=1)
        if step=='target_count':
            rich+='<h3>Количество комментариев</h3><p>Введи число от <b>1</b> до <b>100 000</b>. Учитываются только комментарии верхнего уровня.</p>'
            if data.get('target_count'):
                count=number(data['target_count'])
                rich+='<p>Текущее количество · <b>'+count+'</b></p>'
                text+='\n\nТекущее количество · <b>'+count+'</b>'
        else:
            rich+='<h3>Тексты ответов</h3><p>Отправь тексты сообщением или TXT, один ответ на строку. Можно выбрать шаблон ниже.</p>'
        if error:rich+='<blockquote><b>Проверь ввод</b><br>'+h(error)+'</blockquote>'
        await self.tg.rich(uid,rich,text,keyboard(*rows))

    async def source_reply_preview(self,uid:int):
        d=self.db.draft(uid)
        if not d or d['mode']!='source_reply':return
        d['data']=complete_order_form('source_reply',d['data'])
        if not reply_quantity(d['data']):
            self.save_wizard_step(uid,'source_reply','options',d['data'])
            return await self.source_reply_field(uid,error='Укажи хотя бы один ответ на комментарий.')
        self.db.save_draft(uid,'source_reply','review',d['data'])
        src=self.db.task(d['data']['source_task'],uid)
        if not self.is_reply_source(src):return await self.say(uid,'Исходный сбор недоступен.')
        pool = await self.review_order_accounts(uid,d)
        if pool is None:return
        ids,pool_label = pool
        token=self.wizard_token(d)
        rows=[('Сбор',src['task_id']),('Текстов',len(d['data']['replies'])),(pool_label,len(ids)),
              *form_rows('source_reply',d['data'])]
        with_photo=self.allow_photos(uid) and d['data'].get('photo_mode')=='replies'
        if self.allow_photos(uid):rows.append(('Фото из материалов','В ответы' if with_photo else 'Без фото'))
        target_count=int(d['data'].get('target_count') or 100)
        selected=d['data'].get('target_cids')
        try:
            if not valid_collection_targets(selected):
                previous=d['data'].get('target_ids') or d['data'].get('target_links') or None
                selected=await collected_reply_targets(self.api,src['upstream_id'],
                    len(previous) if previous else target_count,previous=previous)
        except ApiError as exc:
            return await self.say(uid,'Не удалось прочитать сбор. '+safe_api_error(exc.code),keyboard([('↻ Повторить','wiz_continue',None)]))
        except ValueError as exc:
            return await self.say(uid,h(exc),keyboard([('✎ Выбрать заново','source:edit_targets:'+token,None)],
                [('↶ К сбору','task:'+src['task_id'],None)]))
        if not selected:return await self.say(uid,'В сборе нет комментариев с доступными ID для ответов.',keyboard([('↶ К сбору','task:'+src['task_id'],None)]))
        d['data']['target_cids']=selected
        d['data'].pop('target_links',None)
        d['data'].pop('target_ids',None)
        self.db.save_draft(uid,'source_reply','review',d['data'])
        rows.append(('Комментариев для ответов',len(selected)))
        plan_rejected=False
        try:
            plan=await self.api.call('POST','/v1/tasks/'+pth_id(src['upstream_id'])+'/reply-plan',
                                     payload={'targetIds':selected,'withPhotos':with_photo},retries=0)
        except ApiError as exc:
            plan_rejected=exc.code in ('invalid_source','missing_source','invalid_target','empty_job')
            if not plan_rejected:rows.append(('План сервиса','Временно недоступен'))
        else:
            plan_rejected=type(plan.get('selected')) is int and plan['selected']!=len(selected)
            if type(plan.get('videos')) is int:rows.append(('Видео в плане',plan['videos']))
        if plan_rejected:
            d['data'].pop('target_cids',None)
            d['data']['target_ids']=selected
            self.save_wizard_step(uid,'source_reply','review',d['data'])
            token=self.wizard_token(self.db.draft(uid))
            return await self.say(uid,'Не удалось подтвердить выбранные комментарии из сбора.',keyboard(
                [('✎ Выбрать заново','source:edit_targets:'+token,None)],
                [('↶ К сбору','task:'+src['task_id'],None)]))
        limit_min=task_execution_limit_seconds({'sourceJob':src['upstream_id'],'targetIds':selected})//60
        per_video_cap=TASK_LIMITS['repliesPerVideo']
        replies_per_comment=(d['data'].get('options') or {}).get('repliesPerComment') or 0
        if len(selected)*replies_per_comment>per_video_cap:
            rows.append(('Лимит ответов на видео',number(per_video_cap)))
        rich,plain=screen('↪ Проверка ответов',rows,stage=3,
                          caption=f'Ответы появятся под выбранными комментариями. Лимит выполнения — {minutes_label(limit_min)} с начала публикаций.')
        preview='\n'.join(text[:160] for text in d['data']['replies'][:5])+f'\nПоказано {min(5,len(d["data"]["replies"]))} из {len(d["data"]["replies"])} вариантов.'
        rich+='<h3>Ответы, которые будут использованы</h3><p>'+h(preview)+'</p>'
        plain+='\n\nОтветы, которые будут использованы\n'+h(preview)
        await self.tg.rich(uid,rich,plain,keyboard(
            [('✓ Поставить в очередь','source:submit:'+token,'success')],
            [('✎ Количество комментариев','source:edit_targets:'+token,None)],
            [('▧ Убрать фото' if with_photo else '▧ Фото в ответы','source:photo:'+token,None)] if self.allow_photos(uid) else [],
            [('▧ Сохранить шаблон','source:template:'+token,None)],
            [('↶ Назад','source:back:'+token,None),('× Отменить','source:cancel:'+token,'danger')]))

    async def submit_source_reply(self,uid:int):
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        if self.mode_restricted(uid,'source_reply'):
            return await self.say(uid,'Этот вид заказа доступен только владельцу.')
        d=self.db.draft(uid)
        if not d or d['mode']!='source_reply' or d['step']!='review':return
        src=self.db.task(d['data']['source_task'],uid)
        if not self.is_reply_source(src):return await self.say(uid,'Исходный сбор недоступен.')
        data=complete_order_form('source_reply',d['data'])
        if not self.allow_photos(uid):data={**data,'photo_mode':'off'}
        try:
            options={key:value for key,value in (data.get('options') or {}).items() if key!='targetCount'}
            if not options.get('repliesPerComment'):raise ValueError('Укажи хотя бы один ответ на комментарий.')
            if not valid_collection_targets(data.get('target_cids')):
                return await self.source_reply_preview(uid)
            ids=self.confirmed_order_accounts(uid,d)
            if not ids:return await self.say(uid,'Нет назначенных аккаунтов.')
            targets=data['target_cids']
            payload={'sourceJob':src['upstream_id'],'targetIds':targets,
                     'assets':{'accountIds':ids,'replyTexts':data['replies']}}
            folder=self.confirmed_order_folder(uid,d)
            if folder is not None:
                payload['accountFolder']=folder
            payload.update(task_photo_fields('source_reply',data))
            options.update(delaySeconds=2,uniqueText=False,
                           repliesPerVideo=min(TASK_LIMITS['repliesPerVideo'],len(targets)*options['repliesPerComment']))
            payload['settings']=validate_task_options(options)
            tid=self.db.queue_task(uid,'Ответы · '+src['display_name'][:45],'publish',payload,
                max_daily=self.cfg.max_tasks_per_day,max_open=self.cfg.max_open_tasks,draft_token=self.wizard_token(d))
            return await self.task(uid,tid)
        except ValueError as exc:return await self.say(uid,'Нельзя создать: '+h(exc),keyboard(
            [('↻ К проверке','wiz_continue','primary')]))

    async def submit(self,uid:int)->None:
        d=self.db.draft(uid)
        if d and d['mode']=='source_reply':return await self.submit_source_reply(uid)
        return await self._submit_extended(uid)

    async def preset_actions(self,uid:int,index:int):
        if not self.owner(uid) or index<0:return
        data=await self.api.call('GET','/v1/presets')
        items=preset_items(data)
        if index>=len(items):return await self.say(uid,'Список изменился. Открой пресеты снова.')
        preset=items[index]
        name=str(preset.get('name') or '')
        if not name:return
        import secrets
        nonce=secrets.token_hex(4)
        builtin=bool(preset.get('builtin'))
        self.db.save_draft(uid,'preset_action','confirm',{'name':name,'nonce':nonce,'builtin':builtin})
        rich,plain=table_html('▧ Пресет: '+name,[
            ('Комментарии',preset.get('commentTextCount',0)),
            ('Ответы',preset.get('replyTextCount',0)),
            ('Фото комментариев',preset.get('commentPhotoCount',0)),
            ('Фото ответов',preset.get('replyPhotoCount',0))])
        rows=[[('✓ Применить','preset_apply:'+nonce,'success')]]
        if not builtin:rows.append([('× Удалить','preset_delete_confirm:'+nonce,'danger')])
        rows.append([('↶ Пресеты','owner_show:presets',None)])
        await self.tg.rich(uid,rich,plain,keyboard(*rows))

    async def preset_apply_or_delete(self,uid:int,nonce:str,remove:bool=False):
        if not self.owner(uid):return
        d=self.db.draft(uid)
        if not d or d['mode']!='preset_action' or d['data']['nonce']!=nonce:return
        if remove and d['data'].get('builtin'):return
        name=d['data']['name']
        self.db.clear_draft(uid)
        if remove:
            result=await self.api.call('DELETE','/v1/presets',payload={'name':name},retries=0)
        else:
            result=await self.api.call('POST','/v1/presets/apply',payload={'name':name},retries=0)
        self.db.log(uid,'preset_delete' if remove else 'preset_apply',name)
        await self.say(uid,'✓ Пресет '+('удалён' if remove else 'применён')+': '+h(name))

    async def post_template_info(self,uid:int,oid:str):
        if not self.owner(uid):return
        data=await self.api.call('GET','/v1/posts/templates')
        for item in select_items(data):
            if str(item.get('id'))==oid:
                return await self.say(uid,'<b>▧ Шаблон подписи</b>\n\n'
                    '├ Имя: '+h(item.get('name',''))+'\n'
                    '├ Описание: '+h(str(item.get('description',''))[:1000])+'\n'
                    '└ Теги: '+h(str(item.get('hashtags',''))[:400]),self.owner_keyboard(
                    [('× Удалить','posttpl_delete_confirm:'+oid,'danger')],
                    [('↶ Шаблоны','owner_show:post_templates',None)]))
        await self.say(uid,'Шаблон не найден.')

    async def caption_action(self,uid:int,action:str)->None:
        route,_,value=action.partition(':')
        if route=='caption_apply':
            draft=self.db.draft(uid)
            if not draft or draft['mode']!='caption' or draft['data']['wizard_token']!=value:return
            if time.time()-draft['updated_at']>600:
                self.db.clear_draft(uid)
                return await self.say(uid,'Подтверждение истекло. Выбери шаблон заново.')
            data=draft['data']
            await self.api.call('PATCH','/v1/posts/drafts/'+pth_id(data['id']),payload={**data['body'],'ownText':True},retries=0)
            self.db.clear_draft(uid)
            self.db.log(uid,'post_caption_apply',data['id'])
            return await self.draft_info(uid,data['id'])
        oid,_,key=value.partition(':')
        if not SAFE_ID.fullmatch(oid):return
        response=await self.api.call('GET','/v1/posts/templates')
        templates=[item for item in select_items(response) if SAFE_ID.fullmatch(str(item.get('id','')))]
        if route=='caption_pick' and key.isdigit():
            size=10
            page=max(0,min(int(key),(len(templates)-1)//size))
            rows=[[(f'▧ {str(item.get("name") or item["id"])[:45]}',
                    f'caption_preview:{oid}:{item["id"]}',None)] for item in templates[page*size:(page+1)*size]]
            rows.extend([pagination(f'caption_pick:{oid}:',page,len(templates)>(page+1)*size),
                         [('↶ Черновик','draft:'+oid,None)]])
            text='Выбери подпись для черновика.' if templates else 'Шаблонов подписей пока нет.'
            return await self.say(uid,'<b>▧ Шаблоны подписей</b>\n\n'+text,self.owner_keyboard(*rows))
        if route=='caption_preview':
            item=next((item for item in templates if item['id']==key),None)
            if item is None:return await self.say(uid,'Шаблон не найден.')
            body={field:item[field] for field in ('description','hashtags') if isinstance(item.get(field),str)}
            if not body:return await self.say(uid,'В шаблоне нет подписи или хештегов.')
            self.save_wizard_step(uid,'caption','confirm',{'id':oid,'body':body})
            token=self.db.draft(uid)['data']['wizard_token']
            rich,plain=table_html('▧ Подпись для черновика',[
                (label,body[key][:900]) for key,label in (('description','Описание'),('hashtags','Хештеги')) if key in body])
            return await self.tg.rich(uid,rich,plain,self.owner_keyboard(
                [('✓ Заменить подпись','caption_apply:'+token,'success')],
                [('↶ Шаблоны','caption_pick:'+oid+':0',None)]))
