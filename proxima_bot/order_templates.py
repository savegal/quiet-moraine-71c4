"""Personal order templates stored in the bot's database."""
from __future__ import annotations

import json
from secrets import token_hex

from .api import ApiError, TelegramError
from .bot import safe_api_error
from .catalog import (
    MODES, OWNER_ONLY, limited_lines, order_settings, validate_like_links,
    parse_feedwarm, parse_fetch_count, parse_options, parse_targets, validate_task_options, validate_reply_links,
    video_search_terms, edit_video_search_terms,
)
from .presentation import h, keyboard, pagination, table_html
from .order_form import (order_fields, form_rows, parse_field, set_field, complete_order_form, edit_order_form, parse_volume_message)
from .rich_ui import screen, disclosure
from .media_order_form import MEDIA_DRAFT_SETTINGS_FIELDS, MEDIA_TEXT_FIELDS, MEDIA_TEXT_LIMITS, parse_media_text


MEDIA_TEMPLATE_MODES={'video_post':'Публикация видео','photo_post':'Публикация фотоальбома'}
COMMENT_TEMPLATE_MODES=frozenset(('publish','reply_links','keyword_reply','source_reply'))
COMMENT_TEMPLATE_TYPES={
    'full':('Главные комментарии и ответы',('comments','replies')),
    'main':('Только главные комментарии',('comments',)),
    'reply':('Только ответы',('replies',)),
}
TEMPLATE_LABELS={**{mode:spec[0] for mode,spec in MODES.items() if mode not in OWNER_ONLY},
                 'source_reply':'Ответы на собранное',**MEDIA_TEMPLATE_MODES}
TEMPLATE_PROMPTS={
    'name':'Как назвать шаблон? До 60 символов.',
    'videos':'Пришли ссылки на видео или их ID, по одному на строку. Можно вводить их заново при каждом запуске.',
    'comments':'Отправь тексты комментариев, по одному на строку, или оставь ввод для запуска заказа.',
    'replies':'Отправь тексты ответов, по одному на строку, или оставь ввод для запуска заказа.',
    'reply_links':'Пришли ссылки на комментарии или пары ID видео и комментария, по одной на строку.',
    'like_links':'Пришли ссылки на комментарии для лайков, по одной на строку.',
    'likes':'Сколько лайков на комментарий? Введи число от 1 до 100.',
    'keywords':'Пришли поисковые фразы, по одной на строку.',
    'fetch_count':'Сколько комментариев собирать? Выбери 100 или 1000.',
    'fetch_replies':'Нужно ли собирать ответы под комментариями?',
    'fetch_keywords':'Ключевые слова для сбора, по одной фразе на строку. Можно оставить пустым при создании заказа.',
    'warm_json':'Напиши темы поиска, по одной на строку.',
    'options':'Выбери количества кнопками.',
    'description':f'Отправь подпись для публикации, до {MEDIA_TEXT_LIMITS["description"]} символов. Можно оставить подпись выбранного черновика.',
    'hashtags':f'Отправь хештеги одной строкой, до {MEDIA_TEXT_LIMITS["hashtags"]} символов. Можно оставить хештеги выбранного черновика.',
    'per_account':'Сколько публикаций на аккаунт? Введи число от 1 до 100 или возьми настройки кабинета.',
    'media_options':'Параметры задаются в карточке публикации.',
}
FIELD_LABELS={
    'name':'Название','videos':'Видео','comments':'Комментарии','replies':'Ответы',
    'reply_links':'Ссылки для ответов','like_links':'Ссылки для лайков','likes':'Лайков на комментарий',
    'keywords':'Ключевые фразы','fetch_count':'Лимит сбора','fetch_replies':'Сбор ответов','fetch_keywords':'Ключевые слова сбора',
    'warm_json':'Темы поиска','options':'Объём и действия','description':'Подпись',
    'hashtags':'Хештеги','per_account':'Публикаций на аккаунт','media_options':'Настройки публикации',
}
TRANSIENT_FIELDS={'wizard_token','fire_at','review_account_ids','review_account_folder','review_edit','template_steps','form_field','form_template','form_more','_return_draft','auto_name','approved_template'}


def template_fields(mode:str)->tuple[str,...]:
    if mode in MEDIA_TEMPLATE_MODES:
        return ('name','description','hashtags','per_account')
    if mode=='source_reply':
        return ('name','replies','options')
    return tuple(field for field in ('name',*MODES[mode][1:]) if field not in ('videos','reply_links','like_links'))


def task_order_mode(kind:str,payload:dict)->str:
    return 'reply_links' if kind=='publish' and payload.get('replyLinks') else kind


def template_mode(item:dict)->str:
    payload=json.loads(item['payload'])
    return task_order_mode(item['mode'],payload.get('job',{}))


