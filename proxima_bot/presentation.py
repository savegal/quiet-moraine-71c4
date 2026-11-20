from __future__ import annotations
from html import escape
from datetime import datetime
import json
import re
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .db import PUBLICATION_TIMER_KINDS, TERMINAL_STATES, now, snapshot_preparing, snapshot_publication_started, task_execution_limit_seconds
from .api import task_finish_pending

BUTTON_SYMBOLS = frozenset('▣◇◈▤▥▧⌂⌕⚙◷✓×↻＋←→↶↪↗⇩↑♡▷▶Ⅱ■✎−·○!')
ENGINE_REFERENCE = re.compile(
    r"""(?:https?://)?(?:[\w-]+\.)*tokora\.online(?:[/?#][^\s<>"'\\]*)?"""
    r"""|(?:https?://)?(?:t\.me|telegram\.me)/tokora[\w-]*(?:[/?#][^\s<>"'\\]*)?"""
    r'|(?:tg://resolve\?domain=|@|/)?tokora[\w-]*|токор[\w-]+',
    re.IGNORECASE,
)

STATE_DISPLAY = {
    'queued': ('◷', 'В очереди'),
    'starting': ('▷', 'Запуск'),
    'reconcile': ('↻', 'Проверка статуса'),
    'running': ('▷', 'В работе'),
    'paused': ('Ⅱ', 'На паузе'),
    'stopping': ('■', 'Остановка'),
    'completed': ('✓', 'Завершено'),
    'stopped': ('■', 'Остановлено'),
    'failed': ('!', 'Ошибка'),
    'canceled': ('×', 'Отменено'),
    'unknown': ('↻', 'Статус уточняется'),
}

MODE_SYMBOLS = {
    'publish': '▣',
    'reply_links': '↗',
    'fetch': '⌕',
    'commentlike': '♡',
    'keyword_reply': '↪',
    'feedwarm': '▷',
    'usernames': '◇',
    'check': '✓',
    'import': '↑',
}

TASK_PHASES = {
    'prepare':'Подготовка аккаунтов', 'preparing':'Подготовка аккаунтов',
    'init':'Подготовка аккаунтов', 'warming_prepare':'Подготовка аккаунтов',
    'publish':'Публикации начались', 'publishing':'Публикации начались',
    'comments':'Комментарии', 'replies':'Ответы',
    'verify':'Проверка публикаций', 'verifying':'Проверка публикаций',
    'likes':'Лайки комментариев', 'liking':'Лайки комментариев',
    'commentlike':'Лайки комментариев', 'like_comments':'Лайки комментариев',
    'fetch':'Сбор комментариев', 'collecting':'Сбор результатов',
    'settle':'Поздняя проверка публикаций', 'settling':'Поздняя проверка публикаций',
}


def h(value:Any) -> str:
    return escape(str(value if value is not None else ''),quote=True)


def public_system_text(text:str)->str:
    """Remove integration names and links from system-generated output."""
    return ENGINE_REFERENCE.sub('Proxima',text)


def button_text(label:str)->str:
    label=' '.join(label.split())
    if label and (label[0] in BUTTON_SYMBOLS or label.isdecimal()):
        return label
    return '◇ '+(label or 'Открыть')


def keyboard(*rows:list[tuple[str,str,str|None]],pack:Callable[[str],str]|None=None)->dict:
    """Each button: (label, callback_data, style). No custom emoji dependency."""
    result=[]
    for row in rows:
        items=[]
        for title,callback,style in row:
            if pack is not None and len(callback.encode())>64:
                callback=pack(callback)
            if not 1<=len(callback.encode())<=64:raise ValueError('callback too long')
            b={'text':button_text(title),'callback_data':callback}
            if style in ('primary','success','danger'):b['style']=style
            items.append(b)
        if items:result.append(items)
    return {'inline_keyboard':result}


def date_from_unix(unix:int,zone:str='Asia/Jerusalem')->str:
    if not unix:return '—'
    return datetime.fromtimestamp(unix,ZoneInfo(zone)).strftime('%d.%m.%Y %H:%M %Z')


def rich_time(unix:int,fallback:str)->str:
    return f'<tg-time unix="{int(unix)}" format="dt">{h(fallback)}</tg-time>'


def state_label(state:str)->str:
    return STATE_DISPLAY.get(state,('○','Статус уточняется'))[1] if state else '—'


