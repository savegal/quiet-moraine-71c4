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

from .accounts import ACCOUNT_STATUS,account_counts,session_state,valid_task_folder
from .api import ApiError,Telegram,Tokora,TelegramError,lookup_accounts
from .config import Config
from .db import Store, TERMINAL_STATES, local_day_start, now, task_target_links_count
from .presentation import keyboard,home,date_from_unix,report_view,state_label,state_symbol,task_view,h,number,pagination,table_html
from .updates import UpdateInbox
from .worker import snapshot_state

log=logging.getLogger(__name__)
VIDEO_ID=re.compile(r'/video/(\d{10,30})(?:\b|[/?#])')
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


MAIN_MENU_TEXT='🏠 Главное меню'
MAIN_MENU_KEYBOARD={'keyboard':[[{'text':MAIN_MENU_TEXT}]],'resize_keyboard':True,'is_persistent':True}
PUBLICATION_REPORT_KINDS = {'publish', 'keyword_reply', 'commentlike'}
TOPUP_ORDER_MODES = {'reply': 'reply_links', 'like': 'commentlike'}


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
        'invalid_media':'Файл не удалось распознать. Проверь формат и попробуй загрузить его заново.',
        'media_too_large':'Файл превышает допустимый размер.',
        'quota_exceeded':'Место для медиа закончилось. Освободи место в черновиках.',
        'draft_limit':'Достигнут лимит черновиков.',
        'too_many_photos':'В альбоме уже максимальное количество фото.',
        'draft_in_queue':'Черновик участвует в очереди публикаций. Дождись завершения.',
        'no_drafts':'Выбери черновик для публикации.',
        'no_accounts':'Для плана нет подходящих аккаунтов. Проверь настройки публикаций.',
        'too_many_accounts':'В плане слишком много аккаунтов. Уменьши их число.',
        'plan_not_ready':'План ещё проверяется. Обнови его статус.',
        'plan_expired':'Срок плана истёк. Создай новый план для черновика.',
        'queue_full':'Очередь публикаций заполнена. Дождись освобождения места.',
        'post_not_cancellable':'Эту публикацию уже нельзя отменить.',
        'post_not_retryable':'Эту публикацию сейчас нельзя отправить повторно.',
        'confirm_required':'Для повтора публикации нужно подтверждение.',
    }.get(code,'Операция не выполнена. Попробуй позже или обратись к администратору.')


