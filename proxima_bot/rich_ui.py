"""Native Bot API 10.3+ layout primitives; no browser HTML or custom CSS."""
from __future__ import annotations

from .presentation import date_from_unix, h, number, rich_table, rich_time


STAGES=('Материалы','Объём','Проверка')


def stage_html(stage:int)->str:
    if not 1<=stage<=len(STAGES):raise ValueError('Unknown order stage')
    return '<footer>'+(' → '.join('<b>'+label+'</b>' if i==stage else ('✓ '+label if i<stage else label)
                                 for i,label in enumerate(STAGES,1)))+'</footer>'


def screen(title:str,items=(),*,subtitle:str='',stage:int|None=None,lead:tuple[str,str]=('',''),caption:str='')->tuple[str,str]:
    rich='<h2>'+h(title)+'</h2>'
    plain='<b>'+h(title)+'</b>'
    if stage:
        rich+=stage_html(stage)
        plain+='\n'+str(stage)+' из 3 · '+STAGES[stage-1]
    if subtitle:
        rich+='<p>'+h(subtitle)+'</p>'
        plain+='\n'+h(subtitle)
    rich+=lead[0]
    plain+=lead[1]
    for label,value in items:
        rich+='<p>'+h(label)+' · <b>'+h(value)+'</b></p>'
        plain+='\n'+h(label)+' · <b>'+h(value)+'</b>'
    if caption:
        rich+='<footer>'+h(caption)+'</footer>'
        plain+='\n\n'+h(caption)
    return rich,plain


def order_launch(fire_at:int|None,timezone_name:str,*,paused:bool)->tuple[str,str]:
    if fire_at:
        date=date_from_unix(fire_at,timezone_name)
        rich='<p><b>Запуск</b> · не раньше '+rich_time(fire_at,date)+'</p>'
        plain='\n<b>Запуск</b> · не раньше '+h(date)
    else:
        rich='<p><b>Запуск</b> · по очереди кабинета</p>'
        plain='\n<b>Запуск</b> · по очереди кабинета'
    if paused:
        notice='Запуск очереди приостановлен. Заказ будет ждать её возобновления.'
        rich='<blockquote>'+h(notice)+'</blockquote>'+rich
        plain='\n<b>'+h(notice)+'</b>'+plain
    return rich,plain+'\n'


def button_row(*buttons:tuple[str,str,str|None])->str:
    """Documented RichMessageButton callback data is limited to 1–64 bytes."""
    if not 1<=len(buttons)<=8:raise ValueError('A Rich button row requires 1–8 buttons')
    result='<tg-button-row>'
    for label,callback,style in buttons:
        if not 1<=len(callback.encode('utf-8'))<=64:raise ValueError('callback too long')
        if style not in (None,'primary','success','danger','link'):raise ValueError('Unknown Rich button style')
        result+='<tg-button type="callback_data" data="'+h(callback)+'"'+(' style="'+style+'"' if style else '')+'>'+h(label)+'</tg-button>'
    return result+'</tg-button-row>'


def disclosure(label:str,body:str,*,opened:bool=False)->str:
    return '<details'+(' open' if opened else '')+'><summary>'+h(label)+'</summary>'+body+'</details>'


def home_rich(metrics:dict)->str:
    rich,_=screen('🏠 Главное меню',subtitle='Proxima Soft · заказы и результаты')
    available = metrics.get('available_accounts')
    availability = '—' if available is None else number(available)
    rich+='<p>Аккаунты · <b>'+number(metrics.get('total_accounts'))+'</b> · доступно <b>'+availability+'</b></p>'
    rich+='<h3>Сегодня</h3>'
    rich+='<p><b>'+number(metrics.get('comments_today'))+'</b> комментариев · <b>'+number(metrics.get('replies_today'))+'</b> ответов</p>'
    rich+='<footer>На модерации · '+number(metrics.get('moderation_today'))+'</footer>'
    history=rich_table(('Период','Комментарии','Ответы','Модерация'),[
        ('24 часа',number(metrics.get('comments_day')),number(metrics.get('replies_day')),number(metrics.get('moderation_day'))),
        ('7 дней',number(metrics.get('comments_week')),number(metrics.get('replies_week')),number(metrics.get('moderation_week'))),
    ])
    rich+=disclosure('Статистика за 24 часа и 7 дней',history)
    rich+='<p>Всего опубликовано · <b>'+number(metrics.get('total_published'))+'</b></p>'
    return rich