def state_symbol(state:str)->str:
    return STATE_DISPLAY.get(state,('○',''))[0]


def pagination(prefix:str,page:int,has_more:bool)->list[tuple[str,str,str|None]]:
    rows=[]
    if page:
        rows.append(('← Ранее',prefix+str(page-1),None))
    if has_more:
        rows.append(('→ Далее',prefix+str(page+1),'primary'))
    return rows


def days_label(days:int)->str:
    if 11<=days%100<=14:
        return f'{days} дней'
    word={1:'день',2:'дня',3:'дня',4:'дня'}.get(days%10,'дней')
    return f'{days} {word}'


def minutes_label(minutes:int)->str:
    if 11<=minutes%100<=14:
        return f'{minutes} минут'
    word={1:'минута',2:'минуты',3:'минуты',4:'минуты'}.get(minutes%10,'минут')
    return f'{minutes} {word}'


def home(expires:int,owner:bool,counts:dict)->tuple[str,dict]:
    access='Администратор' if owner else ('Активен · до '+date_from_unix(expires) if expires else 'Нет доступа')
    text=(f'<b>▣ Proxima Soft</b>\n\n'
      'Инструменты задач и отчётности команды.\n'
      f'├ <b>Доступ:</b> {h(access)}\n'
      f'├ <b>Задач в очереди:</b> <code>{counts["queued"]}</code>\n'
      f'└ <b>В работе:</b> <code>{counts["running"]}</code>\n\n'
      'Выбери нужный раздел.')
    menu=keyboard(
        [('▣ Мои задачи','tasks','primary')],
        [('＋ Создать задачу','create','success')],
        [('◇ Аккаунты','accounts',None),('◈ Прокси','proxies',None)],
        [('▤ Статистика','stats',None),('⚙ Настройки','settings',None)],
        [('◇ Подписка','access',None)]+([('▣ Панель владельца','admin','primary')] if owner else [])
    )
    return text,menu


def count(value:Any)->int:
    try:
        return max(0,int(value))
    except (TypeError,ValueError,OverflowError):
        return 0


def number(value:Any)->str:
    return f'{count(value):,}'.replace(',',' ')


def percent(success:int,attempts:int)->str:
    return (f'{100*success/attempts:.2f}%'.replace('.',',') if attempts else '—')


def rich_table(headers, rows)->str:
    head=''.join(f'<th>{h(value)}</th>' for value in headers)
    body=''.join('<tr>'+''.join(f'<td>{h(value)}</td>' for value in row)+'</tr>' for row in rows)
    return '<table bordered striped compact><tr>'+head+'</tr>'+body+'</table>'


def table_html(title:str,items:list[tuple[str,Any]],caption:str='')->tuple[str,str]:
    rich='<h2>'+h(title)+'</h2>'+rich_table(('Показатель','Значение'),items)
    plain='<b>'+h(title)+'</b>\n\n'+'\n'.join(
        f'{"└" if i==len(items)-1 else "├"} <b>{h(key)}</b> · <code>{h(value)}</code>'
        for i,(key,value) in enumerate(items))
    if caption:
        rich+='<footer>'+h(caption)+'</footer>'
        plain+='\n\n'+h(caption)
    return rich,plain


