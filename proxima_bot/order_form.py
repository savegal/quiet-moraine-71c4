"""Human-readable controls for documented per-order API settings."""
from __future__ import annotations

from dataclasses import dataclass
import re
from .catalog import username_scan_settings


@dataclass(frozen=True)
class OrderField:
    key: str
    label: str
    default: int | bool
    maximum: int = 100
    minimum: int = 0
    hint: str = ''
    warm: bool = False

    @property
    def boolean(self):
        return type(self.default) is bool


def publication_blocks(mode:str,data:dict)->tuple[bool,bool]:
    """Use the same material switches in orders, templates, and payloads."""
    disabled=set(data.get('disabled_fields') or [])
    publication=mode in ('publish','reply_links','keyword_reply','source_reply')
    comments=mode=='publish' and 'comments' not in disabled and bool(data.get('comments') or data.get('form_template'))
    replies=publication and 'replies' not in disabled and bool(data.get('replies') or data.get('form_template'))
    return comments,replies


def reply_quantity(data:dict)->int:
    value=(data.get('options') or {}).get('repliesPerComment')
    return 3 if value is None else value


def order_fields(mode:str,data:dict)->list[OrderField]:
    fields=[]
    comments,replies=publication_blocks(mode,data)
    if comments:fields.append(OrderField('commentsPerVideo','Комментариев на видео',50,100,1))
    if replies:fields.append(OrderField('repliesPerComment','Ответов на каждый комментарий',3,20,0 if comments else 1))
    if comments:fields.append(OrderField('likesPerComment','Лайков на главный комментарий',15,100,0))
    if replies and reply_quantity(data)>0:
        fields.append(OrderField('likesPerReply','Лайков на ответный комментарий',10,100,0))
    if mode=='keyword_reply':fields.append(OrderField('fetchCount','Просмотреть комментариев',100,1000,1))
    if mode=='usernames':fields.append(OrderField('fetchCount','Просмотреть комментариев',100,1000,1))
    if mode=='feedwarm':
        fields.extend([OrderField('searchLimit','Максимум результатов',100,1000,1,warm=True),
                       OrderField('periodDays','Период поиска, дней',7,365,1,warm=True),
                       OrderField('minLikes','Минимум лайков видео',0,1000000,warm=True),
                       OrderField('minComments','Минимум комментариев видео',0,1000000,warm=True)])
    return fields


def field_value(field:OrderField,data:dict):
    values=data.get('warm_json' if field.warm else 'options') or {}
    return values.get(field.key) if values.get(field.key) is not None else field.default


def field_text(field:OrderField,value)->str:
    if field.boolean:return 'Вкл.' if value else 'Выкл.'
    if field.key in ('likesPerComment','likesPerReply') and value==0:return 'Без лайков'
    return str(value)


def parse_field(field:OrderField,text:str):
    if field.boolean:raise ValueError('Выбери «Включить» или «Выключить» кнопкой.')
    if not re.fullmatch(r'[0-9]{1,7}',text.strip()):
        raise ValueError(f'Введи целое число от {field.minimum} до {field.maximum}.')
    value=int(text.strip())
    if not field.minimum<=value<=field.maximum:
        raise ValueError(f'Допустимо от {field.minimum} до {field.maximum}.')
    return value


def set_field(data:dict,field:OrderField,value)->dict:
    key='warm_json' if field.warm else 'options'
    return {**data,key:{**(data.get(key) or {}),field.key:value}}


def complete_order_form(mode:str,data:dict)->dict:
    """Freeze visible choices in the payload instead of inheriting hidden defaults."""
    if mode=='source_reply' and not data.get('target_count'):
        data={**data,'target_count':(data.get('options') or {}).get('targetCount') or 100}
    for field in order_fields(mode,data):
        data=set_field(data,field,field_value(field,data))
    if mode in ('publish','reply_links','keyword_reply','source_reply'):
        comments,replies=publication_blocks(mode,data)
        options={key:value for key,value in (data.get('options') or {}).items() if key not in ('accountsPerVideo','repliesPerVideo','uniqueText','delaySeconds','targetCount')}
        options.update(delaySeconds=2,uniqueText=False)
        if not comments:
            options.update(commentsPerVideo=0,likesPerComment=0)
        if not replies:options.update(repliesPerVideo=0,repliesPerComment=0,likesPerReply=0)
        if not options.get('repliesPerComment'):options['likesPerReply']=0
        options['repliesPerVideo']=(options.get('commentsPerVideo',50) if comments else 10000)*options.get('repliesPerComment',0)
        comment_likes=bool(options.get('likesPerComment',0)>0)
        reply_likes=bool(options.get('likesPerReply',0)>0)
        options.update(likeEnabled=comment_likes or reply_likes,likeComments=comment_likes,likeReplies=reply_likes)
        data={**data,'options':options}
    if mode=='fetch' and not data.get('form_template'):
        data={**data,'fetch_count':data.get('fetch_count') if data.get('fetch_count') in (100,1000) else 100,
              'fetch_replies':bool(data.get('fetch_replies',False))}
    if mode=='keyword_reply':
        data={**data,'options':{**data['options'],'collectAll':False}}
    if mode=='usernames':
        data={**data,'options':username_scan_settings(data['options'])}
    if mode=='feedwarm':
        data={**data,'warm_json':{**(data.get('warm_json') or {}),'comment':False,'like':False,'dryRun':True,'search':True,'noFeed':True}}
    return {**data,'options':{**(data.get('options') or {}),'delaySeconds':2,'uniqueText':False},'form_ready':True}