def template_values(item:dict)->dict:
    payload=json.loads(item['payload'])
    mode=template_mode(item)
    if 'draft' in payload:
        values={key:value for key,value in payload['draft'].items() if key not in TRANSIENT_FIELDS}
    elif 'media' in payload:
        values=dict(payload['media'])
    elif 'job' in payload:
        job=payload['job']
        assets=job.get('assets') or {}
        options=dict(job.get('settings') or {})
        values={'options':options}
        for source,target in (('awemeIds','videos'),('replyLinks','reply_links'),('likeLinks','like_links'),
                              ('likesPerComment','likes'),('feedWarm','warm_json')):
            if source in job:values[target]=job[source]
        for source,target in (('commentTexts','comments'),('replyTexts','replies')):
            if source in assets:values[target]=assets[source]
        if mode=='keyword_reply':
            values['keywords']=options.pop('fetchKeywords',[])
        if mode=='fetch' and 'fetchKeywords' in options:
            values['fetch_keywords']=options.pop('fetchKeywords')
        if job.get('usePhoto'):
            comments=not job.get('disableCommentPhotos',False)
            replies=not job.get('disableReplyPhotos',False)
            values['photo_mode']='both' if comments and replies else 'comments' if comments else 'replies'
    else:
        values=dict(payload) if mode in COMMENT_TEMPLATE_MODES else {}
    if mode=='fetch':
        options=dict(values.get('options') or {})
        if 'fetch_count' not in values:
            if options.get('collectAll'):
                values['fetch_count']=100
            elif options.get('fetchCount'):
                values['fetch_count']=options['fetchCount'] if options['fetchCount'] in (100,1000) else 100
        elif values['fetch_count'] not in (100,1000):values['fetch_count']=100
        if 'fetch_replies' not in values and type(options.get('includeReplies')) is bool:
            values['fetch_replies']=options['includeReplies']
        if 'fetch_count' in values:
            options.pop('fetchCount',None)
            options.pop('collectAll',None)
        if 'fetch_replies' in values:options.pop('includeReplies',None)
        values['options']=options
    for field in ('videos','reply_links','like_links'):values.pop(field,None)
    for field in ('comments','replies'):
        if field in values.get('disabled_fields',[]):values.pop(field,None)
    values['name']=item['name'][:60]
    return values


def template_texts(values:dict)->dict[str,list[str]]:
    return {key:list(values.get(key) or []) for key in ('comments','replies')}


def template_matches(item:dict|None,mode:str)->bool:
    if not item or not item.get('approved_revision'):return False
    stored=template_mode(item)
    if mode not in COMMENT_TEMPLATE_MODES:return stored==mode
    values=template_values(item)
    if stored not in COMMENT_TEMPLATE_MODES or set(values.get('ask_fields') or ()) & {'comments','replies'}:
        return False
    if any(key in values and not isinstance(values[key],list) for key in ('comments','replies')):return False
    texts=template_texts(values)
    if any(len(lines)>100 or any(not isinstance(line,str) or not line.strip() or len(line)>700 for line in lines)
           for lines in texts.values()):return False
    if mode=='publish':return stored=='publish' and bool(texts['comments'] or texts['replies'])
    return bool(texts['replies'])


def template_order_values(item:dict,mode:str)->dict:
    values=template_values(item)
    if mode in COMMENT_TEMPLATE_MODES:
        if mode!='publish':values['comments']=[]
        values['approved_template']=[item['template_id'],item['approved_revision']]
        values['disabled_fields']=[field for field,lines in template_texts(values).items() if not lines]
        if values.get('comments') and (values.get('options') or {}).get('commentsPerVideo')==0:
            values['options'].pop('commentsPerVideo')
        if values.get('replies') and not values.get('comments') and (values.get('options') or {}).get('repliesPerComment')==0:
            values['options'].pop('repliesPerComment')
    return values


def template_input_steps(mode:str,data:dict)->list[str]:
    if mode=='publish':
        required=['videos']
        if not data.get('comments') and not data.get('replies'):
            required.extend(('comments','replies'))
    else:
        required={'reply_links':['reply_links','replies'],'fetch':['videos'],
                  'commentlike':['like_links','likes'],'keyword_reply':['videos','keywords','replies'],
                  'feedwarm':['warm_json'],'usernames':['videos']}[mode]
    required=[field for field in required if not (video_search_terms(data.get(field)) if field=='warm_json' else data.get(field))]
    missing=(set(required)|set(data.get('ask_fields') or []))-set(data.get('disabled_fields') or [])
    missing.add('options')
    if mode=='fetch':
        for field in ('fetch_count','fetch_replies'):
            if field not in data:missing.add(field)
    return [field for field in ('name',*MODES[mode][1:]) if field in missing]


def template_rows(mode:str,values:dict)->list[tuple[str,str|int]]:
    label=TEMPLATE_LABELS.get(mode,'Заказ')
    if mode=='publish':
        label=next((title for title,fields in COMMENT_TEMPLATE_TYPES.values()
                    if set(fields)=={field for field,lines in template_texts(values).items() if lines}),label)
    rows=[('Тип',label)]
    asked=set(values.get('ask_fields') or [])
    disabled=set(values.get('disabled_fields') or [])
    for field in template_fields(mode):
        if field=='name':continue
        value=values.get(field)
        if field=='options':
            quantities=form_rows(mode,{**values,'form_template':True})
            text=' · '.join(label+': '+value for label,value in quantities) if field not in asked and quantities and (value or values.get('form_ready')) else 'Выбор при запуске'
        elif field in disabled or (mode in COMMENT_TEMPLATE_MODES and field in ('comments','replies') and not value):text='Нет текстов'
        elif field in asked or (value is None and field in ('videos','comments','replies','reply_links','like_links','keywords','warm_json','fetch_count','fetch_replies')):text='Спросить при создании'
        elif value is None:text='Из настроек кабинета'
        elif field=='fetch_replies':text='Собирать' if value else 'Не собирать'
        elif field=='fetch_count' and value=='all':text='Все доступные'
        elif isinstance(value,list):
            count=len(value)
            word='вариант' if count%10==1 and count%100!=11 else 'варианта' if count%10 in (2,3,4) and count%100 not in (12,13,14) else 'вариантов'
            text=f'{count} {word} текста' if field in ('comments','replies') else f'{count} строк · сохранены'
        elif isinstance(value,dict):
            topics=value.get('topics') or value.get('keywords')
            text=' · '.join(topics[:3]) if topics else f'{len(value)} параметров'
        else:text=str(value)[:250]
        rows.append((FIELD_LABELS[field],text))
    return rows


