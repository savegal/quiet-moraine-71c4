from __future__ import annotations
import asyncio
from datetime import datetime,timezone
from html import escape
import json
import logging
import re
from secrets import token_hex
import time
from typing import Any

from .api import ApiError,Telegram,Tokora,TelegramError
from .config import Config
from .db import MAX_USER_ID, Store, TERMINAL_STATES
from .presentation import keyboard,home,date_from_unix,report_view,state_label,state_symbol,task_view,h,days_label,number,pagination,table_html
from .updates import UpdateInbox

log=logging.getLogger(__name__)
VIDEO_ID=re.compile(r'/video/(\d{10,30})(?:\b|[/?#])')
VALID_ID=re.compile(r'^[a-zA-Z0-9_\-]{1,128}$')
TOK_URL=re.compile(r'^https?://(?:[a-z0-9-]+\.)?(?:tiktok\.com|tiktokv\.com)(?:/|$)',re.I)


def lines(text:str,max_count:int=30,max_length:int=400)->list[str]:
    values=[x.strip() for x in text.splitlines() if x.strip()]
    if not values or len(values)>max_count or any(len(x)>max_length for x in values):
        raise ValueError(f'Нужно от 1 до {max_count} строк (до {max_length} символов каждая).')
    return values


def links(text:str)->list[str]:
    arr=lines(text,15,350)
    if any(not (TOK_URL.match(x) or VIDEO_ID.search(x) or x.isdecimal() and 10<=len(x)<=30) for x in arr):
        raise ValueError('Пришли ссылки TikTok или числовые ID видео, каждый с новой строки.')
    return arr


MAIN_MENU_TEXT='⌂ Главное меню'
MAIN_MENU_KEYBOARD={'keyboard':[[{'text':MAIN_MENU_TEXT}]],'resize_keyboard':True,'is_persistent':True}


def safe_api_error(code:str)->str:
    # Do not pass upstream error.message/advice/media URLs into the user interface.
    return {
        'subscription_required':'Сервис временно недоступен. Попробуй чуть позже.',
        'api_scope_denied':'Это действие сейчас недоступно.',
        'api_key_invalid':'Сервис временно недоступен. Попробуй чуть позже.',
        'missing_proxies':'Для выполнения нужны настроенные прокси.',
        'accounts_burned':'Доступные аккаунты исчерпаны или временно недоступны.',
        'job_busy':'Сервер занят. Задание остаётся в очереди.',
        'rate_limited':'Превышен лимит запросов; повторите позже.',
        'connection_unavailable':'Сервис временно не отвечает.',
        'video_not_resolved':'Не удалось получить ID видео. Проверь ссылку.',
    }.get(code,'Операция не выполнена. Попробуй позже или обратись к администратору.')


