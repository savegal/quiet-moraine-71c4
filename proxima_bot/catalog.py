"""Declared Tokora task kinds and a schema-backed, no-censorship payload assembler.

OpenAPI does not enumerate task kinds in TaskStart.kind, but documents them
in GET /v1/tasks kind filter. Modes with uncertain fields are kept explicit,
so the upstream validates them and no undocumented wire field is invented.
"""
from __future__ import annotations
import json
import re
from urllib.parse import urlsplit
from .operations import load_spec, valid_body

TASK_KINDS=('publish','fetch','commentlike','keyword_reply','feedwarm','usernames','check','import')
MODES={
 'publish':('Комментарии и ответы','videos','comments','replies','options'),
 'reply_links':('Ответы по ссылкам','reply_links','replies','options'),
 'fetch':('Сбор комментариев','videos','options'),
 'commentlike':('Лайки комментариев','like_links','likes','options'),
 'keyword_reply':('Ответы по ключам','keywords','replies','options'),
 'feedwarm':('Поиск и прогрев','warm_json','options'),
 'usernames':('Сбор юзернеймов','videos','options'),
 'check':('Проверка аккаунтов','options'),
}
MODE_KIND={m:(m if m in TASK_KINDS else 'publish') for m in MODES}
MODE_KIND['reply_links']='publish'
OWNER_ONLY={'check'}

VIDEO_ID=re.compile(r'/video/([0-9]{10,30})(?:/|$)')
TIKTOK_HOST=re.compile(r'(?:[a-z0-9-]+\.)*(?:tiktok\.com|tiktokv\.com)')
VALID_LONG_ID=re.compile(r'[0-9]{10,30}')
COMMENT_IDS=re.compile(r'[0-9]{10,30}\s*[:\s]\s*[0-9]{10,30}')
BOOLEANS={'includeReplies','collectAll','likeComments','likeReplies','likeEnabled','useReplyTelegram','uniqueText'}
STRING_FIELDS={'commentMethod','replyMethod','telegram'}

# Local admission limits for explicit task overrides. Tokora's own limits and
# defaults are independent. Admins can still edit the general workspace settings.
TASK_LIMITS={
    'commentsPerVideo':100,'repliesPerVideo':100,'repliesPerComment':20,'accountsPerVideo':100,
    'videoCount':100,'workers':20,'fetchWorkers':20,'verifyWorkers':20,'verifyIntervalSeconds':3600,
    'verifyTimeoutSeconds':3600,'attemptsPerItem':10,'fetchCount':1000,'likesPerItem':100,
    'likeWorkers':20,'likeRetries':10,'accountDailyLimit':1000,'likesPerComment':100,'likesPerReply':100,
}
ALLOWED_OPTIONS=set(TASK_LIMITS)|BOOLEANS|STRING_FIELDS|{'delaySeconds','fetchKeywords'}
FEEDWARM_LIMITS={'accounts':100,'perAccount':100,'maxActions':2000,'searchLimit':1000,'periodDays':365}
WIZARD_FIELDS={'keyword_reply':'fetchKeywords','commentlike':'likesPerComment'}


def limited_lines(text:str,maximum:int=300,line_length:int=1000)->list[str]:
    result=[s.strip() for s in text.splitlines() if s.strip()]
    if not result or len(result)>maximum or any(len(s)>line_length for s in result):
        raise ValueError(f'Нужно 1–{maximum} непустых строк, максимум {line_length} символов.')
    return result


def is_tiktok_url(value:str)->bool:
    if any(char.isspace() or char=='\\' or ord(char)<32 or ord(char)==127 for char in value):
        return False
    try:
        url=urlsplit(value)
        default_port={'http':80,'https':443}.get(url.scheme)
        return bool(default_port and TIKTOK_HOST.fullmatch(url.hostname or '')
                    and url.username is None and url.password is None and url.port in (None,default_port))
    except ValueError:
        return False


