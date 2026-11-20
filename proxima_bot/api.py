from __future__ import annotations
import asyncio
import contextvars
import logging
from html import unescape
import json
import re
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from urllib.parse import quote
import aiohttp
from .accounts import ACCOUNT_FOLDER_ID, valid_account_query_folder, valid_task_folder
from .operations import VALID_ID

log = logging.getLogger(__name__)

# Per-update target for in-place editing: while a button press is handled, the first
# plain/rich message to the same chat edits the pressed message instead of sending a new one.
_edit_target: contextvars.ContextVar[dict|None] = contextvars.ContextVar('edit_target', default=None)
MAX_JSON_BYTES=8*1024*1024
ACCOUNT_PAGE_SIZE=200
TOKORA_REQUESTS_PER_MINUTE = 110
TOKORA_BACKGROUND_REQUESTS_PER_MINUTE = 90
TOKORA_ADMISSION_WAIT = 5
VIDEO_RESOLUTION_CACHE_SIZE = 512
VIDEO_RESOLUTION_CACHE_TTL = 600
REQUEST_ID_PATTERN = re.compile(r'[A-Za-z0-9_.:-]{1,128}')
TASK_STATE_MAP = {
    'running':'running', 'queued':'running', 'pending':'running', 'starting':'running', 'stopping':'stopping',
    'finished':'completed', 'completed':'completed', 'complete':'completed', 'done':'completed',
    'success':'completed', 'completed_with_errors':'completed', 'stopped':'stopped',
    'failed':'failed', 'error':'failed', 'interrupted':'failed',
    'canceled':'canceled', 'cancelled':'canceled', 'paused':'paused',
}


def task_finish_pending(snapshot:dict)->bool:
    state=snapshot.get('state')
    finished=snapshot.get('finishedAt')
    return (isinstance(state,str) and TASK_STATE_MAP.get(state.strip().lower()) in
            ('completed','stopped','canceled','failed') and not (isinstance(finished,str) and finished.strip()))


async def read_json(response:aiohttp.ClientResponse):
    length=response.headers.get('Content-Length','')
    if length.isdecimal() and int(length)>MAX_JSON_BYTES:return None
    content=bytearray()
    async for chunk in response.content.iter_chunked(65536):
        if len(content)+len(chunk)>MAX_JSON_BYTES:return None
        content.extend(chunk)
    try:return json.loads(content)
    except (ValueError,RecursionError):return None


def _validate_account_page(page:dict,seen_pages:set[tuple[str,...]])->list[dict]:
    items=page.get('accounts') if isinstance(page,dict) else None
    if not isinstance(items,list) or any(not isinstance(item,dict) or not isinstance(item.get('accountId'),str)
                                         for item in items):
        raise ApiError('invalid_response')
    if len(items)>ACCOUNT_PAGE_SIZE:raise ApiError('invalid_response')
    page_ids=tuple(item['accountId'] for item in items)
    if page_ids in seen_pages:raise ApiError('invalid_response')
    seen_pages.add(page_ids)
    return items


async def _fetch_missing_accounts(api,missing:set[str],concurrency:int=5)->dict[str,dict]:
    """Fetch a bounded set of accounts directly by ID concurrently."""
    valid_ids=[aid for aid in sorted(missing) if VALID_ID.fullmatch(aid)]
    if not valid_ids or not callable(getattr(api,'call',None)):
        return {}
    sem=asyncio.Semaphore(concurrency)

    async def fetch_one(aid:str)->tuple[str,dict]|None:
        async with sem:
            try:
                account=await api.call('GET','/v1/accounts/'+aid,retries=0)
            except ApiError as exc:
                if exc.status==404:
                    return None
                raise
            if not isinstance(account,dict) or account.get('accountId')!=aid:
                raise ApiError('invalid_response')
            return aid,account

    outcomes=await asyncio.gather(*(fetch_one(aid) for aid in valid_ids),return_exceptions=True)
    fetched={}
    for outcome in outcomes:
        if isinstance(outcome,BaseException):
            raise outcome
        if outcome is not None:
            aid,account=outcome
            fetched[aid]=account
    return fetched