def edit_order_form(mode:str,data:dict,verb:str)->dict|None:
    fields=order_fields(mode,data)
    if verb=='form_more':return {**data,'form_more':not data.get('form_more',False)}
    if verb=='volume_typical':
        for field in fields:
            value={'commentsPerVideo':50,'repliesPerComment':3,'likesPerComment':15,'likesPerReply':10}.get(field.key)
            if value is not None:data=set_field(data,field,value)
        return data
    if verb.startswith('pick_'):
        key,_,value=verb[5:].rpartition('_')
        field=next((item for item in fields if item.key==key),None)
        if not field or field.boolean:return None
        return set_field(data,field,parse_field(field,value))
    if verb=='form_back':
        data=dict(data)
        data.pop('form_field',None)
        return data
    if verb=='form_advanced':return None
    if verb.startswith('form_'):
        field=next((item for item in fields if item.key==verb[5:]),None)
        if not field:return None
        return set_field(data,field,not field_value(field,data)) if field.boolean else {**data,'form_field':field.key}
    if verb.startswith('value_'):
        field=next((item for item in fields if item.key==data.get('form_field')),None)
        if not field or field.boolean:return None
        data=set_field(data,field,parse_field(field,verb[6:]))
        data.pop('form_field',None)
        return data
    return None


def form_rows(mode:str,data:dict)->list[tuple[str,str]]:
    return [(field.label,field_text(field,field_value(field,data))) for field in order_fields(mode,data)]


VOLUME_KEYS={'commentsPerVideo','repliesPerComment','likesPerComment','likesPerReply','fetchCount','searchLimit','periodDays','minLikes','minComments'}


def field_presets(field:OrderField)->tuple[int,...]:
    values={'commentsPerVideo':(25,50,75,100),'repliesPerComment':(0,2,3,5),
            'likesPerComment':(5,10,15,20),'likesPerReply':(5,10,15,20),
            'fetchCount':(100,1000)}.get(field.key,(1,10,100))
    return tuple(n for n in values if field.minimum<=n<=field.maximum)


VOLUME_ALIASES={'комментарии':'commentsPerVideo','ответы':'repliesPerComment',
             'ответов на комментарий':'repliesPerComment','лайки':'likesPerComment',
             'лайки ответам':'likesPerReply',
             'лимит сбора':'fetchCount','максимум результатов':'searchLimit','период поиска':'periodDays',
             'минимум лайков':'minLikes','минимум комментариев':'minComments'}


def volume_example(mode:str,data:dict)->str:
    return ', '.join(next(label.capitalize() for label,key in VOLUME_ALIASES.items() if key==field.key)+': '+str(field.default)
                     for field in order_fields(mode,data))


def parse_volume_message(mode:str,data:dict,text:str)->dict:
    def invalid_label():
        example=volume_example(mode,data)
        return ValueError('Используй подписи: '+example+' — по одной на строку.' if example else 'Подтверди показанный объём кнопкой.')

    values={}
    for line in text.splitlines():
        if not line.strip():continue
        label,sep,value=line.partition(':')
        key=VOLUME_ALIASES.get(label.strip().lower())
        if not sep or key is None:raise invalid_label()
        if key in values:raise ValueError('Параметр указан дважды: '+label.strip())
        values[key]=value
    if not values:raise ValueError('Укажи хотя бы одно количество.')

    result=data
    fields={field.key:field for field in order_fields(mode,data)}
    if 'repliesPerComment' in values:
        field=fields.get('repliesPerComment')
        if field is None:raise invalid_label()
        result=set_field(result,field,parse_field(field,values.pop(field.key)))
        fields={field.key:field for field in order_fields(mode,result)}
    for key,value in values.items():
        if key not in fields:raise invalid_label()
        result=set_field(result,fields[key],parse_field(fields[key],value))
    return result