ERROR_LABELS={
    'job_busy':'Сервис занят. Заказ остаётся в очереди, повторим запуск автоматически.',
    'rate_limited':'Достигнут лимит запросов. Повторим попытку после паузы.',
    'external_pool_unknown':'Заказ ждёт повторной проверки очереди.',
    'local_pool_unknown':'Бот уточняет пул незавершённого заказа другого пользователя.',
    'invalid_target':'Не удалось принять цели заказа. Проверь выбранные видео или комментарии и подтверди заказ снова.',
    'invalid_source':'Исходный сбор недоступен или ещё не завершён.',
    'missing_source':'Для выбранных комментариев нужен исходный сбор.',
    'empty_job':'В заказе нет комментариев для ответов.',
    'invalid_reply_links':'Не удалось разобрать список ссылок. Проверь его и повтори заказ.',
    'too_many_reply_links':'В одном заказе по ссылкам допускается до 300 непустых строк.',
    'invalid_order_targets':'Не удалось проверить ссылки заказа. Укажи корректную ссылку на видео или комментарий для выбранного действия.',
    'short_link_retry':'Короткая ссылка пока не открывается. Бот повторит проверку.',
    'short_link_unresolved':'Не удалось открыть короткую ссылку TikTok. Попробуй указать полную ссылку.',
    'account_assignment_changed':'Назначенные аккаунты изменились',
    'account_folder_mismatch':'Заказ ждёт настройки папки. В кабинете нужна отдельная папка со всеми аккаунтами заказа. Бот проверит папку снова.',
    'account_check_unavailable':'Не удалось проверить аккаунты и папку. Бот повторит проверку.',
    'no_active_accounts':'В пуле заказа нет аккаунтов с активной сессией.',
    'not_enough_active_accounts':'После проверки доступно меньше аккаунтов, чем указано в настройках заказа.',
    'admin_action_required':'Требуется действие администратора',
    'connection_unavailable':'Сбой соединения с сервером',
    'job_not_found':'Сервер не вернул задачу',
    'execution_failed':'Задача завершилась с ошибкой',
    'job_duplicate':'Такой заказ уже выполняется в кабинете.',
    'missing_proxies':'Для запуска нужны прокси',
    'accounts_burned':'Аккаунты недоступны',
    'missing_response_id':'Ожидается подтверждение запуска',
    'request_conflict':'Конфликт идентификатора запуска',
    'reconcile_review':'Задача на проверке. Статус скоро обновится.',
    'reconcile_access_revoked':'Доступ закрыт. Идёт сверка возможного запуска.',
    'upstream_absence_confirmed':'Владелец закрыл сверку заказа.',
    'task_limits':'Параметры заказа превышают лимиты бота.',
}

REPORT_REASONS={
    **ERROR_LABELS,
    'session_expired':'Сессия истекла',
    'publication_rejected':'Публикация отклонена',
    'post_rejected':'Публикация отклонена',
    'network_error':'Ошибка соединения',
}


def error_breakdown(errors:list[dict])->dict[str,int]:
    grouped={}
    for item in errors:
        if not isinstance(item,dict) or not count(item.get('count')):
            continue
        code=item.get('code')
        reason=REPORT_REASONS.get(code,'Другие ошибки') if isinstance(code,str) else 'Другие ошибки'
        grouped[reason]=grouped.get(reason,0)+count(item['count'])
    return grouped


RESULT_STATUS={
    'live':'Подтверждено','confirmed':'Подтверждено','published':'Опубликовано',
    'success':'Успешно','ok':'Успешно','sent':'Отправлено','pending':'Ожидает подтверждения',
    'moderation':'На модерации','failed':'Ошибка','error':'Ошибка','rejected':'Отклонено',
    'skipped':'Пропущено','existing':'Уже выполнено','collected':'Собрано',
    'found':'Найдено','candidate':'Кандидат','matched':'Подходит',
    'done':'Выполнено','partial':'Частично выполнено',
}


def result_status(value:Any)->str:
    return RESULT_STATUS.get(value.lower(),'Статус уточняется') if isinstance(value,str) else 'Статус уточняется'


REPLY_CHECK_NOTE=(
    'Даже если показано «0 ответов», ответы могли опубликоваться. '
    'Автоматическая проверка может их пропустить. '
    'Открой исходный комментарий в TikTok и проверь ответы вручную.'
)

COMMENT_CHECK_NOTE=(
    'Проверка комментариев очень часто ошибается: зачастую главные комментарии уже есть на видео, '
    'несмотря на число 0, поэтому желательно проверять комментарии вручную.'
)


def task_has_comments(task:dict)->bool:
    if task.get('kind')!='publish':
        return False
    payload=json.loads(task.get('payload_json') or '{}')
    assets=payload.get('assets') or {}
    if assets.get('commentTexts'):
        return True
    if payload.get('awemeIds') and not payload.get('replyLinks') and not payload.get('sourceJob'):
        return True
    snapshot=json.loads(task.get('snapshot_json') or '{}')
    if any(count(video.get('comments')) for video in snapshot.get('awemes') or [] if isinstance(video,dict)):
        return True
    settings={**(payload.get('settings') or {}),**(snapshot.get('settings') or {})}
    return bool(count(settings.get('commentsPerVideo')))