async def lookup_accounts(api,ids:list[str])->dict[str,dict]:
    """Resolve assigned IDs from documented account pages without treating API errors as missing."""
    wanted=set(ids)
    found={}
    seen_pages=set()
    offset=0
    batch_size=1
    total=None
    while wanted-found.keys():
        if batch_size>1 and type(total) is int and offset<total:
            end=min(total,2000) if offset<2000 else total
            offsets=list(range(offset,end,ACCOUNT_PAGE_SIZE))[:batch_size] or [offset]
            outcomes=await asyncio.gather(*(api.account_page(off) for off in offsets),return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome,BaseException):
                    raise outcome
            pages=list(outcomes)
        else:
            pages=[await api.account_page(offset)]
        stop=False
        last_len=0
        for page in pages:
            items=_validate_account_page(page,seen_pages)
            found.update((item['accountId'],item) for item in items if item['accountId'] in wanted)
            offset+=len(items)
            last_len=len(items)
            page_total=page.get('total')
            if type(page_total) is int:
                total=page_total
                if offset>=total:
                    stop=True
                    break
                if not items:
                    raise ApiError('invalid_response')
            else:
                total=None
                if len(items)<ACCOUNT_PAGE_SIZE:
                    stop=True
                    break
            if not (wanted-found.keys()):
                stop=True
                break
        missing=wanted-found.keys()
        if stop or not missing:
            break
        if 0<len(missing)<=10:
            if offset>=2000:
                found.update(await _fetch_missing_accounts(api,missing))
                break
            if (type(total) is int and total>offset and last_len==ACCOUNT_PAGE_SIZE
                    and callable(getattr(api,'call',None))
                    and all(VALID_ID.fullmatch(aid) for aid in missing)):
                rem_pages=(total-offset+ACCOUNT_PAGE_SIZE-1)//ACCOUNT_PAGE_SIZE
                if len(missing)<=rem_pages:
                    found.update(await _fetch_missing_accounts(api,missing))
                    break
        batch_size=4 if (type(total) is int and offset<total and last_len==ACCOUNT_PAGE_SIZE) else 1
    return found


async def scan_active_tasks(api, *, first_only:bool=False, retries:int=1) -> list[dict]:
    """Tokora may include terminal records even when active=true."""
    offset = 0
    active = []
    seen = set()
    while True:
        params = {'active':'true','limit':100}
        if offset:
            params['offset'] = offset
        page = await api.call('GET','/v1/tasks',params=params,retries=retries)
        items = Tokora.task_items(page)
        more = page.get('more',False)
        if type(more) is not bool or page.get('offset',offset)!=offset or (more and not items):
            raise ApiError('invalid_response')
        for item in items:
            if item['id'] in seen:
                raise ApiError('invalid_response')
            seen.add(item['id'])
            state = item.get('state')
            normalized = TASK_STATE_MAP.get(state.strip().lower()) if isinstance(state,str) else None
            if normalized not in ('completed','stopped','canceled','failed') or task_finish_pending(item):
                active.append(item)
                if first_only:
                    return active
        if not more:
            return active
        offset += len(items)


class ApiError(Exception):
    def __init__(self, code:str, status:int=0, retry_after:float=0, *, request_id:str='', rate_scope:str='',
                 provider_message:str='', required_scope:str='', from_provider:bool=False):
        self.code,self.status,self.retry_after=code,status,retry_after
        self.request_id = request_id
        self.rate_scope = rate_scope
        self.provider_message = provider_message
        self.required_scope = required_scope
        self.from_provider = from_provider
        super().__init__(f'Upstream {code} ({status})')