class BotApp:
    def __init__(self,config:Config,db:Store,tg:Telegram,api:Tokora):
        self.cfg,self.db,self.tg,self.api=config,db,tg,api
        self.db.owner_id = config.owner_id
        self.db.timezone_name = config.timezone_name
        self._home_stats_cache: dict[str, tuple[float, dict[str, int]]] = {}

    def dispatch_active(self)->bool:
        paused = hasattr(self.db,'dispatch_paused') and self.db.dispatch_paused()
        return self.cfg.dispatch_enabled and not paused

    def owner(self,uid:int)->bool:return uid==self.cfg.owner_id

    def mode_restricted(self, uid: int, mode: str) -> bool:
        return not self.owner(uid) and mode in self.cfg.restricted_modes

    def format_date(self,unix:int)->str:
        return date_from_unix(unix,self.cfg.timezone_name)

    async def say(self,uid:int,text:str,markup:dict|None=None)->dict:
        return await self.tg.send(uid,text,markup)

    def format_main_menu(self, metrics: dict[str, Any]) -> str:
        total_acc = number(metrics.get('total_accounts', 0))
        avail_acc = number(metrics.get('available_accounts', 0))
        c_today = number(metrics.get('comments_today', 0))
        r_today = number(metrics.get('replies_today', 0))
        m_today = number(metrics.get('moderation_today', 0))
        c_day = number(metrics.get('comments_day', 0))
        r_day = number(metrics.get('replies_day', 0))
        m_day = number(metrics.get('moderation_day', 0))
        c_week = number(metrics.get('comments_week', 0))
        r_week = number(metrics.get('replies_week', 0))
        m_week = number(metrics.get('moderation_week', 0))
        tot_pub = number(metrics.get('total_published', 0))

        return (
            '<b>🏠 Главное меню</b>\n\n'
            'Управляйте аккаунтами и прокси, собирайте комментарии и запускайте задачи с ответами. '
            'Здесь — актуальная статистика публикаций и доступ к основным разделам бота.\n'
            f'└ Аккаунты: {total_acc} · доступно {avail_acc}\n\n'
            '<b>Комментарии / Ответы / Модерация</b>\n'
            f'├ Сегодня: {c_today} / {r_today} / {m_today}\n'
            f'├ 24 часа: {c_day} / {r_day} / {m_day}\n'
            f'├ 7 дней: {c_week} / {r_week} / {m_week}\n'
            f'└ Всего опубликовано: {tot_pub}'
        )

    async def get_main_menu_data(self, uid: int | None = None) -> dict[str, int]:
        is_owner_scope = uid is None or self.owner(uid)
        cache_key = 'owner' if is_owner_scope else f'user:{uid}'
        now_ts = time.monotonic()
        if not isinstance(getattr(self, '_home_stats_cache', None), dict):
            self._home_stats_cache = {}
        cache_entry = self._home_stats_cache.get(cache_key)
        if cache_entry:
            cached_ts, cached_data = cache_entry
            if now_ts - cached_ts < 20.0:
                return dict(cached_data)

        def _safe_int(val: Any) -> int:
            try:
                return max(0, int(val))
            except (TypeError, ValueError, OverflowError):
                return 0

        if not is_owner_scope and uid is not None:
            user_ids = self.db.account_ids(uid) if hasattr(self.db, 'account_ids') else []
            total_accts = len(user_ids)
            avail_accts = total_accts
            if user_ids and hasattr(self, 'assigned_accounts') and hasattr(self.api, 'call'):
                try:
                    accounts = await asyncio.wait_for(self.assigned_accounts(user_ids), timeout=2.5)
                    if isinstance(accounts, dict):
                        avail_accts = sum(
                            1 for aid in user_ids
                            if isinstance(accounts.get(aid), dict) and session_state(accounts[aid]) == 'active'
                        )
                except Exception:
                    pass
            cur_time = now()
            today_start = local_day_start(cur_time, self.cfg.timezone_name)
            day_start = cur_time - 86400
            week_start = cur_time - 7 * 86400
            c_today = r_today = m_today = 0
            c_day = r_day = m_day = 0
            c_week = r_week = m_week = 0
            tot_pub = 0
            if hasattr(self.db, 'conn'):
                rows = self.db.conn.execute(
                    "SELECT kind, payload_json, snapshot_json, created_at FROM tasks "
                    "WHERE user_id=? AND kind IN ('publish','keyword_reply')",
                    (uid,),
                ).fetchall()
                for row in rows:
                    try:
                        payload = json.loads(row['payload_json'] or '{}')
                    except (TypeError, ValueError):
                        payload = {}
                    try:
                        snap = json.loads(row['snapshot_json'] or '{}')
                    except (TypeError, ValueError):
                        snap = {}
                    if not isinstance(snap, dict):
                        snap = {}
                    awemes = snap.get('awemes')
                    if isinstance(awemes, list) and awemes:
                        comm = sum(_safe_int(a.get('comments')) for a in awemes if isinstance(a, dict))
                        rep = sum(_safe_int(a.get('replies')) for a in awemes if isinstance(a, dict))
                    else:
                        pub = _safe_int(snap.get('published'))
                        is_reply_task = (
                            row['kind'] == 'keyword_reply'
                            or bool(payload.get('replyLinks') or payload.get('sourceJob'))
                        )
                        comm = 0 if is_reply_task else pub
                        rep = pub if is_reply_task else 0
                    mod = _safe_int(snap.get('moderation'))
                    pub_total = max(comm + rep, _safe_int(snap.get('published')))
                    tot_pub += pub_total
                    created_at = _safe_int(row['created_at'])
                    if created_at >= today_start:
                        c_today += comm; r_today += rep; m_today += mod
                    if created_at >= day_start:
                        c_day += comm; r_day += rep; m_day += mod
                    if created_at >= week_start:
                        c_week += comm; r_week += rep; m_week += mod
            data = {
                'total_accounts': total_accts,
                'available_accounts': avail_accts,
                'comments_today': c_today,
                'replies_today': r_today,
                'moderation_today': m_today,
                'comments_day': c_day,
                'replies_day': r_day,
                'moderation_day': m_day,
                'comments_week': c_week,
                'replies_week': r_week,
                'moderation_week': m_week,
                'total_published': tot_pub,
            }
            self._home_stats_cache[cache_key] = (now_ts, data)
            return data

        def _extract_totals(d: Any) -> tuple[int, int, int]:
            if not isinstance(d, dict):
                return 0, 0, 0
            c = _safe_int(d.get('comments', d.get('commentCount', 0)))
            r = _safe_int(d.get('replies', d.get('replyCount', 0)))
            m = _safe_int(d.get('moderation', d.get('moderationCount', 0)))
            return c, r, m

        async def _fetch_stats():
            if hasattr(self.api, 'stats'):
                return await self.api.stats()
            if hasattr(self.api, 'call'):
                return await self.api.call('GET', '/v1/statistics')
            return None

        async def _fetch_accounts():
            if hasattr(self.api, 'account_page'):
                return await self.api.account_page(0, 1)
            if hasattr(self.api, 'call'):
                return await self.api.call('GET', '/v1/accounts', params={'limit': 1, 'offset': 0})
            return None

        stats_res = None
        accts_res = None
        try:
            results = await asyncio.wait_for(
                asyncio.gather(_fetch_stats(), _fetch_accounts(), return_exceptions=True),
                timeout=4.0
            )
            if len(results) > 0 and not isinstance(results[0], Exception):
                stats_res = results[0]
            if len(results) > 1 and not isinstance(results[1], Exception):
                accts_res = results[1]
        except Exception as exc:
            log.warning('Failed to fetch home metrics from upstream: %s', exc)
            if cache_entry:
                return dict(cache_entry[1])

        c_today, r_today, m_today = 0, 0, 0
        c_day, r_day, m_day = 0, 0, 0
        c_week, r_week, m_week = 0, 0, 0
        c_all, r_all = 0, 0
        tot_pub = 0

        if isinstance(stats_res, dict):
            t_data = stats_res.get('today') or {}
            d_data = stats_res.get('day') or stats_res.get('24h') or stats_res.get('last24Hours') or {}
            w_data = stats_res.get('week') or stats_res.get('7d') or stats_res.get('last7Days') or {}
            a_data = stats_res.get('all') or stats_res.get('total') or {}

            c_today, r_today, m_today = _extract_totals(t_data)
            c_day, r_day, m_day = _extract_totals(d_data)
            c_week, r_week, m_week = _extract_totals(w_data)
            c_all, r_all, _ = _extract_totals(a_data)

            tot_pub = _safe_int(a_data.get('published', a_data.get('totalPublished')))
            if not tot_pub:
                tot_pub = c_all + r_all
            if not tot_pub:
                tot_pub = max(c_week + r_week, c_day + r_day, c_today + r_today)

        total_accts = 0
        avail_accts = 0

        if isinstance(accts_res, dict):
            counts = accts_res.get('counts')
            if isinstance(counts, dict):
                total_accts = _safe_int(counts.get('total', accts_res.get('total', 0)))
                avail_accts = _safe_int(counts.get('active', 0))
            else:
                total_accts = _safe_int(accts_res.get('total', len(accts_res.get('accounts') or [])))

            if avail_accts == 0 and total_accts > 0:
                try:
                    if hasattr(self.api, 'account_page'):
                        act_page = await asyncio.wait_for(self.api.account_page(0, 1, status='active'), timeout=2.5)
                    elif hasattr(self.api, 'call'):
                        act_page = await asyncio.wait_for(
                            self.api.call('GET', '/v1/accounts', params={'limit': 1, 'offset': 0, 'status': 'active'}),
                            timeout=2.5
                        )
                    else:
                        act_page = None
                    if isinstance(act_page, dict):
                        avail_accts = _safe_int(act_page.get('total', 0))
                except Exception:
                    pass

        if total_accts == 0 and uid is not None and hasattr(self.db, 'account_ids'):
            try:
                user_ids = self.db.account_ids(uid)
                if user_ids:
                    total_accts = len(user_ids)
                    avail_accts = len(user_ids)
            except Exception:
                pass

        data = {
            'total_accounts': total_accts,
            'available_accounts': avail_accts,
            'comments_today': c_today,
            'replies_today': r_today,
            'moderation_today': m_today,
            'comments_day': c_day,
            'replies_day': r_day,
            'moderation_day': m_day,
            'comments_week': c_week,
            'replies_week': r_week,
            'moderation_week': m_week,
            'total_published': tot_pub,
        }

        if stats_res is not None or accts_res is not None or not cache_entry:
            self._home_stats_cache[cache_key] = (now_ts, data)

        return data

    async def show_home(self,uid:int)->None:
        metrics=await self.get_main_menu_data(uid)
        text=self.format_main_menu(metrics)
        owner=self.owner(uid)
        menu=keyboard(
            [('▣ Мои задачи','tasks','primary')],
            [('＋ Создать задачу','create','success')],
            [('◇ Аккаунты','accounts',None),('◈ Прокси','proxies',None)],
            [('▤ Статистика','stats',None),('⚙ Настройки','settings',None)],
            [('◇ Подписка','access',None)]+([('▣ Панель владельца','admin','primary')] if owner else [])
        )
        await self.say(uid,text,menu)

    async def access(self,uid:int)->None:
        user=self.db.user(uid) or {}
        owner = self.owner(uid)
        active = self.db.is_active(uid,self.cfg.owner_id)
        assigned = len(self.db.account_ids(uid))
        status = 'Владелец' if owner else '■ Заблокирован' if user.get('blocked') else (
            '✓ до '+self.format_date(user.get('expires_at',0)) if active else '○ Доступ не выдан')
        text=f'<b>◇ Доступ к Proxima Soft</b>\n\n├ Статус · <b>{h(status)}</b>\n└ Telegram ID · <code>{uid}</code>'
        rows=[]
        if owner:
            rows.append([('▣ Управление командой','admin','primary')])
        elif user.get('blocked'):
            text+='\n\nОбратись к владельцу сервиса.'
        elif active:
            text+=f'\n\nНазначено аккаунтов · <b>{number(assigned)}</b>.'
            if assigned:
                rows.append([('◇ Мои аккаунты','accounts','primary'),('＋ Новая задача','create','success')])
            else:
                text+=' Попроси владельца назначить тебе рабочие аккаунты.'
        else:
            text+='\n\nПередай свой Telegram ID владельцу. Он выдаст срок доступа и назначит аккаунты.'
        if hasattr(self.db,'redeem_promo') and not user.get('blocked') and not owner:
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
                status=ACCOUNT_STATUS[session_state(account_map.get(aid))]
                listing.append(f'<b>{h(username)}</b> · {status}')
            text=f'<b>◇ Мои аккаунты</b>\n{number(len(ids))} в пуле · стр. {page+1}\n\n'+'\n\n'.join(listing)
        except ApiError as exc:
            text='<b>◇ Мои аккаунты</b>\n\n'+safe_api_error(exc.code)
        await self.say(uid,text,keyboard(nav,[('▥ Сводка пула','accounts_summary','primary')],[('↶ Обзор','home',None)]))

    @staticmethod
    def account_status(status:str)->str:
        return ACCOUNT_STATUS[session_state({'status':status})]

    async def assign_account(self,uid:int,account_id:str)->str:
        account=await self.api.call('GET','/v1/accounts/'+account_id)
        if account.get('accountId')!=account_id:raise ApiError('invalid_response')
        if not self.db.assign(uid,account_id,allow_transfer=False):
            raise ValueError('Аккаунт занят или уже закреплён за другим участником. Проверь его карточку.')
        message='✓ Аккаунт назначен.'
        if account.get('status')!='active':
            status=self.account_status(str(account.get('status') or ''))
            message+='\n└ Сессия · '+h(status)+'\nПроверь её перед первым заказом.'
        return message

    async def assigned_accounts(self,ids:list[str])->dict:
        return await lookup_accounts(self.api,ids)

    async def pool_summary(self,uid:int,target:int)->None:
        if uid!=target and not self.owner(uid):return
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        ids=self.db.account_ids(target)
        back=f'adm:uaccounts:{target}' if self.owner(uid) else 'accounts'
        try:accounts=await self.assigned_accounts(ids)
        except ApiError as exc:
            return await self.say(uid,safe_api_error(exc.code),keyboard([('↶ Аккаунты',back,None)]))
        counts=account_counts(ids,accounts)
        rows=[('Назначено',number(len(ids))),('Активны',number(counts['active'])),
              ('Сессия истекла',number(counts['expired'])),('Ошибка проверки',number(counts['error'])),
              ('Статус неизвестен',number(counts['unknown'])),('Не найдены',number(counts['missing']))]
        if self.owner(uid):
            rows.insert(0,('Telegram ID',target))
            if ids and accounts:
                folders={str((item or {}).get('folderId') or '').strip() for item in accounts.values() if isinstance(item,dict)}
                if len(folders)==1:
                    only=next(iter(folders))
                    rows.append(('Папки в кабинете',f'1 ({only}) · OK' if only else 'Без папки'))
                elif len(folders)>1:
                    rows.append(('Папки в кабинете',f'! Смешано ({len(folders)})'))
        rich,plain=table_html('▥ Состояние пула',rows,'Статусы последней проверки сессий.')
        route = f'check_pool:{target}' if self.owner(uid) else 'check_pool'
        actions = [('✓ Проверить сессии',route,'primary')] if ids else []
        await self.tg.rich(uid,rich,plain,keyboard(actions,[('↶ Аккаунты',back,None)]))

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
        owner = self.owner(uid)
        records=self.db.recent_tasks(uid,11,page*10)
        tasks=records[:10]
        rows=[[('▣ Все задачи','tasks','primary')]] if owner else []
        rows.append([('＋ Создать задачу','create','success')])
        rows.extend([(f'{state_symbol(t["state"])} {t["display_name"][:45]} · {state_label(t["state"])}',
                      'task:'+t['task_id'],None)] for t in tasks)
        rows.extend([pagination('bot_tasks:' if owner else 'tasks_page:',page,len(records)>10),[('↶ Обзор','home',None)]])
        title = 'Задачи из бота' if owner else 'Мои задачи'
        text=f'<b>▣ {title}</b> · стр. {page+1}\n\n'+('\n\n'.join(
            f'<code>{h(t["task_id"])}</code> · <b>{h(t["display_name"][:45])}</b>\n'
            f'└ {state_symbol(t["state"])} {state_label(t["state"])}'
            for t in tasks) if tasks else 'На этой странице задач нет.')
        rich='<h2>▣ '+h(title)+'</h2><footer>Страница '+str(page+1)+'</footer>'
        if tasks:
            for t in tasks:
                rich+='<p><b>'+h(t['display_name'][:70])+'</b><br>'+h(state_symbol(t['state'])+' '+state_label(t['state']))+'</p>'
        else:rich+='<p>Заказов пока нет. Создай первый — он появится здесь вместе со статусом выполнения.</p>'
        await self.tg.rich(uid,rich,text,keyboard(*rows))

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
        except ApiError as exc:
            message=('Подробный отчёт будет доступен после завершения заказа. Текущая статистика — в карточке.'
                     if exc.code=='job_running' else safe_api_error(exc.code))
            await self.say(uid,message,keyboard([('↶ К заказу','task:'+task_id,None)]))

    def save_verified_snapshot(self,job:dict,result:dict|None)->bool:
        if not isinstance(result,dict) or result.get('id')!=job['upstream_id']:return False
        state=snapshot_state(result,job['state'])
        if not state:return False
        error='execution_failed' if state=='failed' else ''
        expected_snapshot=job['snapshot_json'] if job['state'] in TERMINAL_STATES else None
        return self.db.transition(job['task_id'],state,snapshot=result,error_code=error,expected_state=job['state'],
                                  expected_snapshot=expected_snapshot)

    async def change_task_execution(self,uid:int,job:dict,action:str)->bool:
        required={'pause':'running','resume':'paused'}.get(action)
        if not self.owner(uid) or not required or job['state']!=required or not job['upstream_id']:return False
        from urllib.parse import quote
        result=await self.api.call('POST','/v1/tasks/'+quote(job['upstream_id'],safe='')+'/'+action,retries=0)
        self.save_verified_snapshot(job,result)
        self.db.log(uid,'task_'+action,job['task_id'])
        return True

    async def stop_task(self,job:dict)->None:
        result=await self.api.stop(job['upstream_id'])
        if (not isinstance(result,dict) or result.get('id')!=job['upstream_id']
                or not snapshot_state(result,job['state'])):
            raise ApiError('invalid_response')
        current=self.db.task(job['task_id'])
        if current and current['state'] in ('running','paused','stopping'):
            self.save_verified_snapshot(current,{**result,'stopRequested':True})

    async def refresh_task(self,uid:int,job:dict)->None:
        if job['upstream_id'] and job['state']!='reconcile':
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
            elif kind=='commentlike':
                payload['likeLinks']=data['links']
                payload['likesPerComment']=1
            task_id=self.db.queue_task(uid,data['name'],kind,payload,max_daily=self.cfg.max_tasks_per_day,max_open=self.cfg.max_open_tasks)
            self.db.clear_draft(uid)
            await self.say(uid,'<b>✓ Задача создана</b>\n\nID: <code>'+task_id+'</code>\n'+
                ('Ожидает запуска по очереди.' if self.dispatch_active() else 'Добавлена в очередь, но запуск приостановлен администратором.'),
                keyboard([('▣ Открыть задачу','task:'+task_id,'primary')],[('↶ Обзор','home',None)]))
        except ApiError as exc:
            await self.say(uid,'Ошибка проверки ссылок: '+safe_api_error(exc.code)+'\nПопробуй ещё раз.')
        except ValueError as exc:
            await self.say(uid,'Ошибка задачи: '+h(exc))

    @staticmethod
    def _reply_topup_source(info:dict[str,Any],fallback_targets:int=0)->tuple[str,int,int]:
        raw_failed=info.get('failed')
        if raw_failed is None:
            raw_failed=info.get('failedComments')
        failed=max(0,int(raw_failed or 0))
        total_all=max(0,int(info.get('all') or 0))
        if failed>0:
            return 'failed',failed,max(0,int(info.get('videosFailed') or 0)) or max(0,fallback_targets)
        if total_all>0:
            return 'all',total_all,max(0,int(info.get('videosAll') or 0)) or max(0,fallback_targets)
        return '',0,0

    async def topup_info(self,uid:int,job:dict,kind:str)->None:
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        if kind not in TOPUP_ORDER_MODES:return
        if self.mode_restricted(uid, TOPUP_ORDER_MODES[kind]):
            return await self.say(uid,'Этот вид заказа доступен только владельцу.')
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
                mode,amount,_=self._reply_topup_source(info)
                if not mode:
                    return await self.say(uid,'Нет доступных комментариев для повторного запуска ответов.')
                if mode=='failed':
                    extra=f'Неполных основных комментариев: <code>{amount}</code>.'
                else:
                    extra=f'Доступных основных комментариев для доотправки: <code>{amount}</code>.'
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
        if kind not in TOPUP_ORDER_MODES:return
        mode_required = TOPUP_ORDER_MODES[kind]
        if self.mode_restricted(uid, mode_required):
            return await self.say(uid,'Этот вид заказа доступен только владельцу.')
        if source['state'] not in ('completed','stopped') or not source['upstream_id'] or source['kind']!='publish':
            return await self.say(uid,'Исходная задача ещё не завершена.')
        try:
            from urllib.parse import quote
            endpoint='reply-topup' if kind=='reply' else 'like-topup'
            info=await self.api.call('GET','/v1/tasks/'+quote(source['upstream_id'],safe='')+'/'+endpoint)
            if self.mode_restricted(uid, mode_required):
                return await self.say(uid,'Этот вид заказа доступен только владельцу.')
            if not info.get('available'):return await self.say(uid,'Дополнительный запуск недоступен.')
            original=json.loads(source['payload_json'])
            base_targets=task_target_links_count(source)
            ids=self.db.eligible_task_accounts(uid,original.get('assets',{}).get('accountIds',[]),self.cfg.owner_id)
            if not ids:return await self.say(uid,'Нет доступных назначенных аккаунтов.')
            payload={'assets':{'accountIds':ids},'disableCommentPhotos':True,'disableReplyPhotos':True,'usePhoto':False}
            if uid == self.cfg.owner_id:
                launch_data = json.loads(source['launch_payload_json']) if source.get('launch_payload_json') else {}
                source_folder = (original.get('accountFolder')
                                 if 'accountFolder' in original else launch_data.get('accountFolder'))
                if valid_task_folder(source_folder, allow_workspace=True):
                    payload['accountFolder'] = source_folder
            if kind=='reply':
                mode,comments_count,target_links=self._reply_topup_source(info,fallback_targets=base_targets)
                if not mode:
                    return await self.say(uid,'Нет доступных комментариев для дополнительного запуска ответов.')
                texts=original.get('assets',{}).get('replyTexts',[])
                if not texts:return await self.say(uid,'Исходная задача не содержит сохранённых текстов ответов.')
                per_comment=max(1,min(int(info.get('repliesPerComment') or 1),20))
                orig_settings=original.get('settings') if isinstance(original.get('settings'),dict) else {}
                orig_rpv=max(0,int(orig_settings.get('repliesPerVideo') or 0))
                per_video=min(max(orig_rpv,comments_count*per_comment,per_comment),200000)
                payload.update({'sourceJob':source['upstream_id'],'replySourceMode':mode,
                    'assets':{'accountIds':ids,'replyTexts':texts},
                    'settings':{'repliesPerComment':per_comment,'repliesPerVideo':per_video,
                                'delaySeconds':2,'uniqueText':False}})
                job_kind='publish'; label='Доответить'
            else:
                target_links=base_targets
                like_comments=max(0,min(int(info.get('likesPerItem') or 0),100))
                like_replies=max(0,min(int(info.get('likesPerReply') or 0),100))
                if not (like_comments or like_replies):return await self.say(uid,'Цель для дополнительных лайков равна нулю.')
                payload.update({'likeSourceJob':source['upstream_id'],'settings':{
                    'likesPerItem':like_comments,'likesPerComment':like_comments,'likesPerReply':like_replies,
                    'delaySeconds':2,'likeEnabled':True,'likeComments':bool(like_comments),'likeReplies':bool(like_replies)}})
                job_kind='commentlike';label='Долайкать'
            import sqlite3
            try:
                task_id=self.db.queue_task(uid,label+' · '+source['display_name'][:35],job_kind,payload,
                    followup_source=source['task_id'],followup_mode=kind,max_daily=self.cfg.max_tasks_per_day,
                    max_open=self.cfg.max_open_tasks,draft_token=token,target_count=target_links)
            except sqlite3.IntegrityError:
                return await self.say(uid,'Такое дополнительное задание уже создано. Открой «Мои задачи».')
            await self.task(uid,task_id)
        except ApiError as exc:await self.say(uid,safe_api_error(exc.code))
        except ValueError as exc:await self.say(uid,h(exc))

    async def admins(self,uid:int)->None:
        if not self.owner(uid):return
        stats=self.db.counts()
        await self.say(uid,'<b>▣ Панель управления</b>\n\n'+ '\n'.join(f'├ {k}: <code>{v}</code>' for k,v in stats.items())+
          '\nЗапуск заданий: <b>'+('включён' if self.dispatch_active() else 'на паузе / выключен')+'</b>\n\n'
          '/grant ID DAYS — выдать доступ\n/revoke ID — заблокировать доступ\n/assign ID ACCOUNT_ID — назначить аккаунт\n/unassign ID ACCOUNT_ID — отвязать\n/health — состояние API\n/pool — первые 40 аккаунтов\n\n'
          'Подписки выдаются вручную.',
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
        rows=[('Отправка заказов','Включена' if self.dispatch_active() else 'Отключена / на паузе')]
        try:
            tasks=await self.api.active_tasks()
            rows.append(('Задачи · tasks:read',f'✓ Доступ · активных {len(tasks)}'))
        except ApiError as exc:
            reference = ('\nID запроса: <code>'+h(exc.request_id)+'</code>') if exc.request_id else ''
            if exc.status==401 or exc.code=='api_key_invalid':
                return await self.say(uid,'<b>! API отклонил ключ</b>\n\n'
                    'Обнови ключ доступа в настройках запуска. Затем перезапусти бота.'+reference)
            if exc.status==429:
                return await self.say(uid,safe_api_error(exc.code)+reference)
            rows.append(('Задачи · tasks:read',safe_api_error(exc.code)))

        async def check_read(label,scope,path,params):
            try:
                await self.api.call('GET',path,params=params,retries=0)
                result='✓ Доступ'
            except ApiError as exc:
                result='Нет права' if exc.status==403 else safe_api_error(exc.code)
            return label+' · '+scope,result

        rows.extend(await asyncio.gather(*(check_read(*item) for item in reads)))
        request_id = getattr(self.api, 'last_error_request_id', '')
        if request_id:
            rows.append(('Последний ID ошибки API', request_id))
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
        if cmd in ('/terms','/support'):
            await self.say(uid,'<b>Proxima Soft · поддержка</b>\nПодписку на выбранный срок выдаёт администратор команды.\n\nИспользуй только свои аккаунты и разрешённые материалы.')
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
        return False

    async def callback(self,uid:int,query:dict)->None:
        data=query.get('data','')
        if len(data)>80:return
        if data in ('home','access','settings'):
            await {'home':self.show_home,'access':self.access,'settings':self.settings}[data](uid)
        elif data=='accounts_summary':await self.pool_summary(uid,uid)
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
                    await self.stop_task(job)
                    await self.task(uid,id_part)
                except ApiError as exc:await self.say(uid,safe_api_error(exc.code))
            elif action=='verify' and job['upstream_id'] and job['kind']=='publish' and job['state'] in (
                    'running','paused','completed','stopped'):
                if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
                launched=json.loads(job.get('launch_payload_json') or job['payload_json'])
                accounts=set(launched.get('assets',{}).get('accountIds',[]))
                if not accounts or accounts != set(self.db.eligible_task_accounts(uid,list(accounts),self.cfg.owner_id)):
                    return await self.say(uid,'Назначенные аккаунты изменились. Обратись к администратору.')
                try:
                    result=await self.api.verify(job['upstream_id'])
                    self.save_verified_snapshot(job,result)
                    await self.task(uid,id_part)
                except ApiError as exc:await self.say(uid,safe_api_error(exc.code))

    async def _answer_callback_safe(self,callback_id:str)->None:
        try:await self.tg.answer_callback(callback_id)
        except TelegramError:pass

    async def handle(self,update:dict)->None:
        callback=update.get('callback_query')
        if callback:
            uid=int(callback['from']['id'])
            if (callback.get('message') or {}).get('chat',{}).get('type')!='private':return
            self.db.register(uid)
            # Run answerCallbackQuery concurrently with callback execution so clicks feel instant.
            ans_task=asyncio.create_task(self._answer_callback_safe(callback['id']))
            await asyncio.sleep(0)
            # Expired users can still inspect/cancel/stop their existing tasks.
            # Only creation/submission is gated by subscription.
            cb_data=callback.get('data','')
            # Button presses update the pressed message in place where possible.
            pressed=(callback.get('message') or {}).get('message_id')
            token=self.tg.edit_in_place(uid,pressed) if pressed and hasattr(self.tg,'edit_in_place') else None
            body_error:BaseException|None=None
            try:
                if not self.db.is_active(uid,self.cfg.owner_id) and (cb_data in ('create','submit') or cb_data.startswith('new:')):
                    return await self.access(uid)
                return await self.callback(uid,callback)
            except BaseException as exc:
                body_error=exc
                raise
            finally:
                if token is not None:self.tg.reset_edit(token)
                if isinstance(body_error,asyncio.CancelledError) and not ans_task.done():
                    ans_task.cancel()
                try:
                    await ans_task
                except BaseException:
                    if body_error is None:
                        raise
        msg=update.get('message')
        if not msg or msg.get('chat',{}).get('type')!='private':return
        if not msg.get('from'):return
        uid=int(msg['from']['id']); self.db.register(uid)
        text=(msg.get('text') or '').strip()
        if not text:return
        if text in (MAIN_MENU_TEXT,'Главное меню','⌂ Главное меню','🏠 Главное меню'):
            self.db.clear_draft(uid);return await self.show_home(uid)
        if text.startswith('/'):
            if await self.command(uid,text):return
        if not self.db.is_active(uid,self.cfg.owner_id):
            if self.db.draft(uid):return await self.access(uid)
            return await self.show_home(uid)
        if self.db.draft(uid):return await self.input_draft(uid,text)
        await self.show_home(uid)

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