def task_execution_phase_banner(t:dict)->str:
    state=t.get('state')
    if t.get('settle_poll_at') or state not in ('starting','running','paused','stopping'):
        return ''
    snapshot=json.loads(t.get('snapshot_json') or '{}')
    limit_min=task_execution_limit_seconds(t)//60
    if state=='starting' or (snapshot and snapshot_preparing(snapshot)):
        return f'Подготовка аккаунтов · таймер выполнения ({minutes_label(limit_min)}) начнётся с начала публикаций.'
    if t.get('kind') in PUBLICATION_TIMER_KINDS:
        if not t.get('execution_started_at') and not snapshot_publication_started(snapshot):
            return f'Ожидание начала выполнения · лимит {minutes_label(limit_min)}.'
        return f'Публикации начались · лимит выполнения {minutes_label(limit_min)} с начала публикаций.'
    return ''


def task_has_replies(task:dict)->bool:
    if task.get('kind')=='keyword_reply':
        return True
    if task.get('kind')!='publish':
        return False
    payload=json.loads(task.get('payload_json') or '{}')
    assets=payload.get('assets') or {}
    if assets.get('replyTexts') or any(payload.get(key) for key in ('replyLinks','sourceJob','replyId')):
        return True
    snapshot=json.loads(task.get('snapshot_json') or '{}')
    if any(snapshot.get(key) for key in ('replyLinks','sourceJob','replyByKeywords')):
        return True
    if any(count(video.get('replies')) for video in snapshot.get('awemes') or [] if isinstance(video,dict)):
        return True
    settings=snapshot.get('settings') or {}
    return bool((settings.get('useReplyTelegram') and settings.get('telegram'))
                or (not payload and count(settings.get('repliesPerVideo'))))


def report_view(task:dict,report:dict)->tuple[str,str]:
    """Rich HTML with native Telegram table; safe fallback is ordinary HTML."""
    title=h(task['display_name']); idtag=h(public_system_text(task['task_id']))
    comm=report.get('comments') or {}; replies=report.get('replies') or {}
    l=report.get('likes') or {}; web=report.get('web') or {}
    status=f'{state_symbol(task["state"])} {state_label(task["state"])}'
    like_only=task.get('kind')=='commentlike' and bool(l)
    heading='▥ Отчёт по лайкам' if like_only else '▥ Отчёт по текстам'
    head=f'<h2>{heading}</h2><p><b>{title}</b> · <code>{idtag}</code><br>{h(status)}</p>'
    groups=() if like_only else (('Комментарии',comm),('Ответы',replies))
    def summary_row(label,group):
        published,attempts=count(group.get('published')),count(group.get('attempts'))
        return (label,f'{number(published)} / {number(attempts)}',percent(published,attempts),
                f'{number(group.get("moderation"))} / {number(group.get("pending"))}')
    def outcome_table(label,rows):
        return rich_table((label,'Успех / попытки','Мод. / ожид.'),
                          [(name,f'{ratio} · {rate}',pending) for name,ratio,rate,pending in rows])
    publication_rows=[summary_row(label,group) for label,group in groups]
    reply_note=not like_only and (task_has_replies(task) or any(
        replies.get(key) for key in ('attempts','published','failed','moderation','pending','texts')))
    comment_note=not like_only and (task_has_comments(task) or any(
        comm.get(key) for key in ('attempts','published','failed','moderation','pending','texts')))
    table=''
    if publication_rows:
        summary=[('Коммент.' if label=='Комментарии' else label,ratio,rate,pending)
                 for label,ratio,rate,pending in publication_rows]
        table='<h3>Публикации</h3>'+outcome_table('Тип',summary)
    if comment_note:
        table+='<p>'+h(COMMENT_CHECK_NOTE)+'</p>'
    if reply_note:
        table+='<p>'+h(REPLY_CHECK_NOTE)+'</p>'
    extras=[]
    if l:
        confirmed,total=count(l.get('confirmed')),count(l.get('total'))
        existing=count(l.get('existing'))
        extras.extend([
            ('Лайки · подтверждено / цель',f'{number(confirmed)} / {number(total)}'),
            ('Новых / было ранее',f'{number(confirmed-existing)} / {number(existing)}'),
            ('Попыток / повторов',f'{number(l.get("attempts"))} / {number(l.get("retries"))}'),
            ('Не подтверждено / ошибок',f'{number(l.get("unconfirmed"))} / {number(l.get("failed"))}'),
            ('Недобор',number(total-confirmed)),
        ])
    if web:
        extras.append(('Через веб / переходов с мобильного',f'{number(web.get("publications"))} / {number(web.get("fallbacks"))}'))
    if extras:
        heading_extra='♡ Лайки и веб-публикации' if l and web else '♡ Лайки комментариев' if l else '↗ Веб-публикации'
        table+=f'<h3>{heading_extra}</h3>'+rich_table(('Показатель','Значение'),extras)
    def texts(group,label):
        items=group.get('texts') or []
        if not items:return ''
        rows=[summary_row(str(t.get('text') or '—')[:155],t) for t in items[:20]]
        footer='<footer>Первые 20 текстов.</footer>' if len(items)>20 else ''
        return '<h3>'+h(label)+'</h3>'+outcome_table('Текст',rows)+footer
    errors=error_breakdown(report.get('errors') or [])
    error_section=''
    if errors:
        error_section='<details><summary>Ошибки · '+number(sum(errors.values()))+'</summary><ul>'+''.join(
            '<li>'+h(reason)+' · '+number(total)+'</li>' for reason,total in errors.items())+'</ul></details>'
    foot='' if like_only else '<footer>Успехами считаются только подтверждённые публикации. Модерация и неподтверждённые отправки в успехи не входят.</footer>'
    rich=head+table+texts(comm,'Тексты комментариев')+texts(replies,'Тексты ответов')+error_section+foot
    plain=(f'<b>{heading}</b>\n<b>{title}</b> · <code>{idtag}</code>\n{h(status)}\n\n'
       +'\n'.join(f'├ {label}: <code>{value}</code> · {rate}\n└ Модерация / ожидают: <code>{pending}</code>'
                 for label,value,rate,pending in publication_rows))
    plain+=''.join(f'\n├ {h(label)}: <code>{h(value)}</code>' for label,value in extras)
    if comment_note:
        plain+='\n\n'+h(COMMENT_CHECK_NOTE)
    if reply_note:
        plain+='\n\n'+h(REPLY_CHECK_NOTE)
    for label,group in groups:
        items=group.get('texts') or []
        if items:
            plain+=f'\n\n<b>{label} · по текстам</b>'
            for index,item in enumerate(items[:4],1):
                text,ratio,rate,pending=summary_row(str(item.get('text') or '—')[:105],item)
                plain+=f'\n{index}. {h(text)}\n└ <code>{ratio}</code> · {rate} · мод. / ожид. <code>{pending}</code>'
            if len(items)>4:plain+='\nПоказаны первые 4 текста.'
    return rich,plain