class Tokora:
    def __init__(self, session:aiohttp.ClientSession, base_url:str, key:str, timeout:int=25,
                 *, admit_shared: Callable[[], None] | None = None):
        self.session,self.base,self.key,self.timeout=session,base_url.rstrip('/'),key,timeout
        self.rate_limit_until=0.0
        self.rate_limit_scope=''
        self._rate_limit_request_id = ''
        self.last_error_request_id = ''
        self._request_times: deque[float] = deque()
        self._admission_lock = asyncio.Lock()
        self._admit_shared = admit_shared
        self._resolved_videos: OrderedDict[str, tuple[str, float]] = OrderedDict()

    async def reserve_request(self, *, background: bool=False) -> None:
        """Share a rolling minute budget across JSON calls, uploads and downloads."""
        budget = min(TOKORA_REQUESTS_PER_MINUTE, TOKORA_BACKGROUND_REQUESTS_PER_MINUTE) if background else TOKORA_REQUESTS_PER_MINUTE
        deadline = time.monotonic() + (0 if background else TOKORA_ADMISSION_WAIT)
        async with self._admission_lock:
            while True:
                now = time.monotonic()
                if self.rate_limit_until > now:
                    raise ApiError('rate_limited', 429, self.rate_limit_until - now,
                                   request_id=self._rate_limit_request_id, rate_scope=self.rate_limit_scope)
                while self._request_times and now - self._request_times[0] >= 60:
                    self._request_times.popleft()
                if len(self._request_times) < budget:
                    if self._admit_shared is not None:
                        self._admit_shared()
                    self._request_times.append(now)
                    return
                wait = self._request_times[len(self._request_times)-budget] + 60 - now
                if now + wait > deadline:
                    raise ApiError('rate_limited', 429, wait, rate_scope='key')
                await asyncio.sleep(wait)

    def response_error(self, response: aiohttp.ClientResponse, body, fallback: str) -> ApiError:
        body = body if isinstance(body,dict) else {}
        error = body.get('error')
        error = error if isinstance(error, dict) else {}
        candidate_code=error.get('code') or body.get('code') or body.get('error')
        code=candidate_code if (isinstance(candidate_code,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',candidate_code)
                                and not (self.key and self.key in candidate_code)) else fallback
        message=error.get('message') or body.get('message')
        if not isinstance(message,str):
            message=candidate_code if isinstance(candidate_code,str) and candidate_code!=code else ''
        if self.key:message=message.replace(self.key,'[ключ скрыт]')
        scope=body.get('scope') or error.get('scope')
        scope=scope if isinstance(scope,str) and re.fullmatch(r'[a-z0-9_]{1,32}:[a-z0-9_]{1,32}',scope) else ''
        request_id = ''
        for candidate in (error.get('requestId'), body.get('requestId'), response.headers.get('X-Request-Id')):
            if (isinstance(candidate,str) and REQUEST_ID_PATTERN.fullmatch(candidate)
                    and not (self.key and self.key in candidate)):
                request_id = candidate
                self.last_error_request_id = request_id
                log.warning('Tokora HTTP %d, request_id=%s', response.status, request_id)
                break
        retry_after = response.headers.get('Retry-After', '')
        delay = max(1, float(retry_after)) if re.fullmatch(r'[0-9]{1,8}(?:\.[0-9]{1,3})?', retry_after) else 3.0
        rate_scope = body.get('limit')
        if rate_scope not in ('key', 'account', 'ip'):
            rate_scope = error.get('limit')
        rate_scope = rate_scope if rate_scope in ('key', 'account', 'ip') else ''
        if response.status == 429:
            until = time.monotonic() + delay
            if until >= self.rate_limit_until:
                self.rate_limit_until = until
                self._rate_limit_request_id = request_id
                self.rate_limit_scope = rate_scope
        return ApiError(code, response.status, delay, request_id=request_id,
                        rate_scope=rate_scope, provider_message=message[:4000],required_scope=scope,
                        from_provider=True)

    async def call(self, method:str, path:str, *, payload:dict|None=None, params:dict|None=None,
                   retries:int|None=None, background:bool=False) -> dict:
        if not path.startswith('/v1/') or '://' in path or '..' in path:
            raise ValueError('Unsafe API path')
        # A second failure must not hide an ambiguous outcome from an earlier write.
        if retries is None:
            retries = 2 if method.upper()=='GET' else 0
        for attempt in range(retries+1):
            await self.reserve_request(background=background)
            try:
                async with self.session.request(method,self.base+path,params=params,json=payload,
                      headers={'Authorization':'Bearer '+self.key,'Accept':'application/json'},
                      timeout=aiohttp.ClientTimeout(total=self.timeout),allow_redirects=False) as resp:
                    # DELETE / stop operations may legitimately return HTTP 204.
                    # Do not assume every successful provider response has a JSON body.
                    if resp.status==204:
                        return {}
                    raw=await read_json(resp)
                    if 200<=resp.status<300:
                        if not isinstance(raw,dict):
                            raise self.response_error(resp, None, 'invalid_response')
                        return raw
                    error = self.response_error(resp, raw, 'upstream_error')
                    log.warning('Tokora %s %s HTTP %d code=%s request_id=%s',method.upper(),path,resp.status,error.code,error.request_id)
                    if resp.status==503 and error.retry_after<=30 and attempt<retries:
                        await asyncio.sleep(error.retry_after)
                        continue
                    raise error
            except (aiohttp.ClientError,asyncio.TimeoutError) as exc:
                if attempt<retries:
                    await asyncio.sleep(1+attempt)
                    continue
                raise ApiError('connection_unavailable') from exc
        raise ApiError('connection_unavailable')

    async def resolve(self, link:str)->str:
        cached = self._resolved_videos.get(link)
        if cached and cached[1] > time.monotonic():
            self._resolved_videos.move_to_end(link)
            return cached[0]
        result=await self.call('POST','/v1/links/resolve',payload={'link':link,'kind':'video'},retries=1)
        video=result.get('videoId')
        if not isinstance(video,str) or not re.fullmatch(r'[0-9]{10,30}',video):
            raise ApiError('video_not_resolved')
        # Retain successful resolutions so a rate-limited order can retry its remaining links.
        self._resolved_videos[link] = (video, time.monotonic() + VIDEO_RESOLUTION_CACHE_TTL)
        self._resolved_videos.move_to_end(link)
        if len(self._resolved_videos) > VIDEO_RESOLUTION_CACHE_SIZE:
            self._resolved_videos.popitem(last=False)
        return video

    async def start(self,payload:dict)->dict:
        # The queue owns reconciliation of the persisted requestId.
        folder = payload.get('accountFolder')
        if not valid_task_folder(folder, allow_workspace=True):
            raise ApiError('account_folder_required',400)
        body = dict(payload)
        assets = {key:value for key,value in payload.get('assets',{}).items() if key != 'accountIds'}
        if assets:
            body['assets'] = assets
        else:
            body.pop('assets',None)
        return await self.call('POST','/v1/tasks',payload=body,retries=0)

    async def active_tasks(self)->list[dict]:
        return await scan_active_tasks(self)

    async def task_page(self,offset:int=0,limit:int=100)->dict:
        data=await self.call('GET','/v1/tasks',params={'offset':offset,'limit':limit},retries=0)
        self.task_items(data)
        if type(data.get('more')) is not bool:
            raise ApiError('invalid_response')
        return data

    @staticmethod
    def task_items(data:dict)->list[dict]:
        items=data.get('items')
        if not isinstance(items,list) or any(
                not isinstance(item,dict) or not isinstance(item.get('id'),str) or not item['id']
                for item in items):
            raise ApiError('invalid_response')
        return items

    async def task(self,upstream_id:str)->dict:
        return await self.call('GET','/v1/tasks/'+quote(upstream_id,safe=''),retries=1)

    async def publication(self,upstream_id:str)->dict:
        return await self.call('GET','/v1/tasks/'+quote(upstream_id,safe='')+'/publication-report',retries=1)

    async def stop(self,upstream_id:str)->dict:
        return await self.call('POST','/v1/tasks/'+quote(upstream_id,safe='')+'/stop',retries=0)

    async def verify(self,upstream_id:str)->dict:
        return await self.call('POST','/v1/tasks/'+quote(upstream_id,safe='')+'/verify',retries=0)

    async def account_page(self,offset:int=0,limit:int=ACCOUNT_PAGE_SIZE,*,status:str='',folder:str='')->dict:
        params={'limit':limit,'offset':offset}
        if status:params['status']=status
        if folder:params['folder']=folder
        return await self.call('GET','/v1/accounts',params=params,retries=1)

    async def task_account_page(self,task_id:str,offset:int=0,limit:int=15)->dict:
        if not VALID_ID.fullmatch(task_id):raise ValueError('Invalid task ID')
        page=await self.call('GET','/v1/accounts',params={'task':task_id,'offset':offset,'limit':limit})
        accounts,total=page.get('accounts'),page.get('total')
        if (type(total) is not int or total<0 or not isinstance(accounts,list) or len(accounts)>limit
                or len(accounts)>max(0,total-offset) or (offset<total and not accounts)):
            raise ApiError('invalid_response')
        ids=set()
        for item in accounts:
            aid=item.get('accountId') if isinstance(item,dict) else None
            if not isinstance(aid,str) or not VALID_ID.fullmatch(aid) or aid in ids:
                raise ApiError('invalid_response')
            ids.add(aid)
        return page

    async def accounts_for(self,ids:list[str])->dict[str,dict]:
        return await lookup_accounts(self,ids)

    async def accounts_in_folder(self,folder:str,*,expected_count:int|None=None)->dict[str,dict]:
        if not valid_account_query_folder(folder):
            raise ValueError('Invalid account folder ID')
        query_folder = '' if folder in ('', 'all') else folder
        accounts = {}
        snapshot_total = None
        while True:
            offset = len(accounts)
            page = await (self.account_page(offset,folder=query_folder) if query_folder else self.account_page(offset))
            items,total = page.get('accounts'),page.get('total')
            if type(total) is not int or total < 0 or not isinstance(items,list):
                raise ApiError('invalid_response')
            if expected_count is not None and total != expected_count:
                return {}
            if snapshot_total is not None and total != snapshot_total:
                raise ApiError('invalid_response')
            snapshot_total = total
            if (len(items)>ACCOUNT_PAGE_SIZE or offset+len(items)>total or (offset<total and not items)):
                raise ApiError('invalid_response')
            for item in items:
                aid = item.get('accountId') if isinstance(item,dict) else None
                if not isinstance(aid,str) or not VALID_ID.fullmatch(aid) or aid in accounts:
                    raise ApiError('invalid_response')
                item_folder = item.get('folderId')
                if folder == 'none':
                    if item_folder not in (None, ''):
                        raise ApiError('invalid_response')
                elif query_folder and item_folder != query_folder:
                    raise ApiError('invalid_response')
                accounts[aid] = item
            if len(accounts)==total:
                return accounts

    async def stats(self)->dict:
        return await self.call('GET','/v1/statistics',retries=1)

    async def proxies(self)->dict:
        return await self.call('GET','/v1/proxies',retries=1)


    async def upload(self,path:str,data:bytes,content_type:str='application/octet-stream',*,params:dict|None=None)->dict:
        """Upload a bounded binary asset; no implicit mutation retry."""
        if not path.startswith('/v1/') or '..' in path or len(data)>22*1024*1024:
            raise ValueError('Invalid upload')
        await self.reserve_request()
        try:
            async with self.session.post(self.base+path,data=data,params=params,allow_redirects=False,headers={
                'Authorization':'Bearer '+self.key,'Content-Type':content_type,
                'Accept':'application/json'}, timeout=aiohttp.ClientTimeout(total=max(self.timeout,90))) as resp:
                result=await read_json(resp)
                if 200<=resp.status<300 and isinstance(result,dict):return result
                raise self.response_error(resp, result, 'upload_failed')
        except (aiohttp.ClientError,asyncio.TimeoutError) as exc:
            raise ApiError('connection_unavailable') from exc

    async def download(self,path:str,params:dict|None=None,max_bytes:int=15*1024*1024)->tuple[bytes,str]:
        if not path.startswith('/v1/') or '..' in path:raise ValueError('Invalid download path')
        await self.reserve_request()
        try:
            async with self.session.get(self.base+path,params=params,allow_redirects=False,headers={
                'Authorization':'Bearer '+self.key},timeout=aiohttp.ClientTimeout(total=max(self.timeout,90))) as resp:
                if not 200<=resp.status<300:
                    data=await read_json(resp)
                    raise self.response_error(resp, data, 'download_failed')
                length=resp.headers.get('Content-Length','')
                if length.isdecimal() and int(length)>max_bytes:raise ApiError('file_too_large')
                buffer=bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    buffer.extend(chunk)
                    if len(buffer)>max_bytes:raise ApiError('file_too_large')
                return bytes(buffer),resp.headers.get('Content-Type','application/octet-stream')
        except (aiohttp.ClientError,asyncio.TimeoutError) as exc:
            raise ApiError('connection_unavailable') from exc


class TelegramError(Exception):
    def __init__(self, code:int, description:str='', retry_after:int=0):
        self.code=code
        self.description=description
        self.retry_after=retry_after
        super().__init__(f'Telegram error {code}')


class Telegram:
    def __init__(self, session:aiohttp.ClientSession,token:str):
        self.session,self.base=session,'https://api.telegram.org/bot'+token+'/'
        self.edit_listeners:list=[]

    def edit_in_place(self,chat_id:int,message_id:int)->contextvars.Token:
        """Next send()/rich() to chat_id in this context edits message_id (once)."""
        return _edit_target.set({'chat':chat_id,'mid':message_id})

    def reset_edit(self,token:contextvars.Token)->None:
        _edit_target.reset(token)

    def _take_target(self,chat_id:int,keyboard:dict|None)->int|None:
        target=_edit_target.get()
        if not target or not target.get('mid') or target['chat']!=chat_id:return None
        # Reply keyboards cannot be attached to an edited message: send a new one instead.
        if keyboard and 'inline_keyboard' not in keyboard:return None
        mid=target['mid'];target['mid']=None
        return mid

    async def _try_edit(self,chat_id:int,mid:int,data:dict,*,rich:bool=False)->dict|None:
        try:
            result=await self.call('editMessageText',data,timeout=8)
        except TelegramError as exc:
            if 'not modified' not in exc.description.lower():
                if exc.code not in (400,404):raise
                # Old, media or otherwise non-editable message: caller sends a new one.
                return None
            result=None
        for listener in self.edit_listeners:
            listener(chat_id,mid)
        if isinstance(result,dict):return result
        fake={'message_id':mid,'chat':{'id':chat_id}}
        if rich:fake['rich_message']=True
        return fake

    async def call(self,method:str,data:dict,*,timeout:int=25)->dict:
        try:
            async with self.session.post(self.base+method,json=data,allow_redirects=False,
                   timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                raw=await read_json(response)
                if not isinstance(raw,dict):raise TelegramError(response.status if response.status>=400 else 502,'unparseable')
                if not raw.get('ok'):
                    raise TelegramError(int(raw.get('error_code',response.status)),str(raw.get('description','')),
                                        int((raw.get('parameters') or {}).get('retry_after',0)))
                return raw.get('result')
        except (aiohttp.ClientError,asyncio.TimeoutError):
            raise TelegramError(503,'Telegram connection unavailable') from None

    async def send(self,chat_id:int,text:str,keyboard:dict|None=None,*,html:bool=True)->dict:
        if len(text)>4090:
            visible=unescape(re.sub(r'<[^>]*>','',text)) if html else text
            if len(visible)>4090:
                text=visible[:4089]+'…'
                html=False
        data={'chat_id':chat_id,'text':text,'link_preview_options':{'is_disabled':True}}
        if html:data['parse_mode']='HTML'
        if keyboard:data['reply_markup']=keyboard
        mid=self._take_target(chat_id,keyboard)
        if mid:
            edited=await self._try_edit(chat_id,mid,{**data,'message_id':mid})
            if edited is not None:return edited
        return await self.call('sendMessage',data)

    async def edit(self,chat_id:int,message_id:int,text:str,keyboard:dict)->dict:
        return await self.call('editMessageText',{
            'chat_id':chat_id,'message_id':message_id,'text':text,
            'parse_mode':'HTML','link_preview_options':{'is_disabled':True},
            'reply_markup':keyboard},timeout=8)

    async def edit_rich(self,chat_id:int,message_id:int,rich_html:str,keyboard:dict)->dict:
        return await self.call('editMessageText',{
            'chat_id':chat_id,'message_id':message_id,
            'rich_message':{'html':rich_html,'skip_entity_detection':True},
            'reply_markup':keyboard},timeout=8)

    async def rich(self,chat_id:int,rich_html:str,plain_fallback:str,keyboard:dict|None=None,*,fallback_keyboard:dict|None=None)->dict:
        payload={'chat_id':chat_id,'rich_message':{'html':rich_html,'skip_entity_detection':True}}
        if keyboard:payload['reply_markup']=keyboard
        mid=self._take_target(chat_id,keyboard)
        if mid:
            edited=await self._try_edit(chat_id,mid,{**payload,'message_id':mid},rich=True)
            if edited is not None:return edited
            plain={'chat_id':chat_id,'message_id':mid,'text':plain_fallback,'parse_mode':'HTML',
                   'link_preview_options':{'is_disabled':True}}
            if fallback_keyboard or keyboard:plain['reply_markup']=fallback_keyboard or keyboard
            edited=await self._try_edit(chat_id,mid,plain)
            if edited is not None:return edited
        try:
            return await self.call('sendRichMessage',payload)
        except TelegramError as exc:
            # Unsupported/malformed rich markup: gracefully show safe basic Telegram HTML.
            if exc.code not in (400,404):raise
            log.warning('Rich Message fallback: HTTP %d',exc.code)
            return await self.send(chat_id,plain_fallback,fallback_keyboard or keyboard)

    async def answer_callback(self,callback_id:str,text:str='')->None:
        await self.call('answerCallbackQuery',{'callback_query_id':callback_id,'text':text[:180]})

    async def updates(self,offset:int|None)->list[dict]:
        payload={'timeout':30,'allowed_updates':['message','callback_query']}
        if offset is not None:payload['offset']=offset
        return await self.call('getUpdates',payload,timeout=35)

    async def attachment(self,chat_id:int,content:bytes,filename:str='export.txt',caption:str='')->dict:
        if not content or len(content)>20*1024*1024:raise ValueError('File too large')
        form=aiohttp.FormData()
        form.add_field('chat_id',str(chat_id))
        form.add_field('document',content,filename=filename,content_type='application/octet-stream')
        if caption:form.add_field('caption',caption[:1000])
        try:
            async with self.session.post(self.base+'sendDocument',data=form,allow_redirects=False,
                 timeout=aiohttp.ClientTimeout(total=90)) as resp:
                result=await read_json(resp)
                if not isinstance(result,dict):raise TelegramError(resp.status if resp.status>=400 else 502,'Invalid document response')
                if not result.get('ok'):
                    raise TelegramError(int(result.get('error_code',resp.status)),str(result.get('description','')),
                                        int((result.get('parameters') or {}).get('retry_after',0)))
                return result.get('result')
        except (aiohttp.ClientError,asyncio.TimeoutError):
            raise TelegramError(503,'Telegram document transfer failed') from None

    async def fetch_upload(self,file_id:str,max_bytes:int)->tuple[bytes,str]:
        if len(file_id)>200 or not file_id:raise ValueError('Bad Telegram file id')
        info=await self.call('getFile',{'file_id':file_id})
        if int(info.get('file_size') or 0)>max_bytes:raise ValueError('File exceeds limit')
        filename=str(info.get('file_path') or '')
        if any(segment in ('','.','..') for segment in filename.split('/')) or '\\' in filename:
            raise ValueError('Invalid Telegram file path')
        url=self.base.replace('/bot','/file/bot',1)+quote(filename,safe='/')
        try:
            async with self.session.get(url,allow_redirects=False,
                 timeout=aiohttp.ClientTimeout(total=90)) as resp:
                if resp.status!=200:raise TelegramError(resp.status,'File retrieval failed')
                buffer=bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    buffer.extend(chunk)
                    if len(buffer)>max_bytes:raise ValueError('File exceeds limit')
                return bytes(buffer),filename
        except (aiohttp.ClientError,asyncio.TimeoutError):
            raise TelegramError(503,'Telegram file transfer failed') from None
