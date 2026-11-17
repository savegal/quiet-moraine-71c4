"""Declared Tokora task kinds and a schema-backed, no-censorship payload assembler.

OpenAPI does not enumerate task kinds in TaskStart.kind, but documents them
in GET /v1/tasks kind filter. Modes with uncertain fields are kept explicit,
so the upstream validates them and no undocumented wire field is invented.
"""
from __future__ import annotations
import asyncio
import json
import re
from urllib.parse import urlsplit, parse_qs
from .api import ApiError
from .operations import VALID_ID, load_spec, valid_body

TASK_KINDS=('publish','fetch','commentlike','keyword_reply','feedwarm','usernames','check','import')
MODES={
 'publish':('Комментарии и ответы','videos','comments','replies','options'),
 'reply_links':('Ответы по ссылкам','reply_links','replies','options'),
 'fetch':('Сбор комментариев','videos','fetch_count','fetch_replies','fetch_keywords','options'),
 'commentlike':('Лайки комментариев','like_links','likes','options'),
 'keyword_reply':('Ответы по ключам','videos','keywords','replies','options'),
 'feedwarm':('Сбор видео','warm_json','options'),
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
MAX_COLLECTED_REPLY_TARGETS = 100_000
MAX_REPLY_LINKS = 300
MAX_REPLY_INPUT_LINES = 3_000
MAX_REPLY_LINK_LENGTH = 2_048
BOOLEANS={'includeReplies','collectAll','likeComments','likeReplies','likeEnabled','useReplyTelegram','uniqueText'}
STRING_FIELDS={'commentMethod','replyMethod','telegram'}

# Local admission limits for explicit task overrides. Tokora's own limits and
# defaults are independent. Admins can still edit the general workspace settings.
TASK_LIMITS={
    'commentsPerVideo':100,'repliesPerVideo':200000,'repliesPerComment':20,'accountsPerVideo':100,
    'videoCount':100,'workers':20,'fetchWorkers':20,'verifyWorkers':20,'verifyIntervalSeconds':3600,
    'verifyTimeoutSeconds':3600,'attemptsPerItem':10,'fetchCount':1000,'likesPerItem':100,
    'likeWorkers':20,'likeRetries':10,'accountDailyLimit':1000,'likesPerComment':100,'likesPerReply':100,
    'settleMinutes':1440,
}
ALLOWED_OPTIONS=set(TASK_LIMITS)|BOOLEANS|STRING_FIELDS|{'delaySeconds','fetchKeywords'}
MAX_FETCH_COUNT=TASK_LIMITS['fetchCount']
FEEDWARM_LIMITS={'accounts':100,'perAccount':100,'maxActions':2000,'searchLimit':1000,'periodDays':365}
WIZARD_FIELDS={'keyword_reply':'fetchKeywords','commentlike':'likesPerComment'}
PHOTO_LABELS={'off':'Без фото','comments':'В комментарии','replies':'В ответы','both':'В комментарии и ответы'}


def photo_choices(mode:str,data:dict)->tuple[str,...]:
    from .order_form import publication_blocks, reply_quantity
    comments,replies=publication_blocks(mode,data)
    replies=replies and reply_quantity(data)>0
    if mode=='publish':
        if comments and replies:return ('comments','replies','both')
        if comments:return ('comments',)
        if replies:return ('replies',)
    if mode in ('reply_links','keyword_reply','source_reply') and replies:
        return ('replies',)
    return ()


def task_photo_fields(mode:str,data:dict)->dict[str,bool]:
    setting=data.get('photo_mode','off')
    if setting!='off' and setting not in photo_choices(mode,data):
        raise ValueError('Выбери фото для тех публикаций, для которых заполнены тексты.')
    return {'usePhoto':setting!='off',
            'disableCommentPhotos':setting not in ('comments','both'),
            'disableReplyPhotos':setting not in ('replies','both')}


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


def validate_username_targets(items: list[str]) -> list[str]:
    targets = validate_targets(items)
    for target in targets:
        if VALID_LONG_ID.fullmatch(target):
            continue
        path = urlsplit(target).path.strip('/')
        if not path or re.fullmatch(r'@[^/]+', path):
            raise ValueError('Для сбора юзернеймов нужна ссылка на конкретное видео или его ID.')
    return targets


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
        if key in TASK_LIMITS:
            upper=max(100000,TASK_LIMITS[key])
            if type(v) is not int or not 0<=v<=upper:
                raise ValueError(f'{key}: требуется целое 0–{upper}')
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


def parse_fetch_count(text:str)->int:
    value=text.strip()
    if not re.fullmatch(r'[0-9]{1,4}',value) or not 1<=int(value)<=MAX_FETCH_COUNT:
        raise ValueError(f'Укажи целое число от 1 до {MAX_FETCH_COUNT}.')
    return int(value)


def order_settings(mode:str,data:dict)->dict:
    options=validate_task_options(data.get('options'),mode=mode)
    if mode=='usernames':
        return username_scan_settings(options)
    if mode!='fetch':
        return options
    options=options.copy()
    count=data.get('fetch_count')
    options.update(fetchCount=count if count in (100,1000) else 100,collectAll=False,
                   includeReplies=bool(data.get('fetch_replies',False)))
    return options


def username_scan_settings(options: dict) -> dict:
    return {**options, 'fetchCount': options.get('fetchCount') or 100, 'collectAll': False, 'includeReplies': True}


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


def validate_like_links(items:list[str])->list[str]:
    targets=[]
    for link in validate_comment_links(items):
        if COMMENT_IDS.fullmatch(link):
            targets.append(':'.join(re.findall(r'[0-9]+',link)))
            continue
        parts=link.rsplit(maxsplit=1)
        url=urlsplit(parts[0])
        if len(parts)==2:
            video=VIDEO_ID.search(url.path)
            targets.append(f'{video.group(1)}:{parts[1]}' if video else link)
            continue
        query=parse_qs(url.query)
        if '/video/' in url.path and not any(query.get(key) for key in ('comment_id','cid','reply_id')):
            raise ValueError('Нужна ссылка на комментарий или ссылка на видео с отдельным ID комментария.')
        targets.append(link)
    return list(dict.fromkeys(targets))


def validate_reply_links(lines:list[str])->list[str]:
    if not isinstance(lines,list) or len(lines)>MAX_REPLY_INPUT_LINES:
        raise ValueError('Во входном списке допускается до 3000 строк, включая пустые.')
    links=[]
    for index,line in enumerate(lines,1):
        if not isinstance(line,str) or len(line)>MAX_REPLY_LINK_LENGTH or len(line.splitlines())>1:
            raise ValueError(f'Строка {index}: нужна одна ссылка или пара ID длиной до 2048 символов.')
        value=line.strip()
        if not value:
            continue
        parts=value.rsplit(maxsplit=1)
        if not (COMMENT_IDS.fullmatch(value) or is_tiktok_url(value)
                or (len(parts)==2 and VALID_LONG_ID.fullmatch(parts[1]) and is_tiktok_url(parts[0]))):
            raise ValueError(f'Строка {index}: укажи ссылку TikTok или пару ID видео и комментария.')
        links.append(value)
    if not 1<=len(links)<=MAX_REPLY_LINKS:
        raise ValueError('Укажи от 1 до 300 непустых строк. Повторы тоже входят в лимит ввода.')
    return links


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


def video_search_terms(settings: dict) -> list[str]:
    if not isinstance(settings, dict):
        return []
    terms = []
    for field in ('topics', 'keywords', 'hashtags'):
        values = settings.get(field)
        if isinstance(values, list):
            terms.extend(value.strip() for value in values if isinstance(value, str) and value.strip())
    return list(dict.fromkeys(terms))


def edit_video_search_terms(text: str, current: dict | None = None) -> dict:
    if text.lstrip().startswith(('{', '[', '"')):
        raise ValueError('Напиши тему поиска обычным текстом.')
    settings = {key: value for key, value in (current or {}).items() if key not in ('keywords', 'hashtags')}
    return {**settings, 'topics': list(dict.fromkeys(limited_lines(text, 20, 100))),
            'topTag': True, 'phraseSearch': True}


def video_search_settings(value: dict, *, account_count: int | None = None) -> dict:
    if not isinstance(value, dict):
        raise ValueError('Укажи тему поиска видео.')
    settings = validate_feedwarm({**value, 'comment': False, 'like': False, 'dryRun': True,
                                  'search': True, 'noFeed': True}, account_count=account_count)
    if not video_search_terms(settings):
        raise ValueError('Укажи хотя бы одну тему поиска видео.')
    return settings


class ReplyLinkResolutionError(ValueError):
    def __init__(self, lines:list[int]):
        self.lines=lines
        super().__init__('Не удалось проверить ссылки. Ошибки в непустых строках: '+', '.join(map(str,lines[:10])))


async def resolve_reply_targets(api,links:list[str])->list[str]:
    result=await api.call('POST','/v1/tasks/reply-links/resolve',payload={'links':links})
    items=result.get('items')
    if not isinstance(items,list) or len(items)!=len(links) or any(not isinstance(item,dict) for item in items):
        raise ApiError('invalid_response')
    numbered = any('line' in item for item in items)
    by_line = {}
    for index, item in enumerate(items, 1):
        line = item.get('line') if numbered else index
        if type(line) is not int or not 1 <= line <= len(links) or line in by_line:
            raise ApiError('invalid_response')
        if 'input' in item and item['input'] != links[line - 1]:
            raise ApiError('invalid_response')
        by_line[line] = item
    errors=[]
    resolved=[]
    for line in range(1, len(links) + 1):
        item = by_line[line]
        if item.get('error'):
            errors.append(line)
            continue
        if item.get('duplicate') is True:
            continue
        value=item.get('value')
        if not isinstance(value,str) or not COMMENT_IDS.fullmatch(value.strip()):
            raise ApiError('invalid_response')
        resolved.append(':'.join(re.findall(r'[0-9]+',value)))
    if errors:
        raise ReplyLinkResolutionError(errors)
    if result.get('failed'):
        raise ApiError('invalid_response')
    if not resolved:
        raise ApiError('invalid_response')
    return list(dict.fromkeys(resolved))


def is_collection_reply(payload: dict) -> bool:
    return (isinstance(payload.get('sourceJob'), str) and payload.get('replySourceMode') in (None, 'all')
            and isinstance(payload.get('targetIds'), list) and bool(payload['targetIds']))


def valid_collection_targets(targets:object)->bool:
    return (isinstance(targets,list) and 1<=len(targets)<=MAX_COLLECTED_REPLY_TARGETS
            and all(isinstance(target,str) and VALID_LONG_ID.fullmatch(target) for target in targets)
            and len(set(targets))==len(targets))


async def collected_reply_targets(api, source_id: str, count: int, *, previous: list[str] | None = None) -> list[str]:
    """Select root CIDs, optionally restoring an exact selection from an older draft."""
    if not VALID_ID.fullmatch(source_id) or not 1 <= count <= MAX_COLLECTED_REPLY_TARGETS:
        raise ValueError('Проверь исходный сбор и количество комментариев.')
    if previous is not None and (len(previous) != count or any(not isinstance(x, str) or not x for x in previous)):
        raise ValueError('Не удалось восстановить выбранные комментарии. Открой исходный сбор.')
    remaining = set(previous or [])
    matched = {}
    targets = {}
    seen_pages = set()
    offset = 0
    while True:
        for attempt in range(2):
            try:
                page = await api.call('GET', '/v1/tasks/' + source_id + '/entries',
                                      params={'kind': 'comment', 'offset': offset, 'limit': 100})
                break
            except ApiError as exc:
                if exc.status!=429 or attempt or exc.retry_after>60:
                    raise
                await asyncio.sleep(max(1,exc.retry_after))
        items = page.get('items')
        more = page.get('more')
        if (not isinstance(items,list) or len(items)>100 or type(more) is not bool
                or (more and not items) or page.get('offset',offset)!=offset):
            raise ApiError('invalid_response')
        signature=tuple(str(item.get('entryId') or item.get('cid') or '') for item in items if isinstance(item,dict))
        if signature in seen_pages or (more and not signature):
            raise ApiError('invalid_response')
        seen_pages.add(signature)
        for item in items:
            if not isinstance(item,dict) or item.get('kind')!='comment' or item.get('replyId'):
                continue
            video, comment = item.get('awemeId'), item.get('cid')
            if not isinstance(comment,str) or not VALID_LONG_ID.fullmatch(comment):
                continue
            if previous is None:
                targets[comment] = None
                if len(targets) >= count:
                    return list(targets)
            else:
                for key in (item.get('entryId'),comment,f'{video}:{comment}'):
                    if isinstance(key,str) and key in remaining:
                        matched[key] = comment
                        remaining.remove(key)
                if not remaining:
                    return list(dict.fromkeys(matched[key] for key in previous))
        if not more:
            break
        offset += len(items)
    if previous is not None:
        raise ValueError('Не удалось восстановить все выбранные комментарии. Открой исходный сбор.')
    return list(targets)


async def resolve_payload_targets(api,payload:dict)->dict:
    """Resolve targets only in the worker, before freezing any launch request."""
    result=dict(payload)
    if payload.get('awemeIds'):
        ids=[]
        for target in validate_targets(payload['awemeIds']):
            match=VIDEO_ID.search(urlsplit(target).path)
            video_id=target if VALID_LONG_ID.fullmatch(target) else match.group(1) if match else await api.resolve(target)
            if not isinstance(video_id,str) or not VALID_LONG_ID.fullmatch(video_id):
                raise ValueError('Не удалось определить видео по ссылке.')
            if video_id not in ids:ids.append(video_id)
        result['awemeIds']=ids
    if payload.get('sourceJob') and 'replyLinks' in payload:
        raise ValueError('Выбери исходный сбор или ссылки на комментарии.')
    if 'targetIds' in payload:
        if not payload.get('sourceJob'):
            raise ValueError('Для выбранных комментариев нужен исходный сбор.')
        if payload.get('replySourceMode') is None and not valid_collection_targets(payload['targetIds']):
            raise ValueError('Укажи от 1 до 100 000 ID комментариев из сбора.')
    if 'replyLinks' in payload:
        links = validate_reply_links(payload['replyLinks'])
        if all(COMMENT_IDS.fullmatch(link) for link in links):
            result['replyLinks'] = list(dict.fromkeys(':'.join(re.findall(r'[0-9]+',link)) for link in links))
        else:
            result['replyLinks'] = await resolve_reply_targets(api,links)
    if 'likeLinks' in payload:
        result['likeLinks']=validate_like_links(payload['likeLinks'])
    return result


async def build_payload(api,mode:str,data:dict,account_ids:list[str],*,defer_resolution:bool=False)->dict:
    if mode not in MODES:raise ValueError('Неизвестный вид задачи')
    if not account_ids:raise ValueError('Нет назначенных аккаунтов')
    options=order_settings(mode,data)
    assets={'accountIds':account_ids}
    payload={'kind':MODE_KIND[mode],'name':data['name'],'assets':assets,
             **task_photo_fields(mode,data)}
    if mode in ('publish','reply_links','keyword_reply'):
        from .order_form import publication_blocks
        comments,replies=publication_blocks(mode,data)
        assets['replyTexts']=(data.get('replies') or []) if replies else []
        if mode=='publish':
            assets['commentTexts']=(data.get('comments') or []) if comments else []
            if not assets['commentTexts'] and not assets['replyTexts']:
                raise ValueError('Нужны комментарии или ответы.')
        elif not assets['replyTexts']:
            raise ValueError('Добавь тексты ответов.')
    if mode in ('publish','fetch','usernames','keyword_reply'):
        if mode=='keyword_reply' and not data.get('videos'):
            raise ValueError('Добавь видео, под которыми нужно искать комментарии по фразам.')
        ids=[]
        targets = data.get('videos')
        for t in (validate_username_targets(targets) if mode=='usernames' else validate_targets(targets)):
            match=VIDEO_ID.search(urlsplit(t).path)
            vid=t if VALID_LONG_ID.fullmatch(t) else (match.group(1) if match else t if defer_resolution else await api.resolve(t))
            if vid not in ids:ids.append(vid)
        payload['awemeIds']=ids
    if mode=='reply_links':
        links=validate_reply_links(data['reply_links'])
        payload['replyLinks']=links if defer_resolution else await resolve_reply_targets(api,links)
    if mode=='commentlike':
        likes=data['likes']
        if type(likes) is not int or not 1<=likes<=100:raise ValueError('Укажи 1–100 лайков.')
        payload['likeLinks']=validate_like_links(data['like_links'])
        payload['likesPerComment']=likes
    if mode=='keyword_reply':
        keywords=data.get('keywords')
        if not isinstance(keywords,list) or not keywords or any(
                not isinstance(word,str) or not word.strip() for word in keywords):
            raise ValueError('Добавь ключевые фразы для ответов.')
        options={**options,'fetchKeywords':keywords}
        validate_task_options(options)
    if mode=='fetch':
        options['fetchKeywords']=data.get('fetch_keywords') or []
        options.update(fetchCount=data.get('fetch_count') if data.get('fetch_count') in (100,1000) else 100,collectAll=False,includeReplies=bool(data.get('fetch_replies',False)))
    if mode=='feedwarm':payload['feedWarm']=video_search_settings(data.get('warm_json'),account_count=len(account_ids))
    options.update(delaySeconds=2,uniqueText=False)
    options.pop('accountsPerVideo',None)
    payload['settings']=options
    # Texts are sent verbatim; AI rewriting is disabled by the order policy.
    return payload