class BotApp:
    def __init__(self,config:Config,db:Store,tg:Telegram,api:Tokora):
        self.cfg,self.db,self.tg,self.api=config,db,tg,api

    def owner(self,uid:int)->bool:return uid==self.cfg.owner_id

    def format_date(self,unix:int)->str:
        return date_from_unix(unix,self.cfg.timezone_name)

    async def say(self,uid:int,text:str,markup:dict|None=None)->dict:
        return await self.tg.send(uid,text,markup)

    async def show_home(self,uid:int)->None:
        user=self.db.user(uid) or {}
        text,menu=home(user.get('expires_at',0),self.owner(uid),self.db.counts())
        await self.say(uid,text,menu)

    async def access(self,uid:int)->None:
        user=self.db.user(uid) or {}
        status='Администратор' if self.owner(uid) else ('✓ до '+self.format_date(user.get('expires_at',0)) if self.db.is_active(uid,self.cfg.owner_id) else '○ Нет подписки')
        if user.get('blocked'):status='■ Заблокирован'
        text=f'<b>◇ Подписка</b>\n\n├ Доступ: <b>{h(status)}</b>\n└ Твой ID: <code>{uid}</code>'
        rows=[]
        if user.get('blocked'):
            text+='\n\nДоступ заблокирован. Обратись к администратору.'
        elif self.cfg.plans() and not self.owner(uid):
            text+='\n\n<b>▤ Тарифы · Telegram Stars</b>\nВыбери срок. Оплата продлит текущую подписку.'
            plans=[(f'◇ {days_label(days)} · {number(stars)} XTR',f'buy:{days}','success')
                   for days,stars in self.cfg.plans().items()]
            rows.extend(plans[i:i+2] for i in range(0,len(plans),2))
        else:
            text+='\n\nПодписку выдаёт администратор команды.'
        if hasattr(self.db,'redeem_promo') and not user.get('blocked') and not self.owner(uid):
            rows.append([('◇ Промокод','promo',None)])
        rows.append([('↶ Обзор','home',None)])
        await self.say(uid,text,keyboard(*rows))

    async def settings(self,uid:int)->None:
        text=('<b>⚙ Настройки</b>\n\n'
           f'├ Личный ID: <code>{uid}</code>\n'
           f'├ Назначено аккаунтов: <code>{len(self.db.account_ids(uid))}</code>\n'
           '├ Отчёты: Telegram Rich Messages\n'
           '└ Доступом управляет администратор команды.')
        await self.say(uid,text,keyboard([('◇ Доступ','access',None)],[('↶ Обзор','home',None)]))

    async def accounts(self,uid:int,page:int=0)->None:
        ids=self.db.account_ids(uid)
        if not ids:
            await self.say(uid,'<b>◇ Мои аккаунты</b>\n\nАккаунты ещё не назначены. Передай администратору свой ID.\n'
                           f'<code>{uid}</code>',keyboard([('↶ Обзор','home',None)]));return
        size=10
        page=min(page,(len(ids)-1)//size)
        visible=ids[page*size:(page+1)*size]
        nav=pagination('accounts_page:',page,(page+1)*size<len(ids))
        try:
            account_map=await self.assigned_accounts(visible)
            listing=[]
            for index,aid in enumerate(visible,page*size+1):
                item=account_map.get(aid,{})
                profile=item.get('profile') or {}
                name=profile.get('username')
                username='@'+name if isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9_.]{1,32}',name) else f'Аккаунт {index}'
                status=self.account_status(item.get('status','unknown'))
                listing.append(f'<b>{h(username)}</b> · {status}')
            text=f'<b>◇ Мои аккаунты</b>\n{number(len(ids))} в пуле · стр. {page+1}\n\n'+'\n\n'.join(listing)
        except ApiError as exc:
            text='<b>◇ Мои аккаунты</b>\n\n'+safe_api_error(exc.code)
        await self.say(uid,text,keyboard(nav,[('◈ Прокси','proxies',None)],[('↶ Обзор','home',None)]))

    @staticmethod
    def account_status(status:str)->str:
        return {'active':'✓ Активен','expired':'× Сессия истекла','error':'! Ошибка'}.get(status,'○ Статус неизвестен')

    async def assign_account(self,uid:int,account_id:str)->str:
        account=await self.api.call('GET','/v1/accounts/'+account_id)
        if account.get('accountId')!=account_id:raise ApiError('invalid_response')
        self.db.assign(uid,account_id)
        message='✓ Аккаунт назначен.'
        if account.get('status')!='active':
            status=self.account_status(str(account.get('status') or ''))
            message+='\n└ Сессия · '+h(status)+'\nПроверь её перед первым заказом.'
        return message

    async def assigned_accounts(self,ids:list[str])->dict:
        if not ids:return {}
        ids=set(ids)
        account_map={}
        for offset in range(0,2000,200):
            page=await self.api.account_page(offset)
            accounts=page.get('accounts',[])
            for item in accounts:
                aid=str(item.get('accountId'))
                if aid in ids:account_map[aid]=item
            if len(account_map)==len(ids) or len(accounts)<200:return account_map
        # For a large cabinet, fetch only the still-missing assigned accounts.
        for aid in sorted(ids-account_map.keys()):
            if not VALID_ID.fullmatch(aid):continue
            try:account=await self.api.call('GET','/v1/accounts/'+aid,retries=0)
            except ApiError as exc:
                if exc.status==404:continue
                raise
            if account.get('accountId')==aid:account_map[aid]=account
        return account_map

    async def proxies(self,uid:int)->None:
        # Do not expose credentials, raw proxy gateways, or all workspace proxy records.
        ids=self.db.account_ids(uid)
        text=('<b>◈ Прокси</b>\n\n'
           f'Твой пул: <code>{len(ids)}</code> привязанных аккаунтов.\n'
           'Прокси управляются администратором. Адреса, логины и пароли здесь не отображаются.')
        if self.owner(uid):
            try:
                data=await self.api.proxies()
                text+='\n\nВсего записей в кабинете: <code>'+str(len(data.get('items',[])))+'</code>.'
            except ApiError:text+='\n\nНе удалось получить статистику прокси.'
        await self.say(uid,text,keyboard([('↶ Обзор','home',None)]))

    async def stats(self,uid:int)->None:
        local=self.db.recent_tasks(uid,200,include_hidden=True)
        published=moderation=pending=attempts=0
        for item in local:
            snap=json.loads(item['snapshot_json'] or '{}')
            published+=int(snap.get('published') or 0)
            moderation+=int(snap.get('moderation') or 0)
            pending+=int(snap.get('pending') or 0)
            attempts+=int(snap.get('attempts') or 0)
        summary=(f'<h2>▤ Статистика</h2><p>Только твои задачи (последние {len(local)}).</p>'
          f'<table bordered striped compact><tr><th>Показатель</th><th>Количество</th></tr>'
          f'<tr><td>Задачи</td><td>{len(local)}</td></tr>'
          f'<tr><td>Подтверждено</td><td>{published}</td></tr>'
          f'<tr><td>Попыток</td><td>{attempts}</td></tr>'
          f'<tr><td>На модерации</td><td>{moderation}</td></tr>'
          f'<tr><td>Не подтверждено</td><td>{pending}</td></tr></table>'
          '<footer>Показатели отражают последний опрошенный статус, без двойного подсчёта заданий.</footer>')
        plain=(f'<b>▤ Статистика</b>\nЗадач: {len(local)}\n'
           f'Подтверждено: {published}\nПопыток: {attempts}\nМодерация: {moderation}\nНе подтверждено: {pending}')
        await self.tg.rich(uid,summary,plain,keyboard([('↶ Обзор','home',None)]))

    async def tasks(self,uid:int,page:int=0)->None:
        records=self.db.recent_tasks(uid,11,page*10)
        tasks=records[:10]
        rows=[[('＋ Создать задачу','create','success')]]
        rows.extend([(f'{state_symbol(t["state"])} {t["task_id"]} · {state_label(t["state"])}',
                      'task:'+t['task_id'],None)] for t in tasks)
        rows.extend([pagination('tasks_page:',page,len(records)>10),[('↶ Обзор','home',None)]])
        text=f'<b>▣ Мои задачи</b> · стр. {page+1}\n\n'+('\n\n'.join(
            f'<code>{h(t["task_id"])}</code> · <b>{h(t["display_name"][:45])}</b>\n'
            f'└ {state_symbol(t["state"])} {state_label(t["state"])}'
            for t in tasks) if tasks else 'На этой странице задач нет.')
        await self.say(uid,text,keyboard(*rows))

    async def task(self,uid:int,task_id:str)->None:
        t=self.db.task(task_id,uid)
        if not t:
            await self.say(uid,'Задача не найдена.');return
        buttons=[[('▤ Отчёт','report:'+task_id,'primary'),('↻ Обновить','task:'+task_id,None)]]
        if t['state'] in ('running','starting','stopping') and t['upstream_id']:
            buttons.append([('✓ Проверить статусы','verify:'+task_id,None)])
            buttons.append([('■ Остановить','stopconfirm:'+task_id,'danger')])
        if t['state']=='completed' and t['kind']=='publish':
            buttons.append([('↻ Доответить','replyinfo:'+task_id,None),('◇ Долайкать','likeinfo:'+task_id,None)])
        if t['state']=='queued':buttons.append([('× Отменить из очереди','cancel:'+task_id,'danger')])
        buttons.append([('↶ Мои задачи','tasks',None)])
        await self.say(uid,task_view(t),keyboard(*buttons))

    async def report(self,uid:int,task_id:str)->None:
        t=self.db.task(task_id,uid)
        if not t:return await self.say(uid,'Задача не найдена.')
        if not t['upstream_id']:return await self.say(uid,'Задача ещё не запущена.')
        try:
            report=await self.api.publication(t['upstream_id'])
            rich,plain=report_view(t,report)
            await self.tg.rich(uid,rich,plain,keyboard([('⇩ Экспорт','exports:'+task_id,None)],
                                                      [('↶ К задаче','task:'+task_id,None)]))
        except ApiError as exc: await self.say(uid,safe_api_error(exc.code))

    def save_verified_snapshot(self,job:dict,result:dict|None)->None:
        if not isinstance(result,dict) or result.get('id')!=job['upstream_id'] or not result.get('state'):
            return
        self.db.transition(job['task_id'],job['state'],snapshot=result,expected_state=job['state'])

    async def refresh_task(self,uid:int,job:dict)->None:
        if job['upstream_id'] and job['state'] in TERMINAL_STATES:
            result=await self.api.task(job['upstream_id'])
            self.save_verified_snapshot(job,result)
        await self.task(uid,job['task_id'])

    async def create(self,uid:int)->None:
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        if not self.db.account_ids(uid):
            return await self.say(uid,'Сначала администратор должен назначить тебе аккаунты.\nТвой ID: <code>'+str(uid)+'</code>')
        await self.say(uid,'<b>＋ Новая задача</b>\n\nВыбери действие:',keyboard(
          [('▣ Комментарии / ответы','new:publish','primary')],
          [('▤ Сбор комментариев','new:fetch',None)],
          [('◇ Лайки комментариев','new:commentlike',None)],
          [('↶ Обзор','home',None)]))

    async def begin(self,uid:int,kind:str)->None:
        if kind not in ('publish','fetch','commentlike'):return
        if not self.db.is_active(uid,self.cfg.owner_id) or not self.db.account_ids(uid):return await self.create(uid)
        self.db.save_draft(uid,kind,'name',{})
        await self.say(uid,'<b>Новая задача</b>\n\n1 / 4 · Введи название задачи (до 60 символов).\n\n/cancel — отмена')

    async def input_draft(self,uid:int,text:str)->None:
        draft=self.db.draft(uid)
        if not draft:return
        kind,step,data=draft['mode'],draft['step'],draft['data']
        if step=='review':return await self.say(uid,'Проверь параметры и нажми «Поставить в очередь» либо /cancel.')
        try:
            if step=='name':
                if not 1<=len(text.strip())<=60:raise ValueError('Название: от 1 до 60 символов.')
                data['name']=text.strip(); next_step='links'
                msg=('2 / 4 · Пришли ссылки на КОНКРЕТНЫЕ комментарии TikTok, каждый с новой строки (до 15).'
                    if kind=='commentlike' else
                    '2 / 4 · Отправь ссылки TikTok, по одной в строке (до 15). Можно присылать ID видео.')
            elif step=='links':
                data['links']=links(text)
                next_step='comments' if kind=='publish' else 'review'
                msg='3 / 4 · Отправь тексты комментариев, каждый с новой строки (до 30).' if kind=='publish' else ''
            elif step=='comments':
                data['comments']=lines(text)
                next_step='replies';msg='4 / 4 · Отправь тексты ответов на комментарии либо нажми «Пропустить».'
            elif step=='replies':
                data['replies']=lines(text); next_step='review';msg=''
            else:return
            self.db.save_draft(uid,kind,next_step,data)
            if next_step=='review':return await self.preview(uid)
            if next_step=='replies':
                await self.say(uid,msg,keyboard([('→ Пропустить ответы','skipreplies',None)]))
            else:await self.say(uid,msg)
        except ValueError as exc:
            await self.say(uid,'<b>Проверь ввод:</b> '+h(exc))

    async def preview(self,uid:int)->None:
        draft=self.db.draft(uid)
        if not draft or draft['step']!='review':return
        data=draft['data'];kind=draft['mode']
        mode={'publish':'Комментарии и ответы','fetch':'Сбор комментариев','commentlike':'Лайки комментариев'}[kind]
        text=(f'<b>▣ Проверка задачи</b>\n\n'
          f'├ Название: <b>{h(data["name"])}</b>\n'
          f'├ Режим: {mode}\n'
          f'├ Видео/ссылок: {len(data["links"])}\n'
          f'├ Текстов комментариев: {len(data.get("comments",[]))}\n'
          f'├ Текстов ответов: {len(data.get("replies",[]))}\n'
          f'└ Аккаунтов в пуле: {len(self.db.account_ids(uid))}\n\n'
          'Задача будет выполняться с назначенными тебе аккаунтами. Используй только материалы и аккаунты, на работу с которыми есть разрешение.')
        await self.say(uid,text,keyboard(
           [('✓ Поставить в очередь','submit','success')],
           [('× Отменить','discard','danger')]))

    async def submit(self,uid:int)->None:
        draft=self.db.draft(uid)
        if not draft or draft['step']!='review':return await self.say(uid,'Черновик не найден.')
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        data=draft['data'];kind=draft['mode']
        try:
            vids=[]
            for url in data['links']:
                direct=VIDEO_ID.search(url)
                if url.isdecimal():video=url
                elif direct:video=direct.group(1)
                else:video=await self.api.resolve(url)
                if video not in vids:vids.append(video)
            if not vids:raise ValueError('Ссылки не распознаны')
            ids=self.db.account_ids(uid)
            payload={'awemeIds':vids,'assets':{'accountIds':ids},
                 'disableCommentPhotos':True,'disableReplyPhotos':True,'usePhoto':False}
            if kind=='publish':
                payload['assets'].update({'commentTexts':data['comments'],'replyTexts':data.get('replies',[])})
                payload['settings']={'commentsPerVideo':1,'repliesPerVideo':1 if data.get('replies') else 0,
                    'workers':3,'likeEnabled':False,'likeComments':False,'likeReplies':False}
            elif kind=='fetch':
                payload['settings']={'fetchCount':50,'includeReplies':True,'fetchWorkers':3}
            else:  # commentlike: official TaskStart permits likeLinks; resolve underlying IDs upstream.
                payload['likeLinks']=data['links']
                payload['likesPerComment']=1
            task_id=self.db.queue_task(uid,data['name'],kind,payload,max_daily=self.cfg.max_tasks_per_day,max_open=self.cfg.max_open_tasks)
            self.db.clear_draft(uid)
            await self.say(uid,'<b>✓ Задача создана</b>\n\nID: <code>'+task_id+'</code>\n'+
                ('Ожидает запуска по очереди.' if self.cfg.dispatch_enabled else 'Добавлена в очередь, но выполнение отключено администратором.'),
                keyboard([('▣ Открыть задачу','task:'+task_id,'primary')],[('↶ Обзор','home',None)]))
        except ApiError as exc:
            await self.say(uid,'Ошибка проверки ссылок: '+safe_api_error(exc.code)+'\nПопробуй ещё раз.')
        except ValueError as exc:
            await self.say(uid,'Ошибка задачи: '+h(exc))

    async def topup_info(self,uid:int,job:dict,kind:str)->None:
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        if not job['upstream_id'] or job['state'] not in ('completed','stopped') or job['kind']!='publish':
            return await self.say(uid,'Дополнительный запуск доступен после завершения задачи.')
        try:
            from urllib.parse import quote
            endpoint='reply-topup' if kind=='reply' else 'like-topup'
            info=await self.api.call('GET','/v1/tasks/'+quote(job['upstream_id'],safe='')+'/'+endpoint)
            if not info.get('available'):
                return await self.say(uid,'Для этой задачи дополнительные действия сейчас недоступны.')
            if kind=='reply':
                title='Повторная отправка ответов'
                amount=int(info.get('failedComments') or info.get('failed') or 0)
                extra=f'Неполных основных комментариев: <code>{amount}</code>.'
            else:
                title='Дополнение лайков'
                comments=int(info.get('likesPerItem') or 0)
                replies=int(info.get('likesPerReply') or 0)
                extra=f'Цель лайков на комментарий / ответ · <code>{comments} / {replies}</code>.'
            token=token_hex(6)
            self.db.save_draft(uid,'task_topup','review',{'source_task':job['task_id'],'kind':kind,'wizard_token':token})
            await self.say(uid,'<b>↻ '+h(title)+'</b>\n\n'+extra+'\n\nПовторный запуск формируется отдельной задачей с теми же назначенными аккаунтами.',
                keyboard([('✓ Поставить в очередь','topup:'+token,'success')],[('↶ Назад','task:'+job['task_id'],None)]))
        except ApiError as exc:await self.say(uid,safe_api_error(exc.code))

    async def submit_topup(self,uid:int,source:dict,kind:str,token:str)->None:
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        if source['state'] not in ('completed','stopped') or not source['upstream_id'] or source['kind']!='publish':
            return await self.say(uid,'Исходная задача ещё не завершена.')
        if kind not in ('reply','like'):return
        try:
            from urllib.parse import quote
            endpoint='reply-topup' if kind=='reply' else 'like-topup'
            info=await self.api.call('GET','/v1/tasks/'+quote(source['upstream_id'],safe='')+'/'+endpoint)
            if not info.get('available'):return await self.say(uid,'Дополнительный запуск недоступен.')
            original=json.loads(source['payload_json'])
            assigned=set(self.db.account_ids(uid))
            ids=[a for a in original.get('assets',{}).get('accountIds',[]) if a in assigned]
            if not ids:return await self.say(uid,'Нет доступных назначенных аккаунтов.')
            payload={'assets':{'accountIds':ids},'disableCommentPhotos':True,'disableReplyPhotos':True,'usePhoto':False}
            if kind=='reply':
                texts=original.get('assets',{}).get('replyTexts',[])
                if not texts:return await self.say(uid,'Исходная задача не содержит сохранённых текстов ответов.')
                payload.update({'sourceJob':source['upstream_id'],'replySourceMode':'failed',
                    'assets':{'accountIds':ids,'replyTexts':texts},
                    'settings':{'repliesPerComment':max(1,min(int(info.get('repliesPerComment') or 1),20)),
                        'workers':3}})
                job_kind='publish'; label='Доответить'
            else:
                like_comments=max(0,min(int(info.get('likesPerItem') or 0),100))
                like_replies=max(0,min(int(info.get('likesPerReply') or 0),100))
                if not (like_comments or like_replies):return await self.say(uid,'Цель для дополнительных лайков равна нулю.')
                payload.update({'likeSourceJob':source['upstream_id'],'settings':{
                    'likesPerItem':like_comments,'likesPerComment':like_comments,'likesPerReply':like_replies,
                    'likeEnabled':True,'likeComments':bool(like_comments),'likeReplies':bool(like_replies)}})
                job_kind='commentlike';label='Долайкать'
            import sqlite3
            try:
                task_id=self.db.queue_task(uid,label+' · '+source['display_name'][:35],job_kind,payload,
                    followup_source=source['task_id'],followup_mode=kind,max_daily=self.cfg.max_tasks_per_day,
                    max_open=self.cfg.max_open_tasks,draft_token=token)
            except sqlite3.IntegrityError:
                return await self.say(uid,'Такое дополнительное задание уже создано. Открой «Мои задачи».')
            await self.task(uid,task_id)
        except ApiError as exc:await self.say(uid,safe_api_error(exc.code))
        except ValueError as exc:await self.say(uid,h(exc))

    async def admins(self,uid:int)->None:
        if not self.owner(uid):return
        stats=self.db.counts()
        await self.say(uid,'<b>▣ Панель управления</b>\n\n'+ '\n'.join(f'├ {k}: <code>{v}</code>' for k,v in stats.items())+
          '\nЗапуск заданий: <b>'+('включён' if self.cfg.dispatch_enabled else 'выключен')+'</b>\n\n'
          '/grant ID DAYS — выдать доступ\n/revoke ID — заблокировать доступ\n/assign ID ACCOUNT_ID — назначить аккаунт\n/unassign ID ACCOUNT_ID — отвязать\n/health — состояние API\n/pool — первые 40 аккаунтов\n\n'
          'Оплата Stars включается через STARS_30D_PRICE в .env.',
          keyboard([('↶ Обзор','home',None)]))

    async def health(self,uid:int)->None:
        if not self.owner(uid):return
        reads=(
            ('Аккаунты','accounts:read','/v1/accounts',{'limit':1}),
            ('Прокси','proxies:read','/v1/proxies',None),
            ('Настройки','settings:read','/v1/settings',None),
            ('Публикации','posts:read','/v1/posts',{'limit':1}),
            ('Материалы','materials:read','/v1/assets/awemeIds',None),
        )
        rows=[('Отправка заказов','Включена' if self.cfg.dispatch_enabled else 'Отключена')]
        try:
            tasks=await self.api.active_tasks()
            rows.append(('Задачи · tasks:read',f'✓ Доступ · активных {len(tasks)}'))
        except ApiError as exc:
            if exc.status==401 or exc.code=='api_key_invalid':
                return await self.say(uid,'<b>! Tokora API отклонил ключ</b>\n\n'
                    'Возьми действующий ключ <code>tk_…</code> в профиле Tokora → API и обнови '
                    '<code>TOKORA_API_KEY</code> в <code>.env</code>. Затем перезапусти бота.')
            if exc.status==429:return await self.say(uid,safe_api_error(exc.code))
            rows.append(('Задачи · tasks:read',exc.code))

        async def check_read(label,scope,path,params):
            try:
                await self.api.call('GET',path,params=params,retries=0)
                result='✓ Доступ'
            except ApiError as exc:
                result='Нет права' if exc.status==403 else exc.code
            return label+' · '+scope,result

        rows.extend(await asyncio.gather(*(check_read(*item) for item in reads)))
        rich,plain=table_html('▣ Состояние API',rows,caption='Проверены права чтения. Запись и исполнение заказов требуют отдельного теста.')
        await self.tg.rich(uid,rich,plain,keyboard([('↶ Панель владельца','admin',None)]))

    async def command(self,uid:int,text:str)->bool:
        parts=text.split()
        cmd=parts[0].split('@')[0].lower()
        if cmd in ('/start','/home'):
            if cmd=='/start':await self.say(uid,'Кнопка «Главное меню» закреплена внизу.',MAIN_MENU_KEYBOARD)
            await self.show_home(uid);return True
        if cmd=='/cancel':
            self.db.clear_draft(uid);await self.say(uid,'Черновик отменён.');return True
        if cmd=='/tasks':
            await self.tasks(uid);return True
        if cmd=='/access':
            await self.access(uid);return True
        if cmd in ('/terms','/paysupport','/support'):
            await self.say(uid,'<b>Proxima Soft · поддержка</b>\nДоступ предоставляется на указанный срок. Для вопросов об оплате и возврате обратись к администратору команды.\n\nИспользуй только свои аккаунты и разрешённые материалы.')
            return True
        if cmd=='/help':
            await self.say(uid,'<b>Proxima Soft</b>\n/start — меню\n/tasks — задания\n/cancel — отменить ввод\n/access — доступ\n\nВопросы по доступу — администратору команды.');return True
        if cmd=='/pool':
            if not self.owner(uid):return True
            try:
                page=await self.api.account_page(0,40)
                content=['<b>◇ Аккаунты API · первые 40</b>']
                for a in page.get('accounts',[]):
                    account_id=str(a.get('accountId') or '')
                    profile=a.get('profile') or {}
                    content.append('├ <code>'+h(account_id)+'</code> · '+h(str(profile.get('username',''))[:32]))
                await self.say(uid,'\n'.join(content)[:4000])
            except ApiError as exc:await self.say(uid,safe_api_error(exc.code))
            return True
        if cmd in ('/admin','/health'):
            await (self.admins(uid) if cmd=='/admin' else self.health(uid));return True
        if cmd in ('/grant','/revoke','/assign','/unassign'):
            if not self.owner(uid):return True
            try:
                if cmd=='/grant' and len(parts)==3:
                    user_id=int(parts[1]);days=int(parts[2])
                    if not (0<days<=365):raise ValueError()
                    expiry=self.db.grant(user_id,days)
                    result=f'✓ Доступ выдан до {self.format_date(expiry)}.'
                elif cmd=='/revoke' and len(parts)==2:
                    self.db.block(int(parts[1]));result='✓ Доступ заблокирован.'
                elif cmd in ('/assign','/unassign') and len(parts)==3 and VALID_ID.fullmatch(parts[2]):
                    user_id=int(parts[1]);account_id=parts[2]
                    if not 0<user_id<=MAX_USER_ID:raise ValueError('Некорректный Telegram ID.')
                    if cmd=='/assign':
                        result=await self.assign_account(user_id,account_id)
                    else:
                        result='✓ Аккаунт отвязан.' if self.db.unassign(user_id,account_id) else 'Привязка не найдена.'
                else:raise ValueError()
                await self.say(uid,result)
            except (ValueError,ApiError):await self.say(uid,'Команда не выполнена. Проверь аргументы и наличие аккаунта.')
            return True
        return False

    async def callback(self,uid:int,query:dict)->None:
        data=query.get('data','')
        if len(data)>80:return
        if data in ('home','access','settings'):
            await {'home':self.show_home,'access':self.access,'settings':self.settings}[data](uid)
        elif data=='buy' or data.startswith('buy:'):
            suffix=data.removeprefix('buy:')
            if data!='buy' and not re.fullmatch(r'[0-9]{1,3}',suffix):return
            days=self.cfg.subscription_days if data=='buy' else int(suffix)
            price=self.cfg.plans().get(days)
            if not price or self.owner(uid) or (self.db.user(uid) or {}).get('blocked'):
                return await self.access(uid)
            payload=self.db.create_invoice(uid,price,days)
            await self.tg.invoice(uid,payload,price,days)
        elif data=='accounts':await self.accounts(uid)
        elif re.fullmatch(r'accounts_page:[0-9]{1,5}',data):
            await self.accounts(uid,int(data.split(':')[1]))
        elif data=='proxies':await self.proxies(uid)
        elif data=='stats':await self.stats(uid)
        elif data=='tasks':await self.tasks(uid)
        elif re.fullmatch(r'tasks_page:[0-9]{1,7}',data):
            await self.tasks(uid,int(data.split(':')[1]))
        elif data=='create':await self.create(uid)
        elif data=='admin':await self.admins(uid)
        elif data.startswith('new:'):
            await self.begin(uid,data.split(':',1)[1])
        elif data=='skipreplies':
            draft=self.db.draft(uid)
            if draft and draft['step']=='replies':
                self.db.save_draft(uid,draft['mode'],'review',{**draft['data'],'replies':[]})
                await self.preview(uid)
        elif data=='submit':await self.submit(uid)
        elif data=='discard':
            self.db.clear_draft(uid);await self.show_home(uid)
        elif data.startswith('topup:'):
            draft=self.db.draft(uid)
            token=data[6:]
            if not draft or draft['mode']!='task_topup' or draft['data'].get('wizard_token')!=token or time.time()-draft['updated_at']>600:
                return await self.say(uid,'Подтверждение устарело. Открой дополнительное действие в карточке заказа.')
            source=self.db.task(draft['data']['source_task'],uid)
            if source:await self.submit_topup(uid,source,draft['data']['kind'],token)
        elif ':' in data:
            action,id_part=data.split(':',1)
            if not re.fullmatch('[0-9a-f]{8}',id_part):return
            job=self.db.task(id_part,uid)
            if not job:return await self.say(uid,'Нет доступа к этой задаче.')
            if action=='task':await self.task(uid,id_part)
            elif action=='refresh':await self.refresh_task(uid,job)
            elif action=='report':await self.report(uid,id_part)
            elif action=='replyinfo':await self.topup_info(uid,job,'reply')
            elif action=='likeinfo':await self.topup_info(uid,job,'like')
            elif action=='cancel':
                if self.db.cancel_queued(id_part,uid):await self.say(uid,'✓ Задача удалена из очереди.')
                else:await self.say(uid,'Задача уже запущена или отменена.')
            elif action=='stopconfirm':
                await self.say(uid,'Остановить задачу <code>'+id_part+'</code>?',keyboard(
                   [('■ Да, остановить','stop:'+id_part,'danger')],[('↶ Нет','task:'+id_part,None)]))
            elif action=='stop' and job['state'] in ('running','paused') and job['upstream_id']:
                try:
                    await self.api.stop(job['upstream_id'])
                    self.db.transition(id_part,'stopping',expected_state=job['state'])
                    await self.say(uid,'Остановка запрошена. Обнови статус через несколько секунд.')
                except ApiError as exc:await self.say(uid,safe_api_error(exc.code))
            elif action=='verify' and job['upstream_id'] and job['kind']=='publish' and job['state'] in (
                    'running','paused','completed','stopped'):
                if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
                accounts=set(json.loads(job['payload_json']).get('assets',{}).get('accountIds',[]))
                if not accounts or not accounts.issubset(self.db.account_ids(uid)):
                    return await self.say(uid,'Назначенные аккаунты изменились. Обратись к администратору.')
                try:
                    result=await self.api.verify(job['upstream_id'])
                    self.save_verified_snapshot(job,result)
                    await self.say(uid,'✓ Проверка принята. Открой обновлённую карточку.',keyboard(
                        [('↻ Обновить статус','refresh:'+id_part,'primary')]))
                except ApiError as exc:await self.say(uid,safe_api_error(exc.code))

    async def handle(self,update:dict)->None:
        pre=update.get('pre_checkout_query')
        if pre:
            uid=int(pre['from']['id']);currency=pre.get('currency');amount=pre.get('total_amount');payload=pre.get('invoice_payload','')
            ok=bool(currency=='XTR' and self.db.approve_invoice(uid,payload,amount,pre['id']))
            await self.tg.precheckout(pre['id'],ok,'Счёт недоступен. Открой подписку и создай новый счёт.')
            return
        callback=update.get('callback_query')
        if callback:
            uid=int(callback['from']['id'])
            if (callback.get('message') or {}).get('chat',{}).get('type')!='private':return
            self.db.register(uid)
            try:await self.tg.answer_callback(callback['id'])
            except TelegramError:pass
            # Expired users can still inspect/cancel/stop their existing tasks.
            # Only creation/submission is gated by subscription.
            cb_data=callback.get('data','')
            # Button presses update the pressed message in place where possible.
            pressed=(callback.get('message') or {}).get('message_id')
            token=self.tg.edit_in_place(uid,pressed) if pressed and hasattr(self.tg,'edit_in_place') else None
            try:
                if not self.db.is_active(uid,self.cfg.owner_id) and (cb_data in ('create','submit') or cb_data.startswith('new:')):
                    return await self.access(uid)
                return await self.callback(uid,callback)
            finally:
                if token is not None:self.tg.reset_edit(token)
        msg=update.get('message')
        if not msg or msg.get('chat',{}).get('type')!='private':return
        refund=msg.get('refunded_payment')
        payment=msg.get('successful_payment') or refund
        if payment:
            actor=msg['chat'].get('id') or (msg.get('from') or {}).get('id')
            if actor:await self.handle_payment(int(actor),payment,refunded=bool(refund),paid_at=msg.get('date'))
            return
        if not msg.get('from'):return
        uid=int(msg['from']['id']); self.db.register(uid)
        text=(msg.get('text') or '').strip()
        if text in (MAIN_MENU_TEXT,'Главное меню'):
            self.db.clear_draft(uid);return await self.show_home(uid)
        if text.startswith('/'):
            if await self.command(uid,text):return
        if not self.db.is_active(uid,self.cfg.owner_id):
            if self.db.draft(uid):return await self.access(uid)
            return await self.show_home(uid)
        if self.db.draft(uid):return await self.input_draft(uid,text)
        await self.show_home(uid)

    async def handle_payment(self,uid:int,payment:dict,*,refunded:bool=False,paid_at:int|None=None)->None:
        payload=payment.get('invoice_payload','')
        charge_id=payment.get('telegram_payment_charge_id','')
        stars=payment.get('total_amount')
        if payment.get('currency')!='XTR' or type(stars) is not int or stars<=0 or not charge_id:
            return
        if refunded:
            outcome=self.db.refund_invoice(uid,payload,stars,charge_id)
            if outcome=='duplicate':return
            if outcome=='recorded':
                await self.payment_notice(uid,f'✓ Возврат {number(stars)} XTR записан. Срок подписки пересчитан.')
            else:
                await self.payment_notice(self.cfg.owner_id,f'! Возврат для <code>{uid}</code> требует сверки. Открой «Оплаты».')
            return
        if self.db.charge_recorded(uid,payload,charge_id):return
        expiry=self.db.paid_invoice(uid,payload,stars,charge_id,self.cfg.subscription_days,paid_at=paid_at)
        if expiry is None:
            if self.db.record_payment_review('payment',uid,payload,stars,charge_id):
                await self.payment_notice(self.cfg.owner_id,f'! Оплата от <code>{uid}</code> требует сверки. Открой «Оплаты».')
                await self.payment_notice(uid,'Оплата требует сверки. Обратись к администратору.')
            return
        access_state=self.db.charge_access_state(charge_id)
        if access_state=='refunded':
            return await self.payment_notice(uid,'Возврат по этому платежу учтён.')
        if access_state=='superseded':
            await self.payment_notice(uid,'Платёж учтён. Срок подписки уже изменил администратор.')
            return await self.payment_notice(self.cfg.owner_id,f'! Платёж до ручного изменения доступа · <code>{uid}</code>. Проверь «Оплаты».')
        status='Подписка до '+self.format_date(expiry)+'.'
        if not self.db.is_active(uid,self.cfg.owner_id):
            status+=' Доступ заблокирован администратором.'
        await self.payment_notice(uid,'✓ Оплата подтверждена. '+status)
        if uid!=self.cfg.owner_id:
            await self.payment_notice(self.cfg.owner_id,f'✓ Оплата Stars · <code>{uid}</code> · {number(stars)} XTR')

    async def payment_notice(self,uid:int,text:str)->None:
        try:await self.say(uid,text)
        except Exception:log.warning('Could not deliver payment notification')

    async def handle_chat_update(self,item:dict)->None:
        try:
            await self.handle(item)
            return
        except ApiError as exc:
            log.warning('API error in chat update: HTTP %d',exc.status)
            error=safe_api_error(exc.code)
        except TelegramError as exc:
            log.warning('Telegram response failed: HTTP %d',exc.code)
            if exc.code in (403,429) or exc.code>=500:return
            error='Telegram не смог показать этот экран. Открой его заново.'
        except Exception as exc:
            log.warning('Chat update failed: %s',type(exc).__name__)
            error='Произошла ошибка. Попробуй повторить действие.'
        uid=((item.get('message') or item.get('callback_query') or {}).get('from') or {}).get('id')
        if uid:
            try:await self.say(int(uid),error)
            except Exception:pass

    async def run(self)->None:
        await UpdateInbox(self).run()
