from __future__ import annotations
from html import escape
from datetime import datetime
import json
from typing import Any, Callable
from zoneinfo import ZoneInfo

BUTTON_SYMBOLS = frozenset('▣◇◈▤▥▧⌂⌕⚙◷✓×↻＋←→↶↪↗⇩↑♡▷▶Ⅱ■✎−·○!')

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


def h(value:Any) -> str:
    return escape(str(value if value is not None else ''),quote=True)


def button_text(label:str)->str:
    label=' '.join(label.split())
    if label and label[0] in BUTTON_SYMBOLS:
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
    'account_assignment_changed':'Назначенные аккаунты изменились',
    'admin_action_required':'Требуется действие администратора',
    'connection_unavailable':'Сбой соединения с сервером',
    'job_not_found':'Сервер не вернул задачу',
    'execution_failed':'Задача завершилась с ошибкой',
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


RESULT_STATUS={
    'live':'Подтверждено','confirmed':'Подтверждено','published':'Опубликовано',
    'success':'Успешно','ok':'Успешно','sent':'Отправлено','pending':'Ожидает подтверждения',
    'moderation':'На модерации','failed':'Ошибка','error':'Ошибка','rejected':'Отклонено',
    'skipped':'Пропущено','existing':'Уже выполнено','collected':'Собрано',
}


def result_status(value:Any)->str:
    return RESULT_STATUS.get(value.lower(),'Статус уточняется') if isinstance(value,str) else 'Статус уточняется'


def report_view(task:dict,report:dict)->tuple[str,str]:
    """Rich HTML with native Telegram table; safe fallback is ordinary HTML."""
    title=h(task['display_name']); idtag=h(task['task_id'])
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
    table=''
    if publication_rows:
        summary=[('Коммент.' if label=='Комментарии' else label,ratio,rate,pending)
                 for label,ratio,rate,pending in publication_rows]
        table='<h3>Публикации</h3>'+outcome_table('Тип',summary)
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
    errors=report.get('errors') or []
    error_section=''
    if errors:
        grouped={}
        for item in errors:
            if not isinstance(item,dict):continue
            code=item.get('code')
            reason=REPORT_REASONS.get(code,'Другие ошибки') if isinstance(code,str) else 'Другие ошибки'
            grouped[reason]=grouped.get(reason,0)+count(item.get('count'))
        error_section='<details><summary>Ошибки · '+number(sum(grouped.values()))+'</summary><ul>'+''.join(
            '<li>'+h(reason)+' · '+number(total)+'</li>' for reason,total in grouped.items())+'</ul></details>'
    foot='' if like_only else '<footer>Успехами считаются только подтверждённые публикации. Модерация и неподтверждённые отправки в успехи не входят.</footer>'
    rich=head+table+texts(comm,'Тексты комментариев')+texts(replies,'Тексты ответов')+error_section+foot
    plain=(f'<b>{heading}</b>\n<b>{title}</b> · <code>{idtag}</code>\n{h(status)}\n\n'
       +'\n'.join(f'├ {label}: <code>{value}</code> · {rate}\n└ Модерация / ожидают: <code>{pending}</code>'
                 for label,value,rate,pending in publication_rows))
    plain+=''.join(f'\n├ {h(label)}: <code>{h(value)}</code>' for label,value in extras)
    for label,group in groups:
        items=group.get('texts') or []
        if items:
            plain+=f'\n\n<b>{label} · по текстам</b>'
            for index,item in enumerate(items[:4],1):
                text,ratio,rate,pending=summary_row(str(item.get('text') or '—')[:105],item)
                plain+=f'\n{index}. {h(text)}\n└ <code>{ratio}</code> · {rate} · мод. / ожид. <code>{pending}</code>'
            if len(items)>4:plain+='\nПоказаны первые 4 текста.'
    return rich,plain


def task_metrics(t:dict)->list[tuple[str,str]]:
    snapshot=json.loads(t['snapshot_json'] or '{}')
    if not snapshot:
        return []
    done,total=count(snapshot.get('done')),count(snapshot.get('total'))
    rows=[('Обработано',f'{number(done)} / {number(total)} · {min(100,100*done//total)}%' if total else number(done))]
    fields={
        'publish':(('published','Подтверждено'),('moderation','На модерации'),('pending','Ожидают подтверждения'),
                   ('attempts','Попыток'),('expired','Сессий истекло'),('webPublications','Через веб')),
        'keyword_reply':(('published','Подтверждено'),('pending','Ожидают подтверждения'),('attempts','Попыток')),
        'commentlike':(('liked','Лайков подтверждено'),('likeTotal','Цель'),('likeExisting','Было ранее'),
                       ('likeAttempts','Попыток'),('likeRetries','Повторов'),('likeUnconfirmed','Не подтверждено')),
        'fetch':(('scanned','Просмотрено'),('collected','Собрано')),
        'usernames':(('scanned','Просмотрено'),('collected','Собрано')),
        'check':(('checked','Проверено'),('live','Активных'),('expired','Сессий истекло')),
        'import':(('imported','Импортировано'),('importCreated','Новых'),('importUpdated','Обновлено')),
        'feedwarm':(('scanned','Просмотрено'),('collected','Собрано')),
    }
    kind=t.get('kind')
    rows.extend((label,number(snapshot[key])) for key,label in fields.get(kind,()) if key in snapshot)
    if kind=='publish' and snapshot.get('awemes'):
        videos=[video for video in snapshot['awemes'] if isinstance(video,dict)]
        rows.append(('Цель выполнена',f'{sum(bool(video.get("goal")) for video in videos)} / {len(videos)} видео'))
    if kind=='feedwarm' and isinstance(snapshot.get('warm'),dict):
        warm=snapshot['warm']
        rows.extend((label,number(warm[key])) for key,label in
                    (('searchScanned','Видео проверено'),('searchMatched','Видео найдено'),
                     ('scrolled','Просмотрено в ленте')) if key in warm)
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
    if 'failed' in snapshot:
        rows.append(('Ошибок',number(snapshot['failed'])))
    return rows


def task_view(t:dict)->str:
    body=(f'<b>▣ {h(t["display_name"])}</b> · <code>{h(t["task_id"])}</code>\n'
          f'<b>{state_symbol(t["state"])} {state_label(t["state"])}</b>\n')
    rows=task_metrics(t)
    for index,(label,value) in enumerate(rows):
        branch='└' if index==len(rows)-1 else '├'
        body+=f'\n{branch} {h(label)}: <code>{h(value)}</code>'
    if t.get('error_code'):
        body+='\n\n'+h(ERROR_LABELS.get(t['error_code'],'Состояние требует проверки.'))
    return body


def task_rich(t:dict,details:tuple[tuple[str,Any],...]=())->str:
    head=(f'<h2>▣ {h(t["display_name"])}</h2>'
          f'<p><code>{h(t["task_id"])}</code> · <b>{state_symbol(t["state"])} {state_label(t["state"])}</b></p>')
    rows=[*task_metrics(t),*details]
    if rows:
        head+=rich_table(('Показатель','Значение'),rows)
    if t.get('error_code'):
        head+='<footer>'+h(ERROR_LABELS.get(t['error_code'],'Состояние требует проверки.'))+'</footer>'
    return head
