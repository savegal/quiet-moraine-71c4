"""Proxima Soft full interface: independent per-user jobs, branded reports,
Telegram Rich Messages, media uploads and owner-only shared workspace actions.

The provider workspace is shared; accounts, proxies, materials, post drafts and
settings are NEVER exposed for member mutations across user boundaries.
"""
from __future__ import annotations
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
from .bot import BotApp, safe_api_error
from .admin_panel import AdminPanelMixin, csv_safe
from .catalog import (
    MODES, OWNER_ONLY, MODE_KIND, limited_lines, parse_targets, parse_options, parse_feedwarm,
    parse_comment_links, build_payload, validate_task_options, validate_feedwarm, is_tiktok_url,
)
from .api import ApiError, TelegramError
from .db import OPEN_STATES
from .live_cards import LiveCards
from .operations import declared_operations, load_spec, operation_plan, valid_body
from .presentation import MODE_SYMBOLS, h, keyboard, number, pagination, result_status, task_view, task_rich, table_html, rich_table

SAFE_ID=re.compile(r'^[a-zA-Z0-9_-]{1,128}$')

# Explicit UI operations. Each path exists in bundled OpenAPI; placeholders are
# filled only from server-validated IDs, never arbitrary URL input.
ADMIN_ACTIONS={
    'accounts_check':('POST','/v1/accounts/check',{}),
    'accounts_check_stop':('DELETE','/v1/accounts/check',None),
    'proxies_check':('POST','/v1/proxies/check',{}),
    'proxies_check_stop':('DELETE','/v1/proxies/check',None),
    'posts_pause':('POST','/v1/posts/queue/pause',{}),
    'posts_resume':('POST','/v1/posts/queue/resume',{}),
    'posts_refresh':('POST','/v1/posts/queue/refresh',{}),
    'posts_cancel_queued':('POST','/v1/posts/queue/cancel-queued',{}),
    'posts_clear_history':('POST','/v1/posts/queue/clear-history',{}),
}
# Destructive actions require a two-step confirmation rather than trusting a
# stale callback or freeform user text.
DESTRUCTIVE={'accounts_check_stop','proxies_check_stop','posts_cancel_queued','posts_clear_history'}
PUBLICATION_REPORT_KINDS={'publish','keyword_reply','commentlike'}

PROMPTS={
 'name':'Введи короткое название, чтобы найти заказ в списке. До 60 символов.',
 'videos':'Отправь ссылки TikTok или ID видео сообщением либо UTF-8 TXT. Одна ссылка или ID на строку, до 100 строк. Подойдут обычные и короткие ссылки.',
 'comments':'Отправь тексты комментариев сообщением или TXT. Один текст на строку, до 100 строк.',
 'replies':'Отправь тексты ответов сообщением или TXT. Один текст на строку, до 100 строк.',
 'reply_links':'Пришли ссылки на комментарии TikTok или пары «ID видео:ID комментария». До 300 строк, сообщением или UTF-8 TXT.',
 'like_links':'Пришли ссылки на комментарии TikTok или пары «ID видео:ID комментария». До 300 строк, сообщением или UTF-8 TXT.',
 'likes':'Сколько лайков необходимо на комментарий? Целое число 1–100.',
 'keywords':'Пришли поисковые ключевые фразы, по одной в строке (до 100).',
 'warm_json':'Отправь JSON с параметрами feedWarm. Пример: <code>{"keywords":["обзор телефона"],"search":true,"dryRun":true,"perAccount":1}</code>',
 'options':'Оставь стандартные настройки или отправь JSON. Например, <code>{"commentsPerVideo":1,"repliesPerVideo":2}</code>.',
 'when':'Поставь задачу в очередь сейчас или укажи дату и время в формате <code>ДД.ММ.ГГГГ ЧЧ:ММ</code> ({zone}).',
}

STEP_LABELS={
    'name':'Название','videos':'Ссылки на видео','comments':'Тексты комментариев','replies':'Тексты ответов',
    'reply_links':'Ссылки для ответов','like_links':'Ссылки для лайков','likes':'Количество лайков',
    'keywords':'Ключевые фразы','warm_json':'Параметры поиска','options':'Настройки','when':'Время запуска',
    'review':'Проверка заказа',
}


def pth_id(value:str)->str:
    if not SAFE_ID.fullmatch(str(value)):raise ValueError('Invalid ID')
    return quote(str(value),safe='')


def select_items(data:dict,keys=('items','accounts','proxies','schedules','presets','drafts','posts'))->list[dict]:
    for k in keys:
        if isinstance(data.get(k),list):return data[k]
    return []


def preset_items(data:dict)->list[dict]:
    return [*select_items(data,('items',)),*({**item,'builtin':True} for item in select_items(data,('builtins',)))]


def public_result_link(value)->str:
    if not isinstance(value,str) or len(value)>1000 or not is_tiktok_url(value):
        return ''
    url=urlsplit(value)
    if url.scheme!='https':return ''
    query=[(name,item) for name,item in parse_qsl(url.query) if name in ('comment_id','reply_id','cid','parent_comment_id')
           and re.fullmatch(r'[0-9]{1,30}',item)]
    return urlunsplit((url.scheme,url.netloc,url.path,urlencode(query),''))


def public_result_id(value)->str:
    return value if isinstance(value,str) and re.fullmatch(r'[0-9]{1,30}',value) else ''


def public_username(value)->str:
    if isinstance(value,str) and re.fullmatch(r'@?[A-Za-z0-9._]{1,24}',value):
        return '@'+value.lstrip('@')
    return ''