def template_screen(title:str,mode:str,values:dict,*,subtitle:str='')->tuple[str,str]:
    rows=template_rows(mode,values)
    quantities=form_rows(mode,{**values,'form_template':True})
    configured=bool(quantities and (values.get('options') or values.get('form_ready')) and 'options' not in values.get('ask_fields',[]))
    if configured:rows=[row for row in rows if row[0]!=FIELD_LABELS['options']]
    rich,plain=screen(title,rows,subtitle=subtitle)
    if configured:
        rich+=disclosure('Сохранённый объём',''.join('<p>'+h(label)+' · <b>'+h(value)+'</b></p>' for label,value in quantities))
        plain+='\n\n<b>Сохранённый объём</b>\n'+'\n'.join(h(label)+' · '+h(value) for label,value in quantities)
    return rich,plain


class LocalTemplatesMixin:
    def reply_templates_available(self,uid:int)->bool:
        return any(not self.mode_restricted(uid,mode) for mode in ('reply_links','keyword_reply'))

    async def notify_template_review(self,uid:int,tid:str)->None:
        review=self.db.template_review(uid,tid)
        if not review or self.owner(uid):return
        try:
            await self.say(self.cfg.owner_id,'<b>Шаблон на одобрение</b>\n'+h(review['name'])+
                           '\nАвтор · <code>'+str(uid)+'</code>',keyboard(
                               [('▤ Проверить','tr:view:'+review['revision'],'primary')]))
        except TelegramError:
            pass

    async def template_reviews(self,uid:int,page:int=0)->None:
        if not self.owner(uid):return
        reviews=self.db.pending_template_reviews(11,page*10)
        rows=[[(review['name'][:38]+' · '+str(review['user_id']),'tr:view:'+review['revision'],None)]
              for review in reviews[:10]]
        rich,plain=screen('▤ Одобрение шаблонов',subtitle='Выбери заявку для проверки.' if reviews else 'Заявок на этой странице нет.')
        await self.tg.rich(uid,rich,plain,keyboard(*rows,pagination('tr:list:',page,len(reviews)>10),[('↶ Панель','admin',None)]))

    async def template_review_action(self,uid:int,data:str)->None:
        if not self.owner(uid):return
        _,verb,value=data.split(':') if data.count(':')==2 else ('','','')
        if verb=='list' and value.isascii() and value.isdecimal() and len(value)<=5:
            return await self.template_reviews(uid,int(value))
        if verb not in ('view','text','approve','reject') or len(value)!=12:return
        review=self.db.pending_template_review(value)
        if not review:return await self.say(uid,'Заявка уже обработана или заменена новой.',keyboard([('↶ Заявки','tr:list:0',None)]))
        author,tid=review['user_id'],review['template_id']
        item=self.db.template(author,tid,owned_only=True)
        if not item:return
        previous=item if item['approved_revision'] else None
        current=template_values(previous) if previous else {}
        proposed=template_values(review)
        mode=template_mode(review)
        if verb=='text':
            return await self.tg.attachment(uid,
                ('БЫЛО\n'+self.template_text_document(current)+'\nСТАНЕТ\n'+self.template_text_document(proposed)).encode(),
                'template-review-'+tid+'.txt','Полные тексты до и после')
        if verb in ('approve','reject'):
            if verb=='approve' and mode in COMMENT_TEMPLATE_MODES and not template_matches({**review,'approved_revision':value},mode):
                return await self.say(uid,'Для одобрения нужны готовые тексты. Автор может исправить шаблон и отправить его снова.',
                                      keyboard([('↶ Заявки','tr:list:0',None)]))
            result=self.db.decide_template_review(uid,value,verb=='approve')
            if result and author!=uid:
                status='одобрен' if verb=='approve' else 'отклонён'
                try:
                    await self.say(author,'Шаблон «'+h(review['name'])+'» '+status+'.',keyboard([('▧ Открыть шаблон','tpl:'+tid,None)]))
                except TelegramError:
                    pass
            await self.say(uid,'Шаблон одобрен.' if verb=='approve' else 'Правки отклонены.',keyboard([('↶ Заявки','tr:list:0',None)]))
            return
        rich,plain=screen('▤ Одобрение шаблона',[('Автор',str(author)),('Было',previous['name'] if previous else 'Новый шаблон'),('Станет',review['name'])],
                          subtitle=TEMPLATE_LABELS.get(mode,'Шаблон'))
        for label,values in (('Было',current),('Станет',proposed)):
            summary_rows=template_rows(mode,values) if values else [('Версия','Ранее не одобрялся')]
            block,summary=table_html(label,summary_rows)
            rich+=block
            plain+='\n\n'+summary
            if mode in COMMENT_TEMPLATE_MODES:
                preview=self.template_text_document(values,preview=True)
                rich+='<p>'+h(preview).replace('\n','<br>')+'</p>'
                plain+='\n'+h(preview)
        await self.tg.rich(uid,rich,plain,keyboard(
            [('✓ Одобрить','tr:approve:'+value,'success'),('× Отклонить','tr:reject:'+value,'danger')],
            [('⇩ Все тексты до и после','tr:text:'+value,None)] if mode in COMMENT_TEMPLATE_MODES else [],
            [('↶ Заявки','tr:list:0',None)]))

    @staticmethod
    def template_text_document(values:dict,*,preview:bool=False)->str:
        sections=[]
        for field,lines in template_texts(values).items():
            lines=[str(line) for line in lines]
            shown=[line[:150]+('…' if len(line)>150 else '') for line in lines[:2]] if preview else lines
            if preview and len(lines)>2:shown.append(f'Ещё {len(lines)-2} в файле')
            sections.append(FIELD_LABELS[field]+'\n'+('\n'.join(shown) or '—'))
        return '\n\n'.join(sections)+'\n'

    async def template_menu(self,uid:int)->None:
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        rows=[[(label,'tpl_new:'+key,None)] for key,(label,_) in COMMENT_TEMPLATE_TYPES.items()
              if not self.mode_restricted(uid,'publish') or (key=='reply' and self.reply_templates_available(uid))]
        rows.extend([(label,'tpl_new:'+mode,None)] for mode,label in TEMPLATE_LABELS.items()
                    if mode not in COMMENT_TEMPLATE_MODES and not self.mode_restricted(uid,mode))
        rich,plain=screen('＋ Создать шаблон',subtitle='Для какого заказа подготовить тексты и параметры?')
        await self.tg.rich(uid,rich,plain,keyboard(*rows,[('↶ Мои шаблоны','templates',None)]))

    async def template_card(self,uid:int,tid:str)->None:
        item=self.db.template(uid,tid)
        if not item:return await self.say(uid,'Шаблон недоступен.')
        pending=self.db.draft(uid)
        if pending and pending['mode']=='template_rename' and pending['data'].get('id')==tid:
            self.db.clear_draft(uid)
        if item['mode'] not in TEMPLATE_LABELS:
            return await self.say(uid,'Этот шаблон использует старый вид задачи.',keyboard([('↶ Шаблоны','templates',None)]))
        mode=template_mode(item)
        owned=item.get('owned',True)
        review=self.db.template_review(uid,tid) if owned else None
        shown=review if review and not item['approved_revision'] else item
        values=template_values(shown)
        rich,plain=template_screen('▧ '+shown['name'],template_mode(shown),values)
        if review:
            status='На одобрении' if review['state']=='pending' else 'Правки отклонены'
            if item['approved_revision']:status+=' · для заказов действует одобренная версия'
            rich+='<footer>'+h(status)+'</footer>'
            plain+='\n\n'+status
        elif item['approved_revision']:
            rich+='<footer>Одобрен</footer>'
            plain+='\n\nОдобрен'
        if not owned:
            rich+='<footer>Командный шаблон (доступен для запуска).</footer>'
            plain+='\n\nКомандный шаблон (доступен для запуска).'
        elif self.owner(uid):
            badge='Открыт всей команде' if item.get('is_shared') else 'Только для тебя'
            rich+='<footer>Доступ: '+h(badge)+'</footer>'
            plain+='\n\nДоступ: '+h(badge)
        rich+='<footer>Перед запуском проверишь объём и тексты заказа.</footer>'
        plain+='\n\nПеред запуском проверишь объём и тексты заказа.'
        for field in ('comments','replies','keywords'):
            if values.get(field):
                preview='\n'.join(str(value)[:160] for value in values[field][:3])
                rich+='<details><summary>'+h(FIELD_LABELS[field])+' · '+str(len(values[field]))+'</summary><p>'+h(preview)+'</p><footer>Показано '+str(min(3,len(values[field])))+' из '+str(len(values[field]))+'</footer></details>'
                plain+='\n\n<b>'+FIELD_LABELS[field]+'</b>\n'+h(preview)
        rows=[]
        if not self.mode_restricted(uid,mode) and template_matches(item,mode):
            rows.append([('＋ Создать по шаблону','tpl_use:'+tid,'success')])
        if template_matches(item,'source_reply') and not self.mode_restricted(uid,'source_reply') and mode!='source_reply':
            rows.append([('↪ Ответить на собранное','source_tpl_page:'+tid+':0',None)])
        if values.get('comments') or values.get('replies'):
            rows.append([('⇩ Все тексты','tpl_text:'+tid,None)])
        if review and item['approved_revision'] and mode in COMMENT_TEMPLATE_MODES:
            rows.append([('⇩ Тексты предложенных правок','tpl_text_pending:'+tid,None)])
        if owned:
            rows.append([('✎ Изменить','tpl_edit:'+tid,None),('Ещё','tpl_more:'+tid,None)])
            if self.owner(uid) and item['approved_revision']:
                share_label='◇ Сделать личным' if item.get('is_shared') else '◈ Поделиться с командой'
                rows.append([(share_label,'tpl_share:'+tid,None)])
        rows.append([('↶ Шаблоны','templates',None)])
        await self.tg.rich(uid,rich,plain,keyboard(*rows))

    async def edit_local_template(self,uid:int,mode:str,tid:str|None=None,*,compact:bool=False)->None:
        if not self.db.is_active(uid,self.cfg.owner_id):return await self.access(uid)
        template_type=mode if mode in COMMENT_TEMPLATE_TYPES else ''
        if template_type:mode='publish'
        if mode not in TEMPLATE_LABELS:return
        values={}
        if tid is not None:
            item=self.db.template(uid,tid,owned_only=True)
            if not item or item['mode']!=mode:return await self.say(uid,'Шаблон недоступен.')
            item=self.db.template_review(uid,tid) or item
            values=template_values(item)
            mode=template_mode(item)
        reply_editor=(mode in COMMENT_TEMPLATE_MODES and (tid is not None or template_type=='reply')
                      and self.reply_templates_available(uid))
        if self.mode_restricted(uid,mode) and not reply_editor:
            return await self.say(uid,'Этот вид заказа доступен только владельцу.')
        if template_type:
            values['disabled_fields']=[field for field in ('comments','replies') if field not in COMMENT_TEMPLATE_TYPES[template_type][1]]
        self.db.save_draft(uid,'local_template','review' if tid else 'name',
                           {'mode':mode,'template_id':tid,'values':values,'token':token_hex(6),'compact_editor':compact,
                            'template_type':template_type})
        await self.local_template_field(uid)

    async def local_template_field(self,uid:int,*,error:str='')->None:
        draft=self.db.draft(uid)
        if not draft or draft['mode']!='local_template':return
        data=draft['data']
        mode,values,token=data['mode'],data['values'],data['token']
        rows=[]
        if draft['step']=='review':
            rich,plain=template_screen('▧ Редактор шаблона',mode,values,subtitle=values['name'])
            caption='После отправки на одобрение владелец увидит предложенные изменения.'
            rich+='<footer>'+caption+'</footer>'
            plain+='\n\n'+caption
            if error:
                rich+='<p><b>Проверь ввод</b> '+h(error)+'</p>'
                plain+='\n\nПроверь ввод: '+h(error)
            for field in template_fields(mode):
                if data.get('template_type') and field in ('comments','replies') and field not in COMMENT_TEMPLATE_TYPES[data['template_type']][1]:
                    continue
                label=FIELD_LABELS[field]
                if field in ('comments','replies') and mode in COMMENT_TEMPLATE_MODES and not values.get(field):
                    label='Добавить '+('главные комментарии' if field=='comments' else 'ответы')
                    rows.append([(label,f'lt:edit_{field}:{token}',None)])
                else:
                    rows.append([('✎ '+label,f'lt:edit_{field}:{token}',None)])
            await self.tg.rich(uid,rich,plain,keyboard(
                [('↑ Отправить на одобрение','lt:save:'+token,'success')],*rows,
                [('× Отменить','lt:cancel:'+token,None)]))
            return
        field=draft['step']
        if field not in template_fields(mode):
            self.db.save_draft(uid,'local_template','review',data)
            return await self.local_template_field(uid)
        if field=='options':
            return await self.order_form_field(uid,mode,{**values,'form_template':True},token,error=error,prefix='lt')
        if mode=='publish' and field in ('comments','replies'):
            rows.append([('Не использовать',f'lt:off:{token}',None)])
        if field=='fetch_replies':
            selected=values.get(field)
            rows.append([('С ответами','lt:replies_yes:'+token,'primary' if selected is True else None),
                         ('Без ответов','lt:replies_no:'+token,'primary' if selected is False else None)])
        if field=='fetch_count':
            rows.append([(str(count),f'lt:fetch{count}:{token}','primary' if values.get(field)==count else None)
                         for count in (100,1000)])
        if field!='name':
            if field not in ('fetch_count','fetch_replies') and field in values and (field!='warm_json' or video_search_terms(values[field])):
                rows.append([('Оставить текущее','lt:keep:'+token,'primary')])
            if mode not in COMMENT_TEMPLATE_MODES or field not in ('comments','replies'):
                rows.append([('Ввод при запуске' if field in ('videos','reply_links','like_links','keywords','warm_json')
                              else 'Спросить при создании','lt:skip:'+token,None)])
        else:
            rows.append([('Название автоматически','lt:auto:'+token,None)])
        rows.append([('↶ Назад','lt:back:'+token,None),('× Отменить','lt:cancel:'+token,'danger')])
        fields=template_fields(mode)
        prompt=TEMPLATE_PROMPTS[field]
        if mode in COMMENT_TEMPLATE_MODES and field in ('comments','replies'):
            prompt='Отправь текст сообщением или TXT, один вариант на строку. Эти тексты будут использоваться в заказах после одобрения.'
        if field in values:
            value=values[field]
            if field=='fetch_replies':preview='С ответами' if value else 'Без ответов'
            elif field=='warm_json':preview='\n'.join(video_search_terms(value)[:3])
            elif isinstance(value,list):preview='\n'.join(value[:3])
            else:preview=str(value)
            prompt+='\n\nСейчас сохранено\n<code>'+h(preview[:700])+'</code>'
        if error:prompt+='\n\n<b>Проверь ввод</b>\n'+h(error)
        rich,plain=screen('▧ '+TEMPLATE_LABELS[mode],subtitle=FIELD_LABELS[field])
        rich+='<p>'+prompt.replace('\n\n','</p><p>').replace('\n','<br>')+'</p>'
        plain+='\n\n'+prompt
        await self.tg.rich(uid,rich,plain,keyboard(*rows))

    async def next_template_field(self,uid:int,data:dict,field:str)->None:
        fields=template_fields(data['mode'])
        index=fields.index(field)+1
        step='review' if data.pop('editing_field',False) or data.get('compact_editor') or index==len(fields) else fields[index]
        data['token']=token_hex(6)
        self.db.save_draft(uid,'local_template',step,data)
        await self.local_template_field(uid)

    async def local_template_input(self,uid:int,text:str)->bool:
        draft=self.db.draft(uid)
        if not draft or draft['mode'] not in ('local_template','template_rename'):return False
        if not self.db.is_active(uid,self.cfg.owner_id):
            await self.access(uid)
            return True
        if draft['mode']=='template_rename':
            item=self.db.template(uid,draft['data']['id'],owned_only=True)
            if item:
                try:
                    item=self.db.template_review(uid,item['template_id']) or item
                    name=text.strip()
                    if not 1<=len(name)<=60:raise ValueError('Название должно содержать от 1 до 60 символов.')
                    payload=json.loads(item['payload'])
                    if 'draft' in payload:payload['draft']['name']=name
                    self.db.save_template(uid,name,item['mode'],payload,template_id=item['template_id'])
                except ValueError as exc:
                    await self.say(uid,h(exc))
                    return True
                self.db.clear_draft(uid)
                await self.notify_template_review(uid,item['template_id'])
                await self.template_card(uid,item['template_id'])
            else:
                self.db.clear_draft(uid)
                await self.say(uid,'Шаблон больше не существует.')
            return True
        data,field=draft['data'],draft['step']
        if field=='review':
            await self.local_template_field(uid)
            return True
        values,mode=data['values'],data['mode']
        try:
            if field=='name':
                if not 1<=len(text.strip())<=60:raise ValueError('Название должно содержать от 1 до 60 символов.')
                values[field]=text.strip()
            elif field=='videos':values[field]=parse_targets(text)
            elif field=='reply_links':values[field]=validate_reply_links(text.splitlines())
            elif field=='like_links':values[field]=validate_like_links(limited_lines(text,300,700))
            elif field in ('comments','replies'):
                lines=limited_lines(text,100,700)
                if not values.get(field) or field in values.get('disabled_fields',[]):
                    volume,likes={
                        'comments':('commentsPerVideo','likesPerComment'),
                        'replies':('repliesPerComment','likesPerReply'),
                    }[field]
                    options=values.get('options') or {}
                    if options.get(volume)==0:
                        options.pop(volume)
                        options.pop(likes,None)
                values[field]=lines
            elif field=='fetch_keywords':values[field]=limited_lines(text,100,100)
            elif field=='keywords':values[field]=limited_lines(text,100,100)
            elif field=='fetch_count':
                values[field]=parse_fetch_count(text)
                if values[field] not in (100,1000):raise ValueError('Выбери 100 или 1000.')
            elif field in ('likes','per_account'):
                amount=int(text.strip())
                if not 1<=amount<=100:raise ValueError('Укажи целое число от 1 до 100.')
                values[field]=amount
            elif field=='fetch_replies':raise ValueError('Выбери вариант кнопкой.')
            elif field=='warm_json':
                values[field] = edit_video_search_terms(text, values.get(field))
            elif field=='options':
                selected=next((item for item in order_fields(mode,{**values,'form_template':True})
                               if item.key==values.get('form_field')),None)
                if selected:
                    values.update(set_field(values,selected,parse_field(selected,text)))
                    values.pop('form_field',None)
                    data['token']=token_hex(6)
                    self.db.save_draft(uid,'local_template',field,data)
                    await self.local_template_field(uid)
                    return True
                if not text.lstrip().startswith(('{','-')):
                    values.update(parse_volume_message(mode,{**values,'form_template':True},text))
                    data['token']=token_hex(6)
                    self.db.save_draft(uid,'local_template',field,data)
                    await self.local_template_field(uid)
                    return True
                raise ValueError('JSON-ввод отключён. Выбери количество кнопкой.')
            elif field in MEDIA_TEXT_FIELDS:
                values[field]=parse_media_text(field,text)
            elif field=='media_options':raise ValueError('Используй параметры в карточке публикации.')
            if field in values.get('disabled_fields',[]):values['disabled_fields'].remove(field)
            if field in values.get('ask_fields',[]):
                values['ask_fields'].remove(field)
                if not values['ask_fields']:values.pop('ask_fields')
            await self.next_template_field(uid,data,field)
        except (ValueError,TypeError) as exc:
            await self.local_template_field(uid,error=str(exc))
        return True

    async def media_template_drafts(self,uid:int,tid:str,page:int=0)->None:
        if not self.owner(uid):return
        item=self.db.template(uid,tid)
        if not item or not item['approved_revision'] or item['mode'] not in MEDIA_TEMPLATE_MODES or self.mode_restricted(uid,item['mode']):
            return await self.say(uid,'Шаблон недоступен.')
        kind='video' if item['mode']=='video_post' else 'photo'
        result=await self.api.call('GET','/v1/posts/drafts',params={'kind':kind})
        items=[draft for draft in result.get('items',[]) if draft.get('kind')==kind and isinstance(draft.get('id'),str)]
        size=10
        page=max(0,min(page,(len(items)-1)//size))
        rows=[[(str(draft.get('name') or draft['id'])[:45],f'mtpl:pick:{tid}:{draft["id"]}',None)]
              for draft in items[page*size:(page+1)*size]]
        nav=[]
        if page:nav.append(('← Ранее',f'mtpl:page:{tid}:{page-1}',None))
        if (page+1)*size<len(items):nav.append(('→ Далее',f'mtpl:page:{tid}:{page+1}',None))
        rows.append(nav)
        if not items:
            rows.append([('↑ Загрузить видео','upload:video','success')] if kind=='video' else
                        [('＋ Создать фотоальбом','owner_edit:album','success')])
        rows.append([('↶ Шаблон','tpl:'+tid,None)])
        hint='Выбери черновик для применения шаблона.' if items else 'Сначала создай черновик, затем вернись к этому шаблону.'
        rich,plain=screen('▧ '+item['name'],subtitle=hint)
        await self.tg.rich(uid,rich,plain,self.owner_keyboard(*rows))

    async def media_template_preview(self,uid:int,tid:str,oid:str)->None:
        if not self.owner(uid):return
        item=self.db.template(uid,tid)
        if not item or not item['approved_revision'] or item['mode'] not in MEDIA_TEMPLATE_MODES or self.mode_restricted(uid,item['mode']):
            return await self.say(uid,'Шаблон недоступен.')
        expected='video' if item['mode']=='video_post' else 'photo'
        order=await self.load_media_order(self.template_media_id(oid),kind=expected)
        values=template_values(item)
        overrides=dict(values.get('media_options') or {})
        if 'per_account' in values:overrides['perAccount']=values['per_account']
        text_overrides={key:values[key] for key in MEDIA_TEXT_FIELDS if key in values}
        settings=order['settings']
        settings.update(overrides)
        settings.update(text_overrides)
        self._allowed_fields(settings,'PostSettings')
        patch=order['patch']
        if text_overrides:patch.update({**text_overrides,'ownText':True})
        draft_settings={key:overrides[key] for key in MEDIA_DRAFT_SETTINGS_FIELDS if key in overrides}
        if draft_settings:patch.update({**draft_settings,'ownSettings':True})
        token=token_hex(6)
        self.db.save_draft(uid,'local_media_template','review',{
            'token':token,'template_id':tid,'revision':item['approved_revision'],'draft_id':oid,'media_order':order,
        })
        rich,plain=table_html('▧ Применить шаблон',[
            ('Шаблон',item['name']),('Черновик',order['name']),
            ('Тип',MEDIA_TEMPLATE_MODES[item['mode']]),('Публикаций на аккаунт',settings['perAccount']),
            ('Аккаунты','Все подходящие' if settings.get('allAccounts') else len(settings.get('accountIds') or [])),
            ('Подпись',settings['description'] or 'Без подписи'),
            ('Хештеги',settings['hashtags'] or 'Без хештегов'),
            ('Следующий шаг','Подготовка плана и проверка аккаунтов'),
        ])
        await self.tg.rich(uid,rich,plain,self.owner_keyboard(
            [('✓ Подтвердить объём и подготовить план','mtpl:apply:'+token,'success')],
            [('✎ Изменить объём и параметры','mtpl:edit:'+token,None)],
            [('× Отменить','mtpl:cancel:'+token,None)]))

    @staticmethod
    def template_media_id(value:str)->str:
        from .operations import VALID_ID
        if not VALID_ID.fullmatch(value):raise ValueError('Некорректный ID черновика.')
        return value

    async def media_template_callback(self,uid:int,action:str)->None:
        if not self.owner(uid):return
        parts=action.split(':')
        if len(parts)==4 and parts[1]=='page' and parts[3].isdigit():
            return await self.media_template_drafts(uid,parts[2],int(parts[3]))
        if len(parts)==4 and parts[1]=='pick':
            return await self.media_template_preview(uid,parts[2],parts[3])
        if len(parts)!=3 or parts[1] not in ('apply','cancel','edit'):return
        draft=self.db.draft(uid)
        if (not draft or draft['mode']!='local_media_template' or draft['data'].get('token')!=parts[2]):
            return await self.say(uid,'Подтверждение устарело. Открой шаблон заново.')
        data=draft['data']
        if parts[1]=='cancel':
            self.db.clear_draft(uid)
            return await self.template_card(uid,data['template_id'])
        item=self.db.template(uid,data['template_id'])
        if not item or not item['approved_revision'] or item['approved_revision']!=data.get('revision') or self.mode_restricted(uid,item['mode']):
            return await self.say(uid,'Шаблон изменился. Проверь его и выбери черновик снова.')
        order=data.get('media_order')
        if not order:
            return await self.media_template_preview(uid,data['template_id'],data['draft_id'])
        self.db.save_draft(uid,'media_order','review',order)
        if parts[1]=='edit':
            return await self.media_order_screen(uid)
        await self.media_order_callback(uid,'mo:prepare:'+order['token'])

    async def local_template_callback(self,uid:int,action:str)->bool:
        if action.startswith('tr:'):
            await self.template_review_action(uid,action)
            return True
        if action.startswith(('tpl_text:','tpl_text_pending:')):
            if not self.db.is_active(uid,self.cfg.owner_id):return True
            route,tid=action.split(':',1)
            item=self.db.template(uid,tid)
            if not item:return True
            if route=='tpl_text_pending' or not item['approved_revision']:
                item=self.db.template_review(uid,tid)
            if item:
                await self.tg.attachment(uid,self.template_text_document(template_values(item)).encode(),
                                         'template-'+tid+'.txt','Тексты шаблона')
            return True
        if action.startswith('mtpl:'):
            try:
                await self.media_template_callback(uid,action)
            except (ApiError,ValueError) as exc:
                message=safe_api_error(exc.code) if isinstance(exc,ApiError) else h(exc)
                await self.say(uid,message,keyboard([('▧ Мои шаблоны','templates',None)]))
            return True
        if action.startswith('tpl_more:'):
            tid=action[9:]
            item=self.db.template(uid,tid,owned_only=True)
            if item:
                await self.say(uid,'<b>▧ '+h(item['name'])+'</b>',keyboard(
                    [('✎ Переименовать','tpl_rename:'+tid,None)],
                    [('× Удалить шаблон','tpl_delete_confirm:'+tid,'danger')],
                    [('↶ Шаблон','tpl:'+tid,None)]))
            return True
        if action=='tpl_new':
            await self.template_menu(uid)
            return True
        if action.startswith(('tpl_new:','tpl_edit:','tpl_rename:')):
            if not self.db.is_active(uid,self.cfg.owner_id):
                await self.access(uid)
                return True
            route,_,value=action.partition(':')
            if route=='tpl_new':await self.edit_local_template(uid,value,compact=True)
            else:
                item=self.db.template(uid,value,owned_only=True)
                if not item:
                    await self.say(uid,'Шаблон недоступен.')
                elif route=='tpl_edit':
                    await self.edit_local_template(uid,item['mode'],value)
                else:
                    self.db.save_draft(uid,'template_rename','name',{'id':value})
                    await self.say(uid,'Введи новое название шаблона. До 60 символов.',
                                   keyboard([('↶ Шаблон','tpl:'+value,None)]))
            return True
        if not action.startswith('lt:'):return False
        if not self.db.is_active(uid,self.cfg.owner_id):
            await self.access(uid)
            return True
        draft=self.db.draft(uid)
        _,verb,token=action.split(':',2) if action.count(':')==2 else ('','','')
        if not draft or draft['mode']!='local_template' or token!=draft['data'].get('token'):
            await self.say(uid,'Кнопка устарела. Открой шаблон заново.',keyboard([('▧ Мои шаблоны','templates',None)]))
            return True
        data,step=draft['data'],draft['step']
        values=data['values']
        fields=template_fields(data['mode'])
        if step=='options':
            try:updated=edit_order_form(data['mode'],{**values,'form_template':True},verb)
            except ValueError:updated=None
            if updated is not None:
                data.update(values=updated,token=token_hex(6))
                self.db.save_draft(uid,'local_template',step,data)
                await self.local_template_field(uid)
                return True
            if verb=='keep':
                values.update(complete_order_form(data['mode'],{**values,'form_template':True}))
                values.pop('form_field',None)
                if 'options' in values.get('ask_fields',[]):values['ask_fields'].remove('options')
                await self.next_template_field(uid,data,step)
                return True
        if verb=='cancel':
            self.db.clear_draft(uid)
            await self.templates(uid)
        elif verb=='back':
            if data.pop('editing_field',False):
                target='review'
            elif step=='name':
                self.db.clear_draft(uid)
                await self.template_menu(uid)
                return True
            else:
                target=fields[-1] if step=='review' else fields[fields.index(step)-1]
            data['token']=token_hex(6)
            self.db.save_draft(uid,'local_template',target,data)
            await self.local_template_field(uid)
        elif step=='review' and verb.startswith('edit_') and verb[5:] in fields:
            data.update(editing_field=True,token=token_hex(6))
            self.db.save_draft(uid,'local_template',verb[5:],data)
            await self.local_template_field(uid)
        elif step=='review' and verb=='save':
            mode=data['mode']
            clean={key:value for key,value in values.items() if key not in TRANSIENT_FIELDS}
            if mode in COMMENT_TEMPLATE_MODES:
                fields={key for key,lines in template_texts(clean).items() if lines and key not in clean.get('disabled_fields',[])}
                requested=data.get('template_type')
                needed=set(COMMENT_TEMPLATE_TYPES[requested][1]) if requested else set()
                if not fields or (mode!='publish' and 'replies' not in fields) or (needed and fields!=needed):
                    await self.local_template_field(uid,error='Добавь хотя бы один текст для каждого блока выбранного типа шаблона.')
                    return True
                clean['ask_fields']=[key for key in clean.get('ask_fields',[]) if key not in ('comments','replies')]
            if mode in MEDIA_TEMPLATE_MODES:
                payload={'media':{key:value for key,value in clean.items() if key!='name'}}
            elif mode=='source_reply':
                payload={key:value for key,value in clean.items() if key!='name'}
            else:
                clean['options']=order_settings(mode,{**clean,'options':{key:value for key,value in (clean.get('options') or {}).items() if key!='targetCount'}})
                if mode=='fetch':
                    for field,keys in {'fetch_count':('fetchCount','collectAll'),'fetch_replies':('includeReplies',)}.items():
                        if field in clean.get('ask_fields',[]):
                            clean.pop(field,None)
                            for key in keys:clean['options'].pop(key,None)
                payload={'draft':clean}
            try:
                tid=self.db.save_template(uid,clean['name'],mode,payload,
                                          template_id=data['template_id'],draft_token=token)
            except ValueError as exc:
                await self.say(uid,h(exc),keyboard([('▧ Мои шаблоны','templates',None)]))
            else:
                await self.notify_template_review(uid,tid)
                await self.template_card(uid,tid)
        elif verb=='auto' and step=='name':
            await self.local_template_input(uid,TEMPLATE_LABELS[data['mode']])
        elif verb=='keep' and step in values:
            await self.next_template_field(uid,data,step)
        elif verb=='off' and data['mode']=='publish' and step in ('comments','replies'):
            values[step]=[]
            disabled=values.setdefault('disabled_fields',[])
            if step not in disabled:disabled.append(step)
            values['ask_fields']=[key for key in values.get('ask_fields',[]) if key!=step]
            await self.next_template_field(uid,data,step)
        elif verb=='skip' and step!='name' and not (data['mode'] in COMMENT_TEMPLATE_MODES and step in ('comments','replies')):
            values['disabled_fields']=[key for key in values.get('disabled_fields',[]) if key!=step]
            if step=='warm_json' and isinstance(values.get(step),dict):
                values[step]={key:value for key,value in values[step].items() if key not in ('topics','keywords','hashtags')}
            else:
                values.pop(step,None)
            if step=='options':values.pop('form_ready',None)
            if step in ('videos','comments','replies','reply_links','like_links','keywords','warm_json','options','fetch_count','fetch_replies','fetch_keywords'):
                deferred=values.setdefault('ask_fields',[])
                if step not in deferred:deferred.append(step)
            await self.next_template_field(uid,data,step)
        elif verb in ('fetch100','fetch1000') and step=='fetch_count':
            values['ask_fields']=[key for key in values.get('ask_fields',[]) if key!=step]
            values[step]=100 if verb=='fetch100' else 1000
            await self.next_template_field(uid,data,step)
        elif verb in ('replies_yes','replies_no') and step=='fetch_replies':
            values['ask_fields']=[key for key in values.get('ask_fields',[]) if key!=step]
            values[step]=verb=='replies_yes'
            await self.next_template_field(uid,data,step)
        return True