def validate_targets(items:list[str])->list[str]:
    if not isinstance(items,list) or not 1<=len(items)<=100 or any(
            not isinstance(x,str) or len(x)>450 or not (VALID_LONG_ID.fullmatch(x) or is_tiktok_url(x))
            for x in items):
        raise ValueError('Разрешены ссылки TikTok или числовые ID видео.')
    return list(dict.fromkeys(items))


def parse_targets(text:str)->list[str]:
    return validate_targets(limited_lines(text,100,450))


def parse_options(text:str)->dict:
    if not text.strip() or text.strip() in ('-','{}'):return {}
    try: data=json.loads(text)
    except json.JSONDecodeError:raise ValueError('Настройки должны быть JSON-объектом.') from None
    return _validate_options(data)


def _validate_options(data:dict)->dict:
    if not isinstance(data,dict) or len(data)>30:raise ValueError('Настройки: JSON-объект максимум с 30 полями.')
    unknown=set(data)-ALLOWED_OPTIONS
    if unknown:raise ValueError('Неизвестные параметры: '+', '.join(sorted(map(str,unknown))[:8]))
    for key,v in data.items():
        if v is None:continue
        if key in BOOLEANS and type(v) is not bool:raise ValueError(f'{key}: требуется true/false')
        if key in TASK_LIMITS and (type(v) is not int or not 0<=v<=100000):raise ValueError(f'{key}: требуется целое 0–100000')
        if key=='delaySeconds' and (type(v) not in (int,float) or not 0<=v<=3600):raise ValueError('delaySeconds: 0–3600')
        if key in STRING_FIELDS and (not isinstance(v,str) or len(v)>200):raise ValueError(f'{key}: строка до 200 символов')
        if key=='fetchKeywords' and (not isinstance(v,list) or len(v)>100 or any(not isinstance(x,str) or len(x)>100 for x in v)):
            raise ValueError('fetchKeywords: массив до 100 строк')
    return data


def validate_task_options(options:dict|None,*,mode:str|None=None)->dict:
    data=_validate_options({} if options is None else options)
    field=WIZARD_FIELDS.get(mode)
    if field and field in data:
        raise ValueError(f'{field} уже задан на предыдущем шаге. Измени его в мастере.')
    for key,maximum in TASK_LIMITS.items():
        value=data.get(key)
        if value is not None and value>maximum:
            raise ValueError(f'Лимит бота для {key} — {maximum}.')
    return data


def validate_comment_links(items:list[str],maximum:int=300)->list[str]:
    if not isinstance(items,list) or not 1<=len(items)<=maximum:
        raise ValueError(f'Нужно от 1 до {maximum} ссылок на комментарии.')
    for x in items:
        if not isinstance(x,str) or len(x)>700:
            raise ValueError('Ссылка на комментарий должна быть строкой до 700 символов.')
        if COMMENT_IDS.fullmatch(x) or is_tiktok_url(x):continue
        parts=x.rsplit(maxsplit=1)
        if len(parts)==2 and VALID_LONG_ID.fullmatch(parts[1]) and is_tiktok_url(parts[0]):continue
        raise ValueError('Используй ссылки TikTok или пары числовых ID видео и комментария.')
    return list(dict.fromkeys(items))


def parse_comment_links(text:str,maximum:int=300)->list[str]:
    return validate_comment_links(limited_lines(text,maximum,700),maximum)


def parse_feedwarm(text:str)->dict:
    try: value=json.loads(text)
    except json.JSONDecodeError:raise ValueError('Нужен JSON с параметрами поиска и прогрева.') from None
    return validate_feedwarm(value)