class ProximaApp(AdminPanelMixin, BotApp):
    def __init__(self, config, db, tg, api):
        super().__init__(config, db, tg, api)
        self.live_cards=LiveCards(self)
        self.owner_routes:dict[str,str]={}

    def pack_owner_route(self,callback:str)->str:
        route='route:'+blake2s(callback.encode(),digest_size=12).hexdigest()
        self.owner_routes.pop(route,None)
        self.owner_routes[route]=callback
        if len(self.owner_routes)>1024:
            self.owner_routes.pop(next(iter(self.owner_routes)))
        return route

    def owner_keyboard(self,*rows)->dict:
        return keyboard(*rows,pack=self.pack_owner_route)

    @property
    def schedule_zone(self):
        return ZoneInfo(self.cfg.timezone_name)

    async def show_home(self,uid:int)->None:
        user=self.db.user(uid) or {}
        is_owner=self.owner(uid)
        active=self.db.is_active(uid,self.cfg.owner_id)
        access='Владелец' if is_owner else ('✓ до '+self.format_date(user['expires_at']) if active else '○ Нет подписки')
        if user.get('blocked') and not is_owner:access='■ Заблокирован'
        counts=self.db.task_counts(uid)
        text=(f'<b>▣ Обзор · Proxima Soft</b>\n\n'
            f'├ <b>Доступ</b> · {h(access)}\n'
            f'└ <b>Мои аккаунты</b> · <code>{number(len(self.db.account_ids(uid)))}</code>\n\n'
            f'<b>Заказы</b>\n├ В очереди · <code>{number(counts.get("queued",0))}</code>\n'
            f'└ В работе · <code>{number(sum(counts.get(state,0) for state in OPEN_STATES if state!="queued"))}</code>')
        buttons=keyboard(
            [('▣ Мои задачи','tasks','primary')],
            [('＋ Новая задача','create','success')],
            [('◇ Мои аккаунты','accounts',None),('◈ Мои прокси','proxies',None)],
            [('⌕ Собрать комментарии','new:fetch',None)],
            [('▷ Публикации · Видео / фото','posts',None)],
            [('♡ Лайки комментариев','new:commentlike',None)],
            [('⚙︎ Настройки','settings',None),('▷ Сбор видео','new:feedwarm',None)],
            [('▧ Инструменты','tools',None),('◇ Подписка','access',None if active else 'primary')],
            *(([('▣ Панель владельца','admin','primary')],) if is_owner else ())
        )
        await self.say(uid,text,buttons)

    async def tools_menu(self,uid:int)->None:
        await self.say(uid,'<b>▧ Инструменты</b>\n\nСтатистика, шаблоны и расписание твоих заказов.',keyboard(
            [('▥ Статистика','stats','primary'),('◷ Расписание','schedules',None)],
            [('▧ Мои шаблоны','templates',None),('▤ Материалы','materials',None)],
            [('↶ Обзор','home',None)]))

    async def create(self,uid:int)->None:
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        if not self.db.account_ids(uid):
            return await self.say(uid,'<b>◇ Нет назначенных аккаунтов</b>\n\nПередай администратору свой ID.\n'
                                  f'<code>{uid}</code>',keyboard([('↶ Обзор','home',None)]))
        draft=self.db.draft(uid)
        can_resume=bool(draft and (draft['mode'] in MODES or draft['mode'] in ('task_copy','source_reply')))
        rows=[]
        if can_resume:
            rows.append([('↪ Продолжить черновик','wiz_continue','primary')])
        rows.extend([(f'{MODE_SYMBOLS[mode]} {spec[0]}','new:'+mode,'primary' if mode=='publish' and not can_resume else None)]
                    for mode,spec in MODES.items() if mode not in OWNER_ONLY or self.owner(uid))
        rows.append([('▧ Мои шаблоны','templates',None)])
        rows.append([('↶ Обзор','home',None)])
        await self.say(uid,'<b>＋ Новая задача</b>\n\nВыбери вид заказа. Перед запуском можно проверить и изменить параметры.',keyboard(*rows))

    async def begin(self,uid:int,mode:str)->None:
        if mode not in MODES or (mode in OWNER_ONLY and not self.owner(uid)):return
        if not self.db.is_active(uid,self.cfg.owner_id) or not self.db.account_ids(uid):return await self.create(uid)
        self.save_wizard_step(uid,mode,'name',{})
        await self.wizard_field(uid,mode,'name')

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
        return (step in ('options','when') or (mode=='publish' and
                (step=='comments' or (step=='replies' and bool(data.get('comments'))))))

    async def wizard_field(self,uid:int,mode:str,step:str,*,error:str='')->None:
        draft=self.db.draft(uid)
        if not draft:return
        token=self.wizard_token(draft)
        data=draft['data']
        order=self.wizard_steps(mode)
        progress=f'Шаг {order.index(step)+1} из {len(order)-1} · {STEP_LABELS[step]}'
        buttons=[]
        saved_videos=self.db.preferences(uid).get('last_videos') if step=='videos' else None
        if saved_videos:
            buttons.append([(f'▧ Сохранённые видео · {len(saved_videos)}','wiz:saved:'+token,'primary')])
        key='fire_at' if step=='when' else step
        keep=data.get(key)
        if keep and (step!='when' or keep>time.time()+60):
            buttons.append([('✓ Оставить введённое','wiz:keep:'+token,'primary')])
        if self.wizard_optional(mode,step,data):
            label={'options':'→ Стандартные','when':'✓ В очередь сейчас'}.get(step,'→ Пропустить')
            buttons.append([(label,'wiz:skip:'+token,'success' if step=='when' else None)])
        buttons.append([('↶ К видам задач' if step=='name' else '↶ Назад','wiz:back:'+token,None),
                        ('× Отменить','wiz:cancel:'+token,'danger')])
        prompt=PROMPTS[step]
        if step=='when':prompt=prompt.format(zone=h(self.cfg.timezone_name))
        if keep:
            value=(self.format_date(keep) if step=='when' else f'{number(len(keep))} строк' if isinstance(keep,list)
                   else f'{number(len(keep))} параметров' if isinstance(keep,dict) else str(keep))
            prompt+='\n\nРанее введено · <code>'+h(value)+'</code>'
        if error:prompt+='\n\n<b>! Проверь ввод</b>\n'+h(error)
        await self.say(uid,f'<b>{MODE_SYMBOLS[mode]} {h(MODES[mode][0])}</b>\n{h(progress)}\n\n'+prompt,
                       keyboard(*buttons))

    async def show_wizard(self,uid:int)->None:
        draft=self.db.draft(uid)
        if not draft or draft['mode'] not in MODES:return await self.create(uid)
        if draft['step']=='review':return await self.preview(uid)
        await self.wizard_field(uid,draft['mode'],draft['step'])

    async def wizard_action(self,uid:int,action:str)->None:
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        draft=self.db.draft(uid)
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
        if verb=='cancel':
            self.db.clear_draft(uid)
            return await self.create(uid)
        if verb=='back':
            steps=self.wizard_steps(mode)
            index=steps.index(step)
            if not index:return await self.create(uid)
            self.save_wizard_step(uid,mode,steps[index-1],data)
            return await self.show_wizard(uid)
        if verb=='skip':return await self.wizard_skip(uid)
        if verb=='saved' and step=='videos':
            saved=self.db.preferences(uid).get('last_videos')
            if saved:return await self.input_draft(uid,'\n'.join(saved))
        if verb=='keep' and step!='review':
            key='fire_at' if step=='when' else step
            if data.get(key) and (step!='when' or data[key]>time.time()+60):
                return await self.next_step(uid,mode,step,data)
        if step=='review':
            if verb=='submit':return await self.submit(uid)
            if verb=='template':return await self.save_draft_template(uid)

    async def next_step(self,uid:int,mode:str,step:str,data:dict)->None:
        order=self.wizard_steps(mode)
        self.save_wizard_step(uid,mode,order[order.index(step)+1],data)
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
            elif step=='videos':data[step]=parse_targets(text)
            elif step in ('like_links','reply_links'):
                data[step]=parse_comment_links(text)
            elif step=='keywords':data[step]=limited_lines(text,100,100)
            elif step in ('comments','replies'):
                data[step]=limited_lines(text,100,700)
            elif step=='likes':
                x=int(text.strip())
                if not 1<=x<=100:raise ValueError('Укажи 1–100 лайков')
                data[step]=x
            elif step=='warm_json':data[step]=validate_feedwarm(parse_feedwarm(text),account_count=len(self.db.account_ids(uid)))
            elif step=='options':data[step]=validate_task_options(parse_options(text),mode=mode)
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
        if step=='when':
            data.pop('fire_at',None)
        else:
            data[step]=[] if step in ('comments','replies') else {}
        await self.next_step(uid,mode,step,data)

    async def preview(self,uid:int)->None:
        d=self.db.draft(uid)
        if not d or d['mode'] not in MODES or d['step']!='review':return
        mode,data=d['mode'],d['data']
        token=self.wizard_token(d)
        entries=[('Название',data['name']),('Тип',MODES[mode][0]),('Аккаунты',len(self.db.account_ids(uid))) ]
        for key in ('videos','comments','replies','like_links','reply_links','keywords'):
            if key in data:entries.append(({'videos':'Видео','comments':'Комментарии','replies':'Ответы','like_links':'Ссылки на лайки','reply_links':'Ссылки на ответы','keywords':'Ключи'}[key],len(data[key])))
        if data.get('likes'):entries.append(('Лайков на комментарий',data['likes']))
        if data.get('fire_at'):entries.append(('Запуск',datetime.fromtimestamp(data['fire_at'],self.schedule_zone).strftime('%d.%m.%Y %H:%M %Z')))
        else:entries.append(('Запуск','Когда подойдёт очередь'))
        if not self.cfg.dispatch_enabled:entries.append(('Отправка','Приостановлена администратором'))
        entries.append(('Настройки','пользовательские' if data.get('options') else 'по умолчанию сервиса'))
        if mode=='feedwarm':
            try:
                warm=validate_feedwarm(data['warm_json'],account_count=len(self.db.account_ids(uid)))
                forecast=await self.api.call('POST','/v1/tasks/feedwarm/forecast',payload=warm,retries=0)
                bytes_est=int(forecast.get('forecastBytes') or 0)
                entries.append(('Прогноз трафика',f'{bytes_est/1048576:.1f} МиБ'))
                if forecast.get('warn'):entries.append(('Предупреждение','Возможен недостаток трафика'))
            except ApiError:entries.append(('Прогноз трафика','не удалось получить'))
            except ValueError as exc:entries.append(('Параметры поиска',str(exc)))
        rich,plain=table_html('▤ Проверка заказа',entries)
        for field in ('videos','comments','replies','like_links','reply_links','keywords'):
            values=data.get(field,[])
            if values:
                rich+=f'<details><summary>{h(STEP_LABELS[field])} · {len(values)}</summary><ul>'
                rich+=''.join(f'<li>{h(value[:160])}</li>' for value in values[:3])+'</ul>'
                if len(values)>3:rich+='<footer>Показаны первые 3 строки.</footer>'
                rich+='</details>'
        await self.tg.rich(uid,rich,plain,keyboard(
            [('✓ Поставить в очередь','wiz:submit:'+token,'success')],
            [('▧ Сохранить шаблон','wiz:template:'+token,None)],
            [('↶ Назад','wiz:back:'+token,None),('× Отменить','wiz:cancel:'+token,'danger')]))

    async def _submit_extended(self,uid:int)->None:
        d=self.db.draft(uid)
        if not d or d['mode'] not in MODES or d['step']!='review':return await self.say(uid,'Черновик отсутствует.')
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        mode,data=d['mode'],d['data']
        if mode in OWNER_ONLY and not self.owner(uid):return
        try:
            payload=await build_payload(self.api,mode,data,self.db.account_ids(uid))
            task_id=self.db.queue_task(uid,data['name'],MODE_KIND[mode],payload,
                    max_daily=self.cfg.max_tasks_per_day,max_open=self.cfg.max_open_tasks,
                    fire_at=int(data.get('fire_at') or 0),draft_token=self.wizard_token(d))
            if data.get('videos'):
                self.db.set_preferences(uid,{**self.db.preferences(uid),'last_videos':data['videos']})
            self.db.log(uid,'task_created',task_id)
            await self.task(uid,task_id)
        except (ApiError,ValueError) as exc:
            text=safe_api_error(exc.code) if isinstance(exc,ApiError) else h(exc)
            await self.say(uid,'<b>! Заказ не создан</b>\n'+text+'\n\nЧерновик сохранён.',keyboard(
                [('↶ К проверке','wiz_continue','primary')],[('↶ Мои задачи','tasks',None)]))

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
            rows.append([control,('■ Остановить','stopconfirm:'+task_id,'danger')])
        if state=='completed' and kind=='fetch':
            rows.append([('↪ Ответить на собранное','source_reply:'+task_id,'success')])
        if started and kind=='fetch':
            rows.append([('▷ Видео сбора','collected:'+task_id,None)])
        if started and state in ('completed','stopped') and kind=='publish':
            rows.append([('↪ Доответить','replyinfo:'+task_id,None),('♡ Долайкать','likeinfo:'+task_id,'primary')])
        if state in ('completed','failed') and self.is_reusable(t):
            reuse=[('▧ В шаблоны','template_task:'+task_id,None)]
            if state=='completed':reuse.append(('＋ Повторить','repeat:'+task_id,'success'))
            rows.append(reuse)
        if state=='queued':rows.append([('× Отменить заказ','cancel:'+task_id,'danger')])
        if state in ('completed','stopped','failed','canceled'):
            rows.append([('× Убрать из списка','hide_task:'+task_id,'danger')])
        rows.append([('↶ Мои задачи','tasks',None),('↻ Обновить','refresh:'+task_id,None)])
        txt=task_view(t)
        details=()
        if t.get('fire_at'):
            date=datetime.fromtimestamp(t['fire_at'],self.schedule_zone).strftime('%d.%m.%Y %H:%M %Z')
            txt+='\n└ Запланировано: <code>'+h(date)+'</code>'
            details=(('Запланировано',date),)
        rich=task_rich(t,details)
        if state=='queued' and not self.cfg.dispatch_enabled:
            caption='Запуск очереди отключён администратором.'
            txt+='\n\n'+caption
            rich+='<footer>'+caption+'</footer>'
        elif state in OPEN_STATES:
            txt+='\n\n↻ Карточка обновляется автоматически.'
            rich+='<footer>↻ Карточка обновляется автоматически.</footer>'
        return rich,txt,keyboard(*rows)

    @staticmethod
    def is_reusable(task:dict) -> bool:
        payload=json.loads(task['payload_json'])
        return not (payload.get('sourceJob') or payload.get('likeSourceJob'))

    async def templates(self,uid:int,page:int=0)->None:
        records=self.db.templates(uid)
        size=10
        page=max(0,min(page,(len(records)-1)//size))
        rows=[]
        txt=f'<b>▧ Мои шаблоны</b>\n\nВсего · <code>{len(records)} / 50</code>\n\n'
        for item in records[page*size:(page+1)*size]:
            label=MODES.get(item['mode'],('Ответы на собранное',))[0]
            txt+='├ '+h(item['name'])+' · '+h(label)+'\n'
            rows.append([(f'▧ {item["name"][:45]}','tpl:'+item['template_id'],None)])
        if not records:txt+='Шаблонов пока нет.'
        rows.append(pagination('templates_page:',page,len(records)>(page+1)*size))
        rows.append([('＋ Новая задача','create','success')]);rows.append([('↶ Инструменты','tools',None)])
        await self.say(uid,txt,keyboard(*rows))

    async def save_draft_template(self,uid:int):
        d=self.db.draft(uid)
        if not d or d['mode'] not in MODES or d['step']!='review':return
        data={key:value for key,value in d['data'].items() if key not in ('wizard_token','fire_at')}
        await self.save_order_template(uid,data['name'],d['mode'],{'draft':data},'wiz_continue')

    async def save_order_template(self,uid:int,name:str,mode:str,payload:dict,back:str)->None:
        try:
            self.db.save_template(uid,name,mode,payload)
            text='<b>✓ Шаблон сохранён</b>\n'+h(name)
        except ValueError as exc:
            text='<b>! Шаблон не сохранён</b>\n'+h(exc)
        await self.say(uid,text,keyboard(
            [('▧ Мои шаблоны','templates','primary')],[('↶ Назад',back,None)]))

    async def from_template(self,uid:int,tid:str):
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        item=self.db.template(uid,tid)
        if not item:return await self.say(uid,'Шаблон не найден.')
        data=json.loads(item['payload'])
        if item['mode']=='source_reply':
            return await self.source_template_orders(uid,tid)
        if (item['mode'] in OWNER_ONLY or data.get('job',{}).get('kind') in OWNER_ONLY) and not self.owner(uid):
            return
        if not self.db.account_ids(uid):return await self.create(uid)
        if 'draft' in data:
            draft=data['draft'].copy()
            draft.pop('fire_at',None)
            self.save_wizard_step(uid,item['mode'],'review',draft)
            return await self.preview(uid)
        if 'job' in data:
            return await self.prepare_copy(uid,data['job'],item['name'])

    async def prepare_copy(self,uid:int,job:dict,name:str):
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        if job.get('sourceJob') or job.get('likeSourceJob'):
            return await self.say(uid,'Запусти повтор из карточки исходной задачи.')
        if job.get('kind') in OWNER_ONLY and not self.owner(uid):return
        if job.get('kind') not in MODES:return await self.say(uid,'Этот вид заказа нельзя скопировать.')
        if not self.db.account_ids(uid):return await self.create(uid)
        assets={key:value for key,value in job.get('assets',{}).items() if key!='accountIds'}
        job={key:value for key,value in job.items() if key not in ('requestId','name','assets')}
        self.save_wizard_step(uid,'task_copy','review',{'job':{**job,'assets':assets},'name':name[:60]})
        await self.copy_preview(uid)

    async def copy_preview(self,uid:int)->None:
        draft=self.db.draft(uid)
        if not draft or draft['mode']!='task_copy':return
        data=draft['data'];job=data['job']
        mode='reply_links' if job.get('replyLinks') else job['kind']
        entries=[('Название',data['name']),('Вид заказа',MODES[mode][0]),
                 ('Аккаунты',len(self.db.account_ids(uid)))]
        for key,label in (('awemeIds','Видео'),('replyLinks','Ссылки для ответов'),('likeLinks','Ссылки для лайков')):
            if job.get(key):entries.append((label,len(job[key])))
        for key,label in (('commentTexts','Тексты комментариев'),('replyTexts','Тексты ответов')):
            if job['assets'].get(key):entries.append((label,len(job['assets'][key])))
        if job.get('likesPerComment'):entries.append(('Лайков на комментарий',job['likesPerComment']))
        entries.append(('Запуск','Когда подойдёт очередь' if self.cfg.dispatch_enabled else 'Отправка приостановлена администратором'))
        rich,plain=table_html('▤ Проверка копии заказа',entries)
        token=self.wizard_token(draft)
        await self.tg.rich(uid,rich,plain,keyboard(
            [('✓ Поставить в очередь','wiz:submit:'+token,'success')],
            [('× Отменить','wiz:cancel:'+token,'danger')]))

    async def queue_existing(self,uid:int,job:dict,name:str,token:str):
        if job.get('kind') in OWNER_ONLY and not self.owner(uid):return
        ids=self.db.account_ids(uid)
        if not ids:return await self.create(uid)
        try:
            validate_task_options(job.get('settings'))
            if 'feedWarm' in job:validate_feedwarm(job['feedWarm'],account_count=len(ids))
            payload={**job,'assets':{**job['assets'],'accountIds':ids}}
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
        await self.say(uid,'<b>⚙︎ Настройки кабинета</b>\n\nОбщие параметры Tokora для новых заказов.',keyboard(
            [('▤ Текущие настройки','owner_show:settings','primary')],
            [('✎ Изменить настройки','owner_edit:settings',None)],
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
            [('◷ Создать план','owner_edit:post_plan',None),('▤ Последний план','owner_show:plan_latest',None)],
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

    async def accounts(self,uid:int,page:int=0)->None:
        if not self.owner(uid):return await super().accounts(uid,page)
        size=10
        result=await self.api.account_page(page*size,size)
        arr=result.get('accounts',[])
        total=result.get('total',len(arr))
        rows=[]
        for x in arr:
            aid=str(x.get('accountId',''))
            if not SAFE_ID.fullmatch(aid):continue
            username=(x.get('profile') or {}).get('username')
            label='@'+str(username)[:25] if username else aid[:18]
            symbol,status=self.account_status(x.get('status','unknown')).split(' ',1)
            rows.append([(f'{symbol} {label} · {status}','account:'+aid,None)])
        rows.extend([pagination('accounts_page:',page,(page+1)*size<total),
                     [('✓ Проверить все','owner_go:accounts_check','primary'),('▤ Ход проверки','owner_show:accounts_check',None)],
                     [('↑ Импорт аккаунтов','upload:cookies',None)],
                     [('↶ Обзор','home',None)]])
        intro=f'<b>◇ Аккаунты кабинета</b>\n{number(total)} в пуле · стр. {page+1}\n\n'
        caption='Выбери аккаунт, чтобы открыть карточку и проверить статус.' if arr else 'На этой странице аккаунтов нет.'
        await self.say(uid,intro+caption,self.owner_keyboard(*rows))

    async def proxies(self,uid:int)->None:
        if not self.owner(uid):
            ids=self.db.account_ids(uid)
            accounts=await self.assigned_accounts(ids)
            rows=[('Назначено аккаунтов',len(ids)),
                  ('С привязкой прокси',sum(bool(a.get('proxy')) for a in accounts.values())),
                  ('Активных сессий',sum(a.get('status')=='active' for a in accounts.values())),
                  ('Данные недоступны',len(ids)-len(accounts))]
            rich,plain=table_html('◈ Мои прокси',rows,'Прокси и привязки настраивает администратор.')
            return await self.tg.rich(uid,rich,plain,keyboard([('◇ Мои аккаунты','accounts',None)],
                                                          [('↶ Обзор','home',None)]))
        data=await self.api.call('GET','/v1/proxies')
        items=select_items(data)
        await self.say(uid,'<b>◈ Прокси · общий кабинет</b>\n\n'
            'Записей: <code>'+str(len(items))+'</code>\n'
            'Используется пул твоего кабинета Tokora. Для ручной привязки отправь '
            '<code>/accountproxy ACCOUNT_ID PROXY_ID</code>.',keyboard(
            [('▤ Сводка','owner_show:proxies',None)],
            [('↻ Проверить прокси','owner_go:proxies_check','primary')],
            [('▤ Статус проверки','owner_show:proxies_check',None)],
            [('■ Остановить проверку','owner_go:proxies_check_stop','danger')],
            [('↑ Импорт TXT','upload:proxies','success')],
            [('↶ Обзор','home',None)]))

    async def owner_show(self,uid:int,key:str)->None:
        if not self.owner(uid):return
        endpoints={
          'settings':'/v1/settings','presets':'/v1/presets',
          'posts_settings':'/v1/posts/settings','drafts':'/v1/posts/drafts',
          'plan_latest':'/v1/posts/plan/latest','post_queue':'/v1/posts',
          'post_templates':'/v1/posts/templates','accounts_check':'/v1/accounts/check',
          'proxies_check':'/v1/proxies/check','proxies':'/v1/proxies',
          'photos':'/v1/photos','photos_replies':'/v1/photos','asset_comments':'/v1/assets/commentTexts',
          'asset_replies':'/v1/assets/replyTexts', 'stats':'/v1/statistics',
          'schedule_upstream':'/v1/schedules', 'posts_summary':'/v1/posts/summary',
        }
        if key not in endpoints:return
        params={'target':'replies'} if key=='photos_replies' else {'limit':15} if key=='post_queue' else None
        try:result=await self.api.call('GET',endpoints[key],params=params)
        except ApiError as exc:
            if key!='plan_latest' or exc.status!=404:raise
            result={}
        if key in ('asset_comments','asset_replies'):
            content=result.get('lines',[])
            txt='<b>▤ '+h('Комментарии' if key=='asset_comments' else 'Ответы')+'</b>\n'
            txt+='Записей: <code>'+str(len(content))+'</code>\n\n'
            txt+='\n'.join('├ '+h(str(x)[:120]) for x in content[:30])
            return await self.say(uid,txt[:3800],keyboard([('✎ Заменить','owner_edit:'+key,'primary')],[('↶ Материалы','materials',None)]))
        if key in ('settings','posts_settings'):
            entries=[(k, str(v)[:130]) for k,v in result.items() if not any(s in k.lower() for s in ('key','cookie','password','token','proxy'))]
            rich,plain=table_html('⚙ '+key,entries[:35])
            return await self.tg.rich(uid,rich[:32000],plain,keyboard([('✎ Изменить','owner_edit:'+key,'primary')],[('↶ Настройки','settings' if key=='settings' else 'posts',None)]))
        if key in ('accounts_check','proxies_check','stats','posts_summary'):
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
            rows=[]
            for item in items[:25]:
                oid=str(item.get('id',''))
                if SAFE_ID.fullmatch(oid):rows.append([(f'▧ {str(item.get("name") or oid)[:36]}','draft:'+oid,None)])
            rows.append([('↑ Видео','upload:video','success'),('＋ Фотоальбом','owner_edit:album',None)])
            rows.append([('↶ Публикации','posts',None)])
            return await self.say(uid,'<b>▧ Черновики</b>\n\nВсего: <code>'+str(result.get('total',len(items)))+'</code>\n'
                'Выбери черновик или создай новый.',self.owner_keyboard(*rows))
        if key=='post_queue':
            items=select_items(result,('items','tasks','posts','queue'))
            text='<b>▣ Очередь публикаций</b>\n'
            text+=f'Всего: <code>{h(result.get("total",len(items)))}</code> · '
            text+='на паузе\n\n' if result.get('paused') else 'активна\n\n'
            for item in items[:15]:
                text+='├ <code>'+h(item.get('id',''))+'</code> · '+h(item.get('outcome') or item.get('state',''))+'\n'
            if not items:text+='Записей нет или очередь пуста.\n'
            rows=[[(f'▷ {str(x.get("name") or x.get("id"))[:36]}', 'post:'+str(x['id']),None)]
                  for x in items[:12] if SAFE_ID.fullmatch(str(x.get('id') or ''))]
            rows += [[('Ⅱ Пауза','owner_go:posts_pause',None),('▷ Продолжить','owner_go:posts_resume','success')],
                   [('↻ Обновить статусы','owner_go:posts_refresh','primary')],
                   [('× Отменить ожидающие','owner_go:posts_cancel_queued','danger')],
                   [('× Очистить историю','owner_go:posts_clear_history','danger')],
                   [('↶ Публикации','posts',None)] ]
            return await self.say(uid,text,self.owner_keyboard(*rows))
        if key=='plan_latest':
            if not result or not result.get('id'):
                return await self.say(uid,'План пока не создан.',keyboard([('◷ Создать план','owner_edit:post_plan','success')]))
            pid=str(result['id'])
            if not SAFE_ID.fullmatch(pid):return
            return await self.show_plan(uid,pid,result)
        if key=='post_templates':
            items=select_items(result)
            rows=[]
            for v in items[:25]:
                oid=str(v.get('id',''))
                if SAFE_ID.fullmatch(oid):rows.append([(f'▧ {str(v.get("name") or oid)[:42]}','posttpl:'+oid,None)])
            rows.append([('＋ Создать','owner_edit:post_template','success')])
            rows.append([('↶ Публикации','posts',None)])
            return await self.say(uid,'<b>▧ Шаблоны подписей</b>\nВсего: '+str(len(items)),self.owner_keyboard(*rows))
        if key=='presets':
            items=preset_items(result)
            rows=[]
            for i,v in enumerate(items[:25]):
                name=str(v.get('name') or '')
                # Index is resolved against a freshly fetched list at click time.
                rows.append([(f'⚙︎ {name[:38]} · '+('встроенный' if v.get('builtin') else 'личный'),'preset:'+str(i),None)])
            rows.append([('＋ Сохранить','owner_edit:preset_new','success')]);rows.append([('↶ Настройки','settings',None)])
            return await self.say(uid,'<b>▧ Пресеты API</b>\n\nПресетов: <code>'+str(len(items))+'</code>\n'
               'Выбери пресет для применения или удаления.',keyboard(*rows))
        if key=='proxies':
            items=select_items(result)
            statuses={}
            for item in items:statuses[str(item.get('status','неизвестно'))]=statuses.get(str(item.get('status','неизвестно')),0)+1
            rich,plain=table_html('◈ Прокси', [('Всего',len(items)),*statuses.items()],'Адреса, логины и пароли не отображаются в Telegram.')
            if items:
                preview=[(x.get('id','—'),x.get('status','—'),x.get('countryCode') or '—',x.get('latencyMs','—'))
                         for x in items[:15]]
                rich+=rich_table(('ID','Статус','Страна','мс'),preview)
                plain+='\n'+'\n'.join(f'├ <code>{h(pid)}</code> · {h(status)} · {h(country)}'
                                       for pid,status,country,_ in preview)
                if len(items)>15:
                    rich+='<footer>Первые 15 записей. Полный список доступен в кабинете.</footer>'
            return await self.tg.rich(uid,rich,plain,keyboard([('↶ Прокси','proxies',None)]))
        if key in ('photos','photos_replies'):
            items=select_items(result)
            rows=[]
            for item in items[:25]:
                oid=str(item.get('id',''))
                if SAFE_ID.fullmatch(oid):rows.append([(f'▣ {oid[:20]} · {item.get("width","")}×{item.get("height","")}',
                                                       'photo:'+('r:' if key=='photos_replies' else 'c:')+oid,None)])
            rows.append([('↑ Добавить','upload:photo','success')]);rows.append([('↶ Материалы','materials',None)])
            return await self.say(uid,'<b>▣ Фото</b>\nВсего: <code>'+str(len(items))+'</code>',self.owner_keyboard(*rows))
        if key=='schedule_upstream':
            items=select_items(result)
            return await self.say(uid,'<b>◷ Задания в расписании API</b>\n\n'+
                '\n'.join('├ '+h(x.get('id',''))+' · '+h(x.get('state','')) for x in items[:30]),keyboard([('↶ Обзор','home',None)]))

    async def show_plan(self,uid:int,pid:str,result:dict|None=None):
        if not self.owner(uid):return
        v=result or await self.api.call('GET','/v1/posts/plan/'+pth_id(pid))
        labels={'id':'ID плана','state':'Статус','postsTotal':'Публикаций','queued':'В очереди',
                'accepted':'Подошли','skipped':'Пропущено','checked':'Проверено','candidates':'Кандидатов','error':'Ошибка'}
        fields=[(label,v[key]) for key,label in labels.items() if key in v and v[key] not in (None,'')]
        rich,plain=table_html('◷ План публикаций',fields)
        ready=(v.get('state')=='ready' or (type(v.get('readyAt')) is int and v['readyAt']>0))
        launch=[('✓ Запустить план','plan_confirm:'+pid,'success')] if ready and not (v.get('queued') or v.get('error')) else []
        await self.tg.rich(uid,rich,plain,self.owner_keyboard(launch,
            [('↻ Обновить план','plan:'+pid,None)],
            [('× Удалить план','plan_delete_confirm:'+pid,'danger')],
            [('↶ Публикации','posts',None)]))

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
        labels={'name':'Название','kind':'Тип','description':'Описание','hashtags':'Хештеги',
                'visibility':'Видимость','comments':'Комментарии','activeTasks':'Активных задач','sizeBytes':'Размер, Б',
                'durationMs':'Длительность, мс','selected':'Выбран'}
        fields=[(label,('Да' if d[key] else 'Нет') if isinstance(d[key],bool) else str(d[key])[:160])
                for key,label in labels.items() if key in d and d[key] not in (None,'')]
        rich,plain=table_html('▧ Черновик',fields)
        add_photo=[('＋ Добавить фото','upload:album_'+oid,None)] if d.get('kind')=='photo' else []
        await self.tg.rich(uid,rich,plain,self.owner_keyboard(
            [('✎ Изменить','owner_edit:draft_'+oid,'primary')],
            [('▧ Применить шаблон подписи','caption_pick:'+oid+':0',None)],
            [('◷ Создать план для черновика','draft_plan:'+oid,'success')],
            add_photo,
            [('× Удалить','draft_delete_confirm:'+oid,'danger')],
            [('↶ Черновики','owner_show:drafts',None)]))

    async def owner_edit(self,uid:int,key:str):
        if not self.owner(uid):return
        text={
         'settings':'Отправь JSON с <b>изменяемыми полями TaskSettingsPatch</b>. Меняются только указанные поля. Не распространяется на уже созданные задачи.',
         'posts_settings':'Отправь JSON со всеми полями PostSettings (см. docs/tokora-openapi.json). Для безопасности accountIds должны быть реальными.',
         'preset_new':'Отправь название пресета настроек (до 64 символов).',
         'album':'Отправь название нового фотоальбома (до 80 символов).',
         'post_plan':'Отправь JSON для плана, например: <code>{"draftIds":["ID1","ID2"]}</code> (без отправки текста активной задачи).',
         'post_template':'Отправь JSON с name, description и hashtags.',
         'asset_comments':'Отправь новые строки комментариев текстом либо TXT-документом. Это полностью заменит общие материалы.',
         'asset_replies':'Отправь новые строки ответов текстом либо TXT-документом. Это полностью заменит общие материалы.',
        }.get(key)
        if key.startswith('draft_'):
            oid=key[6:]
            if not SAFE_ID.fullmatch(oid):return
            text='Отправь JSON с изменяемыми полями черновика. Например <code>{"description":"Мой ролик","hashtags":"#tag"}</code>.'
        if not text:return
        self.db.save_draft(uid,'owner_edit',key,{})
        labels={'settings':'Настройки кабинета','posts_settings':'Настройки публикаций','preset_new':'Новый пресет',
                'album':'Новый фотоальбом','post_plan':'Новый план','post_template':'Шаблон подписи',
                'asset_comments':'Материалы · комментарии','asset_replies':'Материалы · ответы'}
        await self.say(uid,'<b>✎ '+h(labels.get(key,'Редактировать черновик'))+'</b>\n\n'+text,
                       keyboard([('↶ Отмена','discard',None)]))

    async def aux_input(self,uid:int,d:dict,text:str):
        if not self.owner(uid):return
        mode,step=d['mode'],d['step']
        if mode!='owner_edit':return
        try:
            if step in ('asset_comments','asset_replies'):
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
                    self.db.clear_draft(uid)
                    return await self.say(uid,f'<b>✓ Фотоальбом создан</b>\n\n{h(name)}',self.owner_keyboard(
                        [('↑ Добавить фото','upload:album_'+oid,'success')],
                        [('↶ Черновики','owner_show:drafts',None)]))
                message='Фотоальбом создан · '+h(oid)
            else:
                try: body=json.loads(text)
                except json.JSONDecodeError:raise ValueError('Требуется правильный JSON-объект.') from None
                if not isinstance(body,dict) or len(json.dumps(body))>16000:raise ValueError('JSON-объект слишком большой')
                if step=='settings':
                    opts=parse_options(text)
                    await self.api.call('PATCH','/v1/settings',payload=opts,retries=0)
                    message='Настройки обновлены.'
                elif step=='posts_settings':
                    self._allowed_fields(body,'PostSettings')
                    await self.api.call('PUT','/v1/posts/settings',payload=body,retries=0)
                    message='Настройки публикаций обновлены.'
                elif step=='post_plan':
                    return await self.create_post_plan(uid,body)
                elif step=='post_template':
                    schema=load_spec()['paths']['/v1/posts/templates']['post']['requestBody']['content']['application/json']['schema']
                    if set(body)-set(schema['properties']) or not valid_body(schema,body):
                        raise ValueError('Допустимы строковые поля name, description и hashtags.')
                    if not body['name'].strip():raise ValueError('Укажи название шаблона.')
                    await self.api.call('POST','/v1/posts/templates',payload=body,retries=0)
                    message='Шаблон подписи создан.'
                elif step.startswith('draft_'):
                    oid=step[6:]
                    self._allowed_fields(body,'PostDraftPatch',partial=True)
                    await self.api.call('PATCH','/v1/posts/drafts/'+pth_id(oid),payload=body,retries=0)
                    message='Черновик обновлён.'
                else:raise ValueError('Неизвестная команда')
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

    async def account_info(self,uid:int,aid:str):
        if not self.owner(uid):return
        v=await self.api.call('GET','/v1/accounts/'+pth_id(aid))
        profile=v.get('profile') or {}
        owner=self.db.conn.execute('SELECT user_id FROM accounts WHERE account_id=?',(aid,)).fetchone()
        values=[('ID',aid),('Пользователь',profile.get('username','—')),('Статус',self.account_status(v.get('status','unknown'))),
                ('Назначен',owner[0] if owner else 'Никому'),('Профиль',profile.get('nickname','—')),
                ('Проверено',v.get('checkedAt','—'))]
        rich,plain=table_html('◇ Аккаунт',values)
        await self.tg.rich(uid,rich,plain,self.owner_keyboard(
            [('✓ Проверить аккаунт','acc_go:check:'+aid,'primary')],
            [('↻ Обновить профиль','acc_go:profile:'+aid,None)],
            [('× Удалить','acc_delete_confirm:'+aid,'danger')],
            [('↶ Аккаунты','accounts',None)]))

    async def post_info(self,uid:int,oid:str):
        if not self.owner(uid):return
        v=await self.api.call('GET','/v1/posts/'+pth_id(oid))
        rich,plain=table_html('▣ Публикация',[(label,str(v.get(key,'—'))[:150]) for key,label in (
            ('id','ID'),('name','Название'),('outcome','Статус'),('attempts','Попытки'),
            ('accountName','Аккаунт'),('errorCode','Код ошибки'),('postUrl','Ссылка на пост'))])
        await self.tg.rich(uid,rich,plain,self.owner_keyboard(
            [('↻ Перепроверить','post_go:refresh:'+oid,None)],
            [('↻ Повторить','post_retry_confirm:'+oid,'danger')],
            [('■ Отменить','post_go:cancel:'+oid,'danger')],
            [('↶ Публикации','posts',None)]))

    async def task_entries(self,uid:int,tid:str,offset:int=0):
        job=self.db.task(tid,uid)
        if not job or not job['upstream_id']:return await self.say(uid,'Результаты пока недоступны.')
        size=10
        v=await self.api.call('GET','/v1/tasks/'+pth_id(job['upstream_id'])+'/entries',
                              params={'offset':offset,'limit':size})
        items=v.get('items',[])[:size]
        content=['<b>▤ Результаты</b>',h(job['display_name'])+' · <code>'+h(tid)+'</code>',
                 f'{number(v.get("total",len(items)))} записей · стр. {offset//size+1}']
        for i,item in enumerate(items,offset+1):
            author=public_username(item.get('author')) if job['kind']=='usernames' else ''
            text=h(author or str(item.get('text') or '—')[:145])
            status=result_status(item.get('outcome'))
            content.append('\n<b>'+str(i)+'.</b> '+text+'\n└ '+status)
            if job['kind']=='feedwarm' and (link:=public_result_link(item.get('url'))):
                content.append('<a href="'+h(link)+'">↗ Открыть видео</a>')
        if not items:content.append('\nРезультатов на этой странице пока нет.')
        rows=[]
        nav=[]
        if offset:nav.append(('← Ранее','entries:'+tid+':'+str(max(0,offset-size)),None))
        if v.get('more'):nav.append(('→ Далее','entries:'+tid+':'+str(offset+size),'primary'))
        actions=[('⇩ Экспорт','exports:'+tid,'primary')]
        if job['kind'] in PUBLICATION_REPORT_KINDS:actions.insert(0,('▥ Отчёт','report:'+tid,None))
        rows.extend([nav,actions,
                     [('↶ К задаче','task:'+tid,None)]])
        await self.say(uid,'\n'.join(content),keyboard(*rows))

    async def exports(self,uid:int,tid:str):
        job=self.db.task(tid,uid)
        if not job or not job['upstream_id']:return await self.say(uid,'Пока нет результатов.')
        rows=[[('⇩ Записи · CSV','export_csv:'+tid,'primary'),('⇩ Ссылки · TXT','export_links:'+tid,None)]]
        if job['kind'] in PUBLICATION_REPORT_KINDS:
            label='▥ Отчёт по лайкам' if job['kind']=='commentlike' else '▥ Отчёт по текстам'
            rows.append([(label,'report:'+tid,None)])
        rows.append([('↶ К результатам','entries:'+tid,None)])
        await self.say(uid,'<b>⇩ Экспорт задачи</b>\n\nДо 2 000 первых записей.',keyboard(*rows))

    async def collected_videos(self,uid:int,tid:str):
        task=self.db.task(tid,uid)
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
                rich+=f'<tr><td><code>{video}</code></td><td>{comments}</td><td>{replies}</td></tr>'
                plain+=f'\n├ <code>{video}</code> · {comments} / {replies}'
            rich+='</table>'
            if len(result['items'])>20:rich+='<footer>Показаны первые 20 видео.</footer>'
        await self.tg.rich(uid,rich,plain,keyboard([('↶ Задача','task:'+tid,None)]))

    async def export_file(self,uid:int,tid:str,mode:str):
        job=self.db.task(tid,uid)
        if not job or not job['upstream_id']:return await self.say(uid,'Нет доступа к данным.')
        items=[]
        path='/v1/tasks/'+pth_id(job['upstream_id'])+'/entries'
        for offset in range(0,2000,200):
            page=await self.api.call('GET',path,params={'offset':offset,'limit':200})
            items.extend(page.get('items',[]))
            if not page.get('more'):break
        if mode=='csv':
            buf=io.StringIO()
            fields=('kind','awemeId','cid','text','outcome','status','confirmed','url','at')
            if job['kind']=='usernames':fields+=('username',)
            wr=csv.DictWriter(buf,fieldnames=fields,extrasaction='ignore');wr.writeheader()
            kinds={'comment':'Комментарий','reply':'Ответ','video':'Видео','like':'Лайк','username':'Пользователь'}
            for x in items:
                values={'kind':kinds.get(str(x.get('kind')),'Запись'),'awemeId':public_result_id(x.get('awemeId')),
                        'cid':public_result_id(x.get('cid')),'text':str(x.get('text') or '')[:1800],
                        'outcome':result_status(x.get('outcome')),'url':public_result_link(x.get('url'))}
                if job['kind']=='usernames':values['username']=public_username(x.get('author')).lstrip('@')
                for key in ('status','confirmed'):
                    if type(x.get(key)) is int:values[key]=x[key]
                at=x.get('at')
                if isinstance(at,str):
                    try:values['at']=datetime.fromisoformat(at).isoformat()
                    except ValueError:pass
                wr.writerow({key:csv_safe(value) for key,value in values.items()})
            content=b'\xef\xbb\xbf'+buf.getvalue().encode('utf-8')
            filename='proxima_'+tid+'_entries.csv'
        else:
            # Link exports are reassembled locally, not forwarded unchanged from
            # provider-generated text that might contain upstream branding.
            links=[link for x in items if (link:=public_result_link(x.get('url')))]
            content=('\n'.join(dict.fromkeys(links))+'\n').encode('utf-8')
            filename='proxima_'+tid+'_links.txt'
        if len(content)>20*1024*1024:raise ValueError('Файл слишком большой')
        await self.tg.attachment(uid,content,filename,'Proxima Soft · экспорт')
        self.db.log(uid,'export',tid)

    async def upload_intro(self,uid:int,kind:str):
        if not self.owner(uid):return
        if kind in ('cookies','proxies'):
            return await self.say(uid,'<b>Безопасная загрузка</b>\n\n'
                'Cookie-файлы и реквизиты прокси нужно импортировать на своём сервере, '\
                'а не отправлять в Telegram: переписка с ботами не поддерживает сквозное шифрование.\n\n'
                '<code>python -m proxima_bot.manage import-account /private/cookies.json --user-id 123</code>\n'
                '<code>python -m proxima_bot.manage import-proxies /private/proxies.txt</code>')
        if kind not in ('video','photo','photo_comments','photo_replies') and not kind.startswith('album_'):return
        if kind.startswith('album_') and not SAFE_ID.fullmatch(kind[6:]):return
        if kind=='photo':
            return await self.say(uid,'<b>↑ Добавить фото</b>\n\nКуда загрузить?',keyboard(
                [('▤ Комментарии','upload:photo_comments','primary'),('↪ Ответы','upload:photo_replies',None)],
                [('↶ Материалы','materials',None)]))
        if kind=='video':hint='MP4 / MOV до 20 МиБ. Отправь файл именно как документ.'
        else:hint='JPEG / PNG / WebP до 15 МиБ. Отправь как фото или документ.'
        self.db.save_draft(uid,'upload',kind,{})
        await self.say(uid,'<b>↑ Загрузка медиа</b>\n\n'+hint,keyboard([('↶ Отмена','discard',None)]))

    async def handle_upload(self,uid:int,msg:dict)->bool:
        d=self.db.draft(uid)
        if not d:return False
        if (d['mode'] in MODES or (d['mode']=='source_reply' and d['step']=='replies')) and msg.get('document'):
            doc=msg['document']
            if not str(doc.get('file_name') or '').lower().endswith('.txt'):
                await self.say(uid,'Нужен обычный TXT-файл в UTF-8.');return True
            try:
                data,_=await self.tg.fetch_upload(doc['file_id'],100*1024)
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
        if not doc and not photo:
            await self.say(uid,'Отправь файл или /cancel.');return True
        if kind=='video' and not doc:
            await self.say(uid,'Видео нужно отправить как документ MP4/MOV.');return True
        f=doc or photo[-1]
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
                name=(doc.get('file_name') or 'video')[:70]
                result=await self.api.upload('/v1/posts/drafts/upload',content,params={'kind':'video','name':name})
            elif kind.startswith('album_'):
                result=await self.api.upload('/v1/posts/drafts/'+pth_id(kind[6:])+'/photos',content,image_type)
            else:
                target='replies' if kind=='photo_replies' else 'comments'
                result=await self.api.upload('/v1/photos',content,image_type,params={'target':target})
            oid=result.get('id') or (result.get('draft') or {}).get('id') or ''
            self.db.clear_draft(uid)
            self.db.log(uid,'upload_media',kind)
            if kind=='video' or kind.startswith('album_'):
                rows=[[('↶ Черновики','owner_show:drafts',None)]]
                if SAFE_ID.fullmatch(str(oid)):
                    rows.insert(0,[('▧ Открыть черновик','draft:'+str(oid),'primary')])
                if kind.startswith('album_'):
                    rows.insert(0,[('↑ Добавить ещё фото','upload:'+kind,'success')])
            else:
                rows=[[('↶ Материалы','materials',None)]]
            await self.say(uid,'<b>✓ Файл принят</b>\n'+('ID: <code>'+h(oid)+'</code>' if oid else 'Операция принята API.'),
                            self.owner_keyboard(*rows))
        except (ValueError,ApiError,TelegramError) as exc:
            detail=(safe_api_error(exc.code) if isinstance(exc,ApiError) else
                    'Telegram не смог передать файл.' if isinstance(exc,TelegramError) else h(exc))
            await self.say(uid,'Ошибка: '+detail)
        return True

    async def owner_go(self,uid:int,key:str,confirmed:bool=False):
        if not self.owner(uid) or key not in ADMIN_ACTIONS:return
        if key in DESTRUCTIVE:
            if not confirmed:
                self.db.save_draft(uid,'owner_confirmation','confirm',{'action':key})
                return await self.say(uid,'<b>Подтверждение</b>\n\nДействие <code>'+h(key)+'</code> может изменить общий кабинет.',keyboard(
                    [('× Да, выполнить','owner_confirm:'+key,'danger')],
                    [('↶ Отмена','discard',None)]))
            if not self.consume_owner_confirmation(uid,key):return
        method,path,payload=ADMIN_ACTIONS[key]
        await self.api.call(method,path,payload=payload,retries=0)
        self.db.log(uid,key)
        await self.say(uid,'✓ Операция выполнена.\n'+h(key),keyboard([('↶ Администратор','admin',None)]))

    def consume_owner_confirmation(self,uid:int,action:str,oid:str='')->bool:
        draft=self.db.draft(uid)
        if not (draft and draft['mode']=='owner_confirmation' and time.time()-draft['updated_at']<=600
                and draft['data'].get('action')==action and draft['data'].get('id','')==oid):
            return False
        self.db.clear_draft(uid)
        return True

    async def owner_action(self,uid:int,action:str,oid:str):
        if not self.owner(uid):return
        q=pth_id(oid)
        action_map={
            'draft_delete':('DELETE','/v1/posts/drafts/'+q,None),
            'plan_delete':('DELETE','/v1/posts/plan/'+q,None),
            'plan_launch':('POST','/v1/posts/plan/'+q+'/launch',{}),
            'acc_delete':('DELETE','/v1/accounts/'+q,None),
            'photo_delete':('DELETE','/v1/photos/'+q,None),
            'post_cancel':('POST','/v1/posts/'+q+'/cancel',{}),
            'post_refresh':('POST','/v1/posts/'+q+'/refresh',{}),
            'post_retry':('POST','/v1/posts/'+q+'/retry',{'confirm':True}),
            'post_resolve':('POST','/v1/posts/'+q+'/resolve',{}),
            'acc_check':('POST','/v1/accounts/'+q+'/check',{}),
            'acc_profile':('POST','/v1/accounts/'+q+'/profile/refresh',{}),
        }
        if action not in action_map:return
        method,path,payload=action_map[action]
        if action=='acc_delete':
            if self.db.conn.execute('SELECT 1 FROM accounts WHERE account_id=?',(oid,)).fetchone():
                return await self.say(uid,'Сначала отвяжи аккаунт от трафера. Привязку действующего заказа снять нельзя.')
        result=await self.api.call(method,path,payload=payload,retries=0)
        self.db.log(uid,action,oid)
        if action=='plan_launch':
            return await self.say(uid,'<b>✓ Публикации поставлены в очередь</b>\n'
                                  f'├ Добавлено · <code>{number(result.get("queued"))}</code>',
                                  keyboard([('▣ Очередь публикаций','owner_show:post_queue','primary')],
                                           [('↶ Публикации','posts',None)]))
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

        body=json.dumps(scrub(result),ensure_ascii=False,indent=2)
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
            # Proxy addresses are not accepted over Telegram; only preexisting proxy IDs.
            proxy_binding={'proxy':None} if px=='-' else {'proxyId':px}
            await self.api.call('PUT','/v1/accounts/'+pth_id(aid)+'/proxy',payload=proxy_binding,retries=0)
            self.db.log(uid,'account_proxy_changed',aid)
            await self.say(uid,'✓ Привязка прокси обновлена.');return True
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
        if action.startswith('wiz:') or action in ('wiz_continue','wiz_skip','wiz_saved_videos','save_draft_template','submit'):
            return await self.wizard_action(uid,action)
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
        if action=='materials':return await self.materials(uid)
        if action=='export_menu':return await self.stats(uid)
        if action=='admin_health':return await self.health(uid)
        if action=='noop':return
        if action.startswith('new:'):
            if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
            return await self.begin(uid,action[4:])
        if action.startswith('owner_show:'):
            return await self.owner_show(uid,action[11:])
        if action.startswith('owner_edit:'):
            return await self.owner_edit(uid,action[11:])
        if action.startswith('owner_go:'):
            return await self.owner_go(uid,action[9:])
        if action.startswith('owner_confirm:'):
            return await self.owner_go(uid,action[14:],confirmed=True)
        if action.startswith('upload:'):
            return await self.upload_intro(uid,action[7:])
        if action.startswith('account:'):
            return await self.account_info(uid,action[8:])
        if action.startswith('draft:'):
            return await self.draft_info(uid,action[6:])
        if action.startswith('draft_plan:') and self.owner(uid):
            oid=action[11:]
            if SAFE_ID.fullmatch(oid):return await self.create_post_plan(uid,{'draftIds':[oid]})
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
            tid=action[4:]
            item=self.db.template(uid,tid)
            if not item:return await self.say(uid,'Шаблон недоступен.')
            return await self.say(uid,'<b>▧ '+h(item['name'])+'</b>\n\nВыбери действие:',keyboard(
                [('＋ Создать по шаблону','tpl_use:'+tid,'success')],
                [('× Удалить','tpl_delete_confirm:'+tid,'danger')],
                [('↶ Шаблоны','templates',None)]))
        if action.startswith('tpl_use:'):return await self.from_template(uid,action[8:])
        if action.startswith('tpl_delete_confirm:'):
            tid=action[19:]
            if not self.db.template(uid,tid):return
            return await self.say(uid,'Удалить шаблон <code>'+h(tid)+'</code>?',keyboard(
                [('× Удалить','tpl_delete:'+tid,'danger')],[('↶ Отмена','templates',None)]))
        if action.startswith('tpl_delete:'):
            self.db.delete_template(uid,action[11:]);return await self.templates(uid)
        if action.startswith('template_task:'):
            tid=action[14:]
            t=self.db.task(tid,uid)
            if not t or not self.is_reusable(t):return
            job=json.loads(t['payload_json'])
            job.pop('requestId',None)
            job.pop('name',None)
            job.get('assets',{}).pop('accountIds',None)
            return await self.save_order_template(uid,t['display_name'],t['kind'],{'job':job},'task:'+tid)
        if action.startswith('repeat:'):
            tid=action[7:]
            t=self.db.task(tid,uid)
            if not t or t['state']!='completed' or not self.is_reusable(t):return
            return await self.prepare_copy(uid,json.loads(t['payload_json']),'Повтор · '+t['display_name'][:45])
        if action.startswith('entries:'):
            a=action.split(':')
            if len(a) not in (2,3):return
            try:offset=int(a[2]) if len(a)==3 else 0
            except ValueError:return
            if not 0<=offset<=50000:return
            return await self.task_entries(uid,a[1],offset)
        if action.startswith('exports:'):
            return await self.exports(uid,action[8:])
        if action.startswith('collected:'):
            return await self.collected_videos(uid,action[10:])
        if action.startswith('hide_task:'):
            tid=action[10:]
            t=self.db.task(tid,uid)
            if not t or t['state'] not in ('completed','stopped','failed','canceled'):return
            return await self.say(uid,'Убрать задачу из твоего списка? Она сохранится в истории администратора.',
                keyboard([('× Убрать','hide_confirm:'+tid,'danger')],[('↶ Задача','task:'+tid,None)]))
        if action.startswith('hide_confirm:'):
            self.db.hide_task(uid,action[13:])
            return await self.tasks(uid)
        for prefix,mode in [('export_csv:','csv'),('export_links:','links')]:
            if action.startswith(prefix):return await self.export_file(uid,action[len(prefix):],mode)
        if action.startswith(('pause:','resume:')):
            action_kind,tid=action.split(':',1)
            t=self.db.task(tid,uid)
            if not t or t['state']!=('running' if action_kind=='pause' else 'paused') or not t['upstream_id']:return
            if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
            await self.api.call('POST','/v1/tasks/'+pth_id(t['upstream_id'])+'/'+action_kind,retries=0)
            self.db.log(uid,'task_'+action_kind,tid)
            return await self.say(uid,'✓ Запрос принят, обнови статус через несколько секунд.',keyboard([('↶ Задача','task:'+tid,None)]))
        if action.startswith('post_go:'):
            fields=action.split(':',2)
            if len(fields)==3:return await self.owner_action(uid,'post_'+fields[1],fields[2])
        if action.startswith('acc_go:'):
            fields=action.split(':',2)
            if len(fields)==3:return await self.owner_action(uid,'acc_'+fields[1],fields[2])
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
                label='Запустить план' if internal=='plan_launch' else 'Подтвердить изменение общего кабинета'
                self.db.save_draft(uid,'owner_confirmation','confirm',{'action':internal,'id':oid})
                return await self.say(uid,'<b>'+h(label)+'</b>\n\nID: <code>'+h(oid)+'</code>\n'
                    'При повторной публикации существует риск дубля.',self.owner_keyboard(
                    [('✓ Подтверждаю','owner_do:'+internal+':'+oid,'danger' if internal!='plan_launch' else 'success')],
                    [('↶ Отмена','discard',None)]))
        if action.startswith('owner_do:') and self.owner(uid):
            fields=action.split(':',2)
            if len(fields)==3 and self.consume_owner_confirmation(uid,fields[1],fields[2]):
                if fields[1]=='photo_delete':
                    target,oid=fields[2][:2],fields[2][2:]
                    if target not in ('r:','c:') or not SAFE_ID.fullmatch(oid):return
                    await self.api.call('DELETE','/v1/photos/'+pth_id(oid),params={'target':'replies' if target=='r:' else 'comments'},retries=0)
                    self.db.log(uid,'photo_delete',oid)
                    return await self.say(uid,'✓ Фото удалено.')
                return await self.owner_action(uid,fields[1],fields[2])
        return await super().callback(uid,query)

    async def handle(self,update:dict)->None:
        msg=update.get('message') or {}
        callback=update.get('callback_query') or {}
        chat=(msg or callback.get('message') or {}).get('chat',{})
        if chat.get('type')=='private':
            actor=(msg or callback).get('from') or {}
        else:
            actor=(update.get('pre_checkout_query') or {}).get('from') or {}
        if actor.get('id'):
            self.db.touch_user(int(actor['id']),actor.get('first_name',''),actor.get('last_name',''),actor.get('username',''))
        if chat.get('type')=='private' and (msg.get('photo') or msg.get('document')):
            uid=int(msg['from']['id'])
            self.db.register(uid)
            if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
            if await self.handle_upload(uid,msg):return
        return await super().handle(update)

    async def command(self,uid:int,text:str)->bool:
        if await self.admin_command(uid,text):return True
        cmd=text.split(maxsplit=1)[0].lower().split('@')[0]
        if cmd=='/promo':
            fields=text.split()
            if len(fields)!=2 or not re.fullmatch(r'[a-fA-F0-9]{16}',fields[1]):
                await self.say(uid,'Введи <code>/promo КОД</code>.')
                return True
            expiry=self.db.redeem_promo(uid,fields[1])
            await self.say(uid,'✓ Промокод активирован. Подписка до '+self.format_date(expiry)+'.' if expiry else
                           'Промокод недействителен или уже использован.',keyboard([('↶ Обзор','home',None)]))
            return True
        if cmd in ('/grant','/revoke','/block','/unblock','/setdays','/limit'):
            if not self.owner(uid):return True
            words=text.split()
            try:
                if cmd in ('/grant','/setdays') and len(words)==3:
                    target=int(words[1]);days=int(words[2])
                    self.db.modify_access(target,uid,'extend' if cmd=='/grant' else 'replace',days)
                elif cmd in ('/revoke','/block','/unblock') and len(words)==2:
                    target=int(words[1]);self.db.modify_access(target,uid,cmd[1:])
                elif cmd=='/limit' and len(words)==4:
                    target=int(words[1]);self.db.change_limit(target,uid,words[2],int(words[3]))
                else:raise ValueError()
                await self.admin_user(uid,target)
            except ValueError:
                await self.say(uid,'Формат: /grant ID DAYS; /setdays ID DAYS; /revoke ID; /block ID; /unblock ID; /limit ID daily|open VALUE. Дни: 1–36500.')
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
        data=str(query.get('data',''))
        if len(data.encode('utf-8'))>64:return
        if data.startswith('route:'):
            if not self.owner(uid):return
            data=self.owner_routes.get(data,'')
            if not data:
                return await self.say(uid,'<b>↻ Этот экран устарел</b>\nОткрой нужный раздел заново.',
                                      keyboard([('↶ Панель владельца','admin',None)]))
            query={**query,'data':data}
        if await self.admin_callback(uid,data):return
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
            self.db.save_draft(uid,'owner_confirmation','confirm',{'action':'posttpl_delete','id':oid})
            return await self.say(uid,'Удалить шаблон подписи <code>'+h(oid)+'</code>?',self.owner_keyboard(
                [('× Удалить','posttpl_delete:'+oid,'danger')],[('↶ Отмена','owner_show:post_templates',None)]))
        if data.startswith('posttpl_delete:') and self.owner(uid):
            oid=data[15:]
            if not SAFE_ID.fullmatch(oid) or not self.consume_owner_confirmation(uid,'posttpl_delete',oid):return
            await self.api.call('DELETE','/v1/posts/templates/'+pth_id(oid),retries=0)
            self.db.log(uid,'posttpl_delete',oid)
            return await self.say(uid,'✓ Шаблон подписи удалён.')
        if data.startswith('source_reply:'):
            tid=data[len('source_reply:'):]
            source=self.db.task(tid,uid)
            if not self.is_reply_source(source):
                return await self.say(uid,'Доступен только завершённый собственный сбор.')
            if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
            self.save_wizard_step(uid,'source_reply','replies',{'source_task':tid})
            return await self.source_reply_field(uid)
        if data.startswith('source:') or data in ('source_reply_submit','source_reply_skip'):
            return await self.source_reply_action(uid,data)
        return await self._callback_extended(uid,query)

    async def input_draft(self,uid:int,text:str)->None:
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
                if d['step']=='replies':
                    data['replies']=limited_lines(text,100,700)
                    self.save_wizard_step(uid,'source_reply','options',data)
                    return await self.source_reply_field(uid)
                if d['step']=='options':
                    data['options']=validate_task_options(parse_options(text))
                    self.save_wizard_step(uid,'source_reply','review',data)
                    return await self.source_reply_preview(uid)
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
        if verb=='cancel':
            self.db.clear_draft(uid)
            return await self.task(uid,data['source_task'])
        if verb=='back' and step in ('options','review'):
            self.save_wizard_step(uid,'source_reply','replies' if step=='options' else 'options',data)
            return await self.source_reply_field(uid)
        if verb=='skip' and step=='options':
            self.save_wizard_step(uid,'source_reply','review',{**data,'options':{}})
            return await self.source_reply_preview(uid)
        if verb=='submit' and step=='review':return await self.submit_source_reply(uid)
        if verb=='template' and step=='review':
            saved={key:data[key] for key in ('replies','options') if key in data}
            return await self.save_order_template(uid,'Ответы на собранное','source_reply',saved,'source:review:'+token)
        if verb=='review' and step=='review':return await self.source_reply_preview(uid)

    async def source_template_orders(self,uid:int,tid:str,page:int=0)->None:
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        item=self.db.template(uid,tid)
        if not item or item['mode']!='source_reply':return await self.say(uid,'Шаблон не найден.')
        size=10
        sources=self.db.completed_collections(uid,size+1,page*size)
        rows=[[(f'▤ {source["display_name"][:38]} · {source["task_id"]}',
                f'source_tpl_use:{tid}:{source["task_id"]}',None)] for source in sources[:size]]
        rows.extend([pagination(f'source_tpl_page:{tid}:',page,len(sources)>size),
                     [('↶ Шаблоны','templates',None)]])
        hint='Выбери завершённый сбор комментариев.' if sources else 'На этой странице нет завершённых сборов.'
        await self.say(uid,'<b>↪ Источник для ответов</b>\n\n'+hint,keyboard(*rows))

    async def use_source_template(self,uid:int,tid:str,source_id:str)->None:
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        item=self.db.template(uid,tid)
        source=self.db.task(source_id,uid)
        if not item or item['mode']!='source_reply' or not self.is_reply_source(source):
            return await self.say(uid,'Шаблон или исходный сбор недоступен.')
        if not self.db.account_ids(uid):return await self.create(uid)
        data=json.loads(item['payload'])
        self.save_wizard_step(uid,'source_reply','review',{**data,'source_task':source_id})
        await self.source_reply_preview(uid)

    @staticmethod
    def is_reply_source(source:dict|None)->bool:
        return bool(source and source['kind']=='fetch' and source['state']=='completed' and source['upstream_id'])

    async def source_reply_field(self,uid:int,*,error:str=''):
        draft=self.db.draft(uid)
        if not draft or draft['mode']!='source_reply':return
        step=draft['step']
        if step=='review':return await self.source_reply_preview(uid)
        token=self.wizard_token(draft)
        if step=='replies':
            text='<b>↪ Ответы на собранное</b>\nШаг 1 из 2 · Тексты\n\nОтправь тексты сообщением или TXT, один ответ на строку.'
            rows=[[('↶ К исходной задаче','source:cancel:'+token,None)]]
        else:
            text=('<b>↪ Ответы на собранное</b>\nШаг 2 из 2 · Настройки\n\n'
                  'Оставь стандартные настройки или отправь JSON, например <code>{"repliesPerComment":1}</code>.')
            rows=[[('→ Стандартные','source:skip:'+token,'primary')],
                  [('↶ Назад','source:back:'+token,None),('× Отменить','source:cancel:'+token,'danger')]]
        if error:text+='\n\n<b>! Проверь ввод</b>\n'+h(error)
        await self.say(uid,text,keyboard(*rows))

    async def source_reply_preview(self,uid:int):
        d=self.db.draft(uid)
        if not d or d['mode']!='source_reply':return
        src=self.db.task(d['data']['source_task'],uid)
        if not self.is_reply_source(src):return await self.say(uid,'Исходный сбор недоступен.')
        token=self.wizard_token(d)
        rows=[('Сбор',src['task_id']),('Текстов',len(d['data']['replies'])),('Аккаунтов',len(self.db.account_ids(uid)))]
        try:
            plan=await self.api.call('POST','/v1/tasks/'+pth_id(src['upstream_id'])+'/reply-plan',
                                    payload={'targetIds':[],'withPhotos':False},retries=0)
            if type(plan.get('totalReplies')) is int:
                rows.append(('Оценка по общим настройкам',number(plan['totalReplies'])))
        except ApiError:
            rows.append(('Прогноз','Недоступен'))
        rich,plain=table_html('↪ Проверка ответов',rows)
        await self.tg.rich(uid,rich,plain,keyboard(
            [('✓ Поставить в очередь','source:submit:'+token,'success')],
            [('▧ Сохранить шаблон','source:template:'+token,None)],
            [('↶ Назад','source:back:'+token,None),('× Отменить','source:cancel:'+token,'danger')]))

    async def submit_source_reply(self,uid:int):
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        d=self.db.draft(uid)
        if not d or d['mode']!='source_reply' or d['step']!='review':return
        src=self.db.task(d['data']['source_task'],uid)
        if not self.is_reply_source(src):return await self.say(uid,'Исходный сбор недоступен.')
        ids=self.db.account_ids(uid)
        if not ids:return await self.say(uid,'Нет назначенных аккаунтов.')
        payload={'sourceJob':src['upstream_id'],'replySourceMode':'all',
                 'assets':{'accountIds':ids,'replyTexts':d['data']['replies']},
                 'settings':{},'usePhoto':False,
                 'disableCommentPhotos':True,'disableReplyPhotos':True}
        try:
            payload['settings']=validate_task_options(d['data'].get('options',{}))
            tid=self.db.queue_task(uid,'Ответы · '+src['display_name'][:45],'publish',payload,
                max_daily=self.cfg.max_tasks_per_day,max_open=self.cfg.max_open_tasks,draft_token=self.wizard_token(d))
            return await self.task(uid,tid)
        except ValueError as exc:return await self.say(uid,'Нельзя создать: '+h(exc))

    async def submit(self,uid:int)->None:
        d=self.db.draft(uid)
        if d and d['mode']=='source_reply':return await self.submit_source_reply(uid)
        return await self._submit_extended(uid)

    async def preset_actions(self,uid:int,index:int):
        if not self.owner(uid) or not 0<=index<100:return
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
            await self.api.call('PATCH','/v1/posts/drafts/'+pth_id(data['id']),payload=data['body'],retries=0)
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