def task_metrics(t:dict,*,queue_paused:bool=False)->list[tuple[str,str]]:
    snapshot=json.loads(t['snapshot_json'] or '{}')
    preflight=json.loads(t.get('preflight_json') or '{}')
    names={'publish':'Комментарии и ответы','keyword_reply':'Ответы по ключевым фразам',
           'fetch':'Сбор комментариев','usernames':'Поиск юзернеймов','feedwarm':'Сбор видео',
           'commentlike':'Лайки комментариев','check':'Проверка аккаунтов','import':'Импорт аккаунтов'}
    rows=[('Вид заказа',names.get(t.get('kind'),'Заказ'))]
    errors=preflight.get('reply_link_error_lines')
    if isinstance(errors,list) and errors:
        lines=', '.join(str(line) for line in errors[:10] if type(line) is int)
        if lines:
            remaining=f' · ещё {len(errors)-10}' if len(errors)>10 else ''
            rows.append(('Ошибки в непустых строках',lines+remaining))
    if t['state']=='queued':
        stage = {'short_link_retry':'Ожидание повторной проверки ссылки',
                 'account_folder_mismatch':'Ожидание настройки папки аккаунтов',
                 'account_check_unavailable':'Ожидание проверки папки аккаунтов'}.get(t.get('error_code'))
        if not stage and not queue_paused:
            stage = 'Ожидание выбранного времени' if (t.get('fire_at') or 0)>now() else 'Ожидание свободной очереди кабинета'
        if stage:
            rows.insert(0,('Этап',stage))
    phase=preflight.get('phase')
    if phase=='checking':
        rows.append(('Аккаунты','Проверка состава папки и сессий'))
    elif phase=='checked':
        rows.append(('Готовы к запуску',f'{number(preflight.get("active"))} / {number(preflight.get("requested"))}'))
        excluded=[(label,count(preflight.get(key))) for key,label in
                  (('expired','истекли'),('error','ошибка'),('unknown','не проверены'),('missing','не найдены'))]
        if any(total for _,total in excluded):
            rows.append(('Без активной сессии',' · '.join(f'{label} {number(total)}' for label,total in excluded if total)))
    if not snapshot:
        if t['state']=='starting':
            rows.append(('Этап','Подготовка аккаунтов'))
            rows.append(('Лимит времени',minutes_label(task_execution_limit_seconds(t)//60)))
        return rows
    if t['state']=='stopping' and task_finish_pending(snapshot):
        rows.append(('Этап','Ожидается подтверждение завершения'))
    if t.get('settle_poll_at'):
        rows.append(('Этап','Поздняя проверка публикаций'))
    elif t['state'] in ('running','paused'):
        limit_min=task_execution_limit_seconds(t)//60
        raw_phase=snapshot.get('phase')
        if snapshot_preparing(snapshot):
            rows.append(('Этап','Подготовка аккаунтов'))
            rows.append(('Таймер выполнения',f'Ожидает начала публикаций ({minutes_label(limit_min)})'))
        else:
            stage_label=TASK_PHASES.get(raw_phase.lower(),'Выполнение задачи') if isinstance(raw_phase,str) and raw_phase else ''
            if stage_label:
                rows.append(('Этап',stage_label))
            if stage_label!='Публикации начались' and t.get('kind') in PUBLICATION_TIMER_KINDS and (
                t.get('execution_started_at') or snapshot_publication_started(snapshot)
            ):
                rows.append(('Статус публикаций','Публикации начались'))
            rows.append(('Лимит времени',minutes_label(limit_min)))
    done,total=count(snapshot.get('done')),count(snapshot.get('total'))
    unit={'fetch':'Видео обработано','usernames':'Видео обработано','feedwarm':'Обработано',
          'check':'Аккаунтов обработано','import':'Записей обработано'}.get(t.get('kind'),'Действий обработано')
    rows.append((unit,f'{number(done)} / {number(total)} · {min(100,100*done//total)}%' if total else number(done)))
    if type(snapshot.get('accountsWorking')) is int:
        rows.append(('Аккаунтов в работе',number(snapshot['accountsWorking'])))
    fields={
        'publish':(('published','Всего подтверждено'),('moderation','На модерации'),('pending','Ожидают подтверждения'),
                   ('attempts','Попыток'),('expired','Сессий истекло'),('webPublications','Через веб')),
        'keyword_reply':(('published','Подтверждено'),('moderation','На модерации'),('pending','Ожидают подтверждения'),('attempts','Попыток')),
        'commentlike':(('liked','Лайков подтверждено'),('likeTotal','Цель'),('likeExisting','Было ранее'),
                       ('likeAttempts','Попыток'),('likeRetries','Повторов'),('likeUnconfirmed','Не подтверждено'),
                       ('likeTargets','Комментариев для лайков'),('likeWaiting','Ожидают'),('likeSkipped','Пропущено')),
        'fetch':(('scanned','Просмотрено'),('collected','Собрано')),
        'usernames':(('scanned','Просмотрено'),('collected','Собрано')),
        'check':(('checked','Проверено'),('live','Активных'),('expired','Сессий истекло'),('checkRetries','Повторов проверки')),
        'import':(('imported','Импортировано'),('importCreated','Новых'),('importUpdated','Обновлено'),
                  ('importWebOnly','Только веб-сессия')),
        'feedwarm':(('scanned','Просмотрено'),('collected','Собрано')),
    }
    kind=t.get('kind')
    rows.extend((label,number(snapshot[key])) for key,label in fields.get(kind,()) if key in snapshot)
    if kind in ('publish','keyword_reply','commentlike'):
        for active,assigned,label in (('checkers','checkersTotal','Аккаунты проверки'),
                                      ('likers','likersTotal','Аккаунты для лайков')):
            if type(snapshot.get(assigned)) is int and snapshot[assigned]>0:
                rows.append((label,f'{number(snapshot.get(active))} / {number(snapshot[assigned])}'))
    if kind in ('publish','keyword_reply'):
        rows.extend((label,number(snapshot[key])) for key,label in
                    (('lateConfirmed','Поздние подтверждения'),
                     ('publishedAfterFinish','Публикации после завершения'))
                    if type(snapshot.get(key)) is int and snapshot[key]>0)
    if kind=='publish' and count(snapshot.get('likeTotal')):
        rows.append(('Лайки комментариев',f'{number(snapshot.get("liked"))} / {number(snapshot["likeTotal"])}'))
    if kind=='publish' and snapshot.get('awemes'):
        videos=[video for video in snapshot['awemes'] if isinstance(video,dict)]
        goals=[video['goal'] for video in videos if type(video.get('goal')) is bool]
        if goals:rows.append(('Цель выполнена',f'{sum(goals)} / {len(goals)} видео'))
    if kind=='feedwarm' and isinstance(snapshot.get('warm'),dict):
        warm=snapshot['warm']
        if 'searchMatched' not in warm and warm.get('searchDone') is True and type(warm.get('matched')) is int:
            warm={**warm,'searchMatched':warm['matched']}
        rows.extend((label,number(warm[key])) for key,label in
                    (('searchScanned','Видео проверено'),('searchMatched','Видео найдено'),
                     ('scrolled','Просмотрено в ленте'),('accountsDone','Аккаунтов завершили'),
                     ('accounts','Аккаунтов участвуют'),('failed','Ошибок сбора'),
                     ('shortActions','Действий не выполнено'),('skipped','Пропущено')) if key in warm)
    if kind=='usernames' and isinstance(snapshot.get('usernameStats'),dict):
        stats=snapshot['usernameStats']
        rows.extend((label,number(stats[key])) for key,label in
                    (('names','Telegram-юзернеймы'),('candidates','Кандидаты'),('mentions','Упоминания'),
                     ('candidateMentions','Упоминания кандидатов'))
                    if key in stats)
    if t['state'] in ('completed','stopped','failed','canceled') and snapshot.get('finishedAt'):
        try:
            start=datetime.fromisoformat(snapshot['createdAt'])
            finish=datetime.fromisoformat(snapshot['finishedAt'])
            seconds=max(0,(finish-start).total_seconds())
            rows.append(('Время выполнения',f'{seconds:.1f}'.replace('.',',')+' сек'))
        except (KeyError,TypeError,ValueError):
            pass
    if kind=='commentlike' and 'likeFailed' in snapshot:
        rows.append(('Ошибок лайков',number(snapshot['likeFailed'])))
    if kind not in ('publish','check') and type(snapshot.get('expired')) is int:
        rows.append(('Сессий истекло',number(snapshot['expired'])))
    if 'failed' in snapshot:
        rows.append(('Ошибок',number(snapshot['failed'])))
    if isinstance(snapshot.get('errorCounts'),list):
        reasons=error_breakdown(snapshot['errorCounts'])
        if reasons:
            rows.append(('Причины',' · '.join(f'{label} {number(total)}' for label,total in reasons.items())))
    return rows


def publication_card_rows(task:dict)->list[tuple[str,str,str]]:
    if task.get('kind') not in ('publish','keyword_reply'):return []
    snapshot=json.loads(task.get('snapshot_json') or '{}')
    payload=json.loads(task.get('payload_json') or '{}')
    settings={**(payload.get('settings') or {}),**(snapshot.get('settings') or {})}
    videos=[video for video in snapshot.get('awemes') or [] if isinstance(video,dict)]
    rows=[]
    for field,label,target in (('comments','Комментарии','commentsPerVideo'),('replies','Ответы','repliesPerVideo')):
        confirmed=number(sum(count(video.get(field)) for video in videos)) if videos else '—'
        goal=number(settings[target]) if type(settings.get(target)) is int else '—'
        rows.append((label,confirmed,goal))
    return rows


def snapshot_timeout(t:dict)->bool:
    return bool(json.loads(t.get('snapshot_json') or '{}').get('executionTimeLimit'))


def empty_video_search(t:dict, *, entry_total:int|None=None)->bool:
    if t.get('kind')!='feedwarm' or t.get('state')!='completed':
        return False
    snapshot=json.loads(t.get('snapshot_json') or '{}')
    warm=snapshot.get('warm')
    warm=warm if isinstance(warm,dict) else {}
    if warm.get('searchDone') is False or count(warm.get('failed')):
        return False
    counts = [snapshot.get('collected'), warm.get('searchMatched'), entry_total]
    if warm.get('searchDone') is True:
        counts.append(warm.get('matched'))
    known = [value for value in counts if type(value) is int and value >= 0]
    return bool(known) and max(known) == 0


def task_status(t:dict)->str:
    state=t['state']
    if state=='queued':return '◷ Заказ поставлен в очередь'
    if state in TERMINAL_STATES:
        upstream_state=str(json.loads(t.get('snapshot_json') or '{}').get('state') or '').strip().lower()
        if state=='failed' and upstream_state=='interrupted':return '! Выполнение прервано'
        if state=='completed' and upstream_state=='completed_with_errors':return '! Завершено с ошибками'
    return f'{state_symbol(state)} {state_label(state)}'


def task_notices(t:dict,queue_paused:bool)->list[str]:
    notices=[]
    if t.get('state')=='queued' and queue_paused:
        notices.append('Запуск приостановлен.')
    if snapshot_timeout(t):
        limit_sec=count(json.loads(t.get('snapshot_json') or '{}').get('executionLimitSeconds')) or task_execution_limit_seconds(t)
        limit=minutes_label(max(1,limit_sec//60))
        notices.append(f'Достигнут лимит выполнения ({limit}).')
    if t.get('error_code'):
        notices.append(ERROR_LABELS.get(t['error_code'],'Состояние требует проверки.'))
    if empty_video_search(t):
        notices.append('Ничего не нашлось. Измени тему поиска и создай новый сбор.')
    return notices


def task_view(t:dict,*,timezone_name:str='Asia/Jerusalem',queue_paused:bool=False)->str:
    body=f'<b>▣ {h(t["display_name"])}</b>\n<b>{h(task_status(t))}</b>\n'
    for notice in task_notices(t,queue_paused):
        body+='\n'+h(notice)+'\n'
    if fire_at:=t.get('fire_at'):
        body+='\n<b>Запланировано</b> · '+h(date_from_unix(fire_at,timezone_name))+'\n'
    if banner:=task_execution_phase_banner(t):body+='\n<b>'+h(banner)+'</b>\n'
    rows=task_metrics(t,queue_paused=queue_paused)
    for index,(label,value) in enumerate(rows):
        branch='└' if index==len(rows)-1 else '├'
        body+=f'\n{branch} {h(label)}: <code>{h(value)}</code>'
    publications=publication_card_rows(t)
    if publications:
        body+='\n\n<b>Публикации</b>'
        body+=''.join(f'\n{h(label)} · подтверждено <b>{confirmed}</b> · цель на видео {goal}'
                      for label,confirmed,goal in publications)
        body+='\nСчётчик обработанных действий включает попытки отправки.'
    if task_has_comments(t):
        body+='\n\n'+h(COMMENT_CHECK_NOTE)
    if task_has_replies(t):
        body+='\n\n'+h(REPLY_CHECK_NOTE)
    return body+'\n\nЗаказ · <code>'+h(public_system_text(t['task_id']))+'</code>'


def task_rich(t:dict,details:tuple[tuple[str,Any],...]=(),*,timezone_name:str='Asia/Jerusalem',queue_paused:bool=False)->str:
    head=(f'<h2>▣ {h(t["display_name"])}</h2>'
          f'<p><b>{h(task_status(t))}</b></p>')
    notices=task_notices(t,queue_paused)
    if notices:
        head+='<blockquote>'+'<br>'.join(h(notice) for notice in notices)+'</blockquote>'
    if fire_at:=t.get('fire_at'):
        head+='<p><b>Запланировано</b> · '+rich_time(fire_at,date_from_unix(fire_at,timezone_name))+'</p>'
    if banner:=task_execution_phase_banner(t):head+='<p><b>'+h(banner)+'</b></p>'
    rows=[*task_metrics(t,queue_paused=queue_paused),*details]
    publications=publication_card_rows(t)
    if publications:
        head+='<h3>Публикации</h3>'+rich_table(('Тип','Подтверждено','Цель на видео'),publications)
    if rows:
        head+=rich_table(('Показатель','Значение'),rows)
    if publications:
        head+='<footer>Счётчик обработанных действий включает попытки отправки.</footer>'
    if task_has_comments(t):
        head+='<p>'+h(COMMENT_CHECK_NOTE)+'</p>'
    if task_has_replies(t):
        head+='<details><summary>Как считаются ответы</summary><p>'+h(REPLY_CHECK_NOTE)+'</p></details>'
    head+='<footer>Заказ · <code>'+h(public_system_text(t['task_id']))+'</code></footer>'
    return head