def validate_feedwarm(value:dict,*,account_count:int|None=None)->dict:
    if not isinstance(value,dict):raise ValueError('Нужен JSON-объект.')
    schema=load_spec()['components']['schemas']['FeedWarmStart']
    unknown=set(value)-set(schema['properties'])
    if unknown:raise ValueError('Неизвестные параметры FeedWarm: '+', '.join(sorted(map(str,unknown))))
    for key in ('keywords','hashtags','sampleLinks','topics'):
        v=value.get(key,[])
        if not isinstance(v,list) or len(v)>100 or any(not isinstance(x,str) or len(x)>500 for x in v):
            raise ValueError(key+': требуется список строк')
    if len(json.dumps(value))>10000:raise ValueError('Слишком большой JSON')
    if not valid_body(schema,value):raise ValueError('Поля feedWarm имеют неверный тип.')
    if any(not is_tiktok_url(link) for link in value.get('sampleLinks',[])):
        raise ValueError('sampleLinks принимает только ссылки TikTok.')
    for key,maximum in FEEDWARM_LIMITS.items():
        if key in value and (type(value[key]) is not int or not 0<=value[key]<=maximum):
            raise ValueError(f'Лимит бота для {key} — от 0 до {maximum}.')
    if account_count is not None and value.get('accounts',0)>account_count:
        raise ValueError(f'Для поиска доступно {account_count} назначенных аккаунтов.')
    return value


async def build_payload(api,mode:str,data:dict,account_ids:list[str])->dict:
    if mode not in MODES:raise ValueError('Неизвестный вид задачи')
    if not account_ids:raise ValueError('Нет назначенных аккаунтов')
    options=validate_task_options(data.get('options'),mode=mode)
    assets={'accountIds':account_ids}
    payload={'kind':MODE_KIND[mode],'name':data['name'],'assets':assets,
             'disableCommentPhotos':True,'disableReplyPhotos':True,'usePhoto':False}
    if mode in ('publish','fetch','usernames'):
        ids=[]
        for t in validate_targets(data['videos']):
            match=VIDEO_ID.search(urlsplit(t).path)
            vid=t if VALID_LONG_ID.fullmatch(t) else (match.group(1) if match else await api.resolve(t))
            if vid not in ids:ids.append(vid)
        payload['awemeIds']=ids
    if mode=='publish':
        assets['commentTexts']=data.get('comments',[])
        assets['replyTexts']=data.get('replies',[])
        if not assets['commentTexts'] and not assets['replyTexts']:raise ValueError('Нужны комментарии или ответы')
    if mode=='reply_links':
        links=validate_comment_links(data['reply_links'])
        result=await api.call('POST','/v1/tasks/reply-links/resolve',payload={'links':links})
        items=result.get('items')
        if (result.get('failed') or not isinstance(items,list) or len(items)!=len(links)
                or any(not isinstance(item,dict) or item.get('error')
                       or (item.get('duplicate') is not True and
                           (not isinstance(item.get('value'),str) or not item['value'].strip())) for item in items)):
            raise ValueError('Не все ссылки на комментарии удалось проверить. Проверь список и повтори.')
        resolved=list(dict.fromkeys(item['value'] for item in items if item.get('duplicate') is not True))
        if not resolved:raise ValueError('Не удалось определить комментарии по ссылкам.')
        payload['replyLinks']=resolved
        assets['replyTexts']=data['replies']
    if mode=='commentlike':
        likes=data['likes']
        if type(likes) is not int or not 1<=likes<=100:raise ValueError('Укажи 1–100 лайков.')
        payload['likeLinks']=validate_comment_links(data['like_links'])
        payload['likesPerComment']=likes
    if mode=='keyword_reply':
        assets['replyTexts']=data['replies']
        keywords=data.get('keywords')
        if not isinstance(keywords,list) or not keywords or any(
                not isinstance(word,str) or not word.strip() for word in keywords):
            raise ValueError('Добавь ключевые фразы для ответов.')
        options={**options,'fetchKeywords':keywords}
        validate_task_options(options)
    if mode=='feedwarm':payload['feedWarm']=validate_feedwarm(data['warm_json'],account_count=len(account_ids))
    if options:
        payload['settings']=options
    # Do not overwrite uniqueText or censor a word in the user's materials.
    return payload
