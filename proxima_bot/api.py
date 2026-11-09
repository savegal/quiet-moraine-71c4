from __future__ import annotations
import asyncio
import contextvars
import logging
from html import unescape
import json
import re
import time
from urllib.parse import quote
import aiohttp
from .presentation import days_label, number

log = logging.getLogger(__name__)

# Per-update target for in-place editing: while a button press is handled, the first
# plain/rich message to the same chat edits the pressed message instead of sending a new one.
_edit_target: contextvars.ContextVar[dict|None] = contextvars.ContextVar('edit_target', default=None)
MAX_JSON_BYTES=8*1024*1024


async def read_json(response:aiohttp.ClientResponse):
    length=response.headers.get('Content-Length','')
    if length.isdecimal() and int(length)>MAX_JSON_BYTES:return None
    content=bytearray()
    async for chunk in response.content.iter_chunked(65536):
        if len(content)+len(chunk)>MAX_JSON_BYTES:return None
        content.extend(chunk)
    try:return json.loads(content)
    except (ValueError,RecursionError):return None


class ApiError(Exception):
    def __init__(self, code:str, status:int=0, retry_after:float=0):
        self.code,self.status,self.retry_after=code,status,retry_after
        super().__init__(f'Upstream {code} ({status})')


class Tokora:
    def __init__(self, session:aiohttp.ClientSession, base_url:str, key:str, timeout:int=25):
        self.session,self.base,self.key,self.timeout=session,base_url.rstrip('/'),key,timeout
        self.rate_limit_until=0.0

    def check_rate_limit(self)->None:
        remaining=self.rate_limit_until-time.monotonic()
        if remaining>0:raise ApiError('rate_limited',429,remaining)

    def retry_delay(self,status:int,header:str)->float:
        delay=max(1,float(header)) if re.fullmatch(r'[0-9]{1,8}(?:\.[0-9]{1,3})?',header) else 3.0
        if status==429:self.rate_limit_until=max(self.rate_limit_until,time.monotonic()+delay)
        return delay

    async def call(self, method:str, path:str, *, payload:dict|None=None, params:dict|None=None, retries:int|None=None) -> dict:
        if not path.startswith('/v1/') or '://' in path or '..' in path:
            raise ValueError('Unsafe API path')
        # A second failure must not hide an ambiguous outcome from an earlier write.
        if retries is None:
            retries = 2 if method.upper()=='GET' else 0
        for attempt in range(retries+1):
            self.check_rate_limit()
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
                            raise ApiError('invalid_response',resp.status)
                        return raw
                    error=raw.get('error') if isinstance(raw,dict) else None
                    code=error.get('code','upstream_error') if isinstance(error,dict) else 'upstream_error'
                    retry_after=self.retry_delay(resp.status,resp.headers.get('Retry-After',''))
                    if resp.status==503 and retry_after<=30 and attempt<retries:
                        await asyncio.sleep(retry_after)
                        continue
                    raise ApiError(str(code),resp.status,retry_after)
            except (aiohttp.ClientError,asyncio.TimeoutError) as exc:
                if attempt<retries:
                    await asyncio.sleep(1+attempt)
                    continue
                raise ApiError('connection_unavailable') from exc
        raise ApiError('connection_unavailable')

    async def resolve(self, link:str)->str:
        result=await self.call('POST','/v1/links/resolve',payload={'link':link,'kind':'video'},retries=1)
        video=result.get('videoId')
        if not isinstance(video,str) or not re.fullmatch(r'[0-9]{10,30}',video):
            raise ApiError('video_not_resolved')
        return video

    async def start(self,payload:dict)->dict:
        # The queue owns reconciliation of the persisted requestId.
        return await self.call('POST','/v1/tasks',payload=payload,retries=0)

    async def active_tasks(self)->list[dict]:
        data=await self.call('GET','/v1/tasks',params={'active':'true','limit':100},retries=1)
        return self.task_items(data)

    async def task_page(self,offset:int=0,limit:int=100)->dict:
        data=await self.call('GET','/v1/tasks',params={'offset':offset,'limit':limit},retries=0)
        self.task_items(data)
        if type(data.get('more')) is not bool:
            raise ApiError('invalid_response')
        return data

    @staticmethod
    def task_items(data:dict)->list[dict]:
        items=data.get('items')
        if not isinstance(items,list) or any(not isinstance(item,dict) or not item.get('id') for item in items):
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

    async def account_page(self,offset:int=0,limit:int=200)->dict:
        return await self.call('GET','/v1/accounts',params={'limit':limit,'offset':offset},retries=1)

    async def stats(self)->dict:
        return await self.call('GET','/v1/statistics',retries=1)

    async def proxies(self)->dict:
        return await self.call('GET','/v1/proxies',retries=1)


    async def upload(self,path:str,data:bytes,content_type:str='application/octet-stream',*,params:dict|None=None)->dict:
        """Upload a bounded binary asset; no implicit mutation retry."""
        if not path.startswith('/v1/') or '..' in path or len(data)>22*1024*1024:
            raise ValueError('Invalid upload')
        self.check_rate_limit()
        try:
            async with self.session.post(self.base+path,data=data,params=params,allow_redirects=False,headers={
                'Authorization':'Bearer '+self.key,'Content-Type':content_type,
                'Accept':'application/json'}, timeout=aiohttp.ClientTimeout(total=max(self.timeout,90))) as resp:
                result=await read_json(resp)
                if 200<=resp.status<300 and isinstance(result,dict):return result
                error=result.get('error') if isinstance(result,dict) else None
                delay=self.retry_delay(resp.status,resp.headers.get('Retry-After',''))
                raise ApiError(str(error.get('code') or 'upload_failed') if isinstance(error,dict) else 'upload_failed',
                               resp.status,delay)
        except (aiohttp.ClientError,asyncio.TimeoutError) as exc:
            raise ApiError('connection_unavailable') from exc

    async def download(self,path:str,params:dict|None=None,max_bytes:int=15*1024*1024)->tuple[bytes,str]:
        if not path.startswith('/v1/') or '..' in path:raise ValueError('Invalid download path')
        self.check_rate_limit()
        try:
            async with self.session.get(self.base+path,params=params,allow_redirects=False,headers={
                'Authorization':'Bearer '+self.key},timeout=aiohttp.ClientTimeout(total=max(self.timeout,90))) as resp:
                if not 200<=resp.status<300:
                    data=await read_json(resp)
                    error=data.get('error') if isinstance(data,dict) else None
                    delay=self.retry_delay(resp.status,resp.headers.get('Retry-After',''))
                    raise ApiError(str(error.get('code') or 'download_failed') if isinstance(error,dict) else
                                   'download_failed',resp.status,delay)
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

    async def rich(self,chat_id:int,rich_html:str,plain_fallback:str,keyboard:dict|None=None)->dict:
        payload={'chat_id':chat_id,'rich_message':{'html':rich_html,'skip_entity_detection':True}}
        if keyboard:payload['reply_markup']=keyboard
        mid=self._take_target(chat_id,keyboard)
        if mid:
            edited=await self._try_edit(chat_id,mid,{**payload,'message_id':mid},rich=True)
            if edited is not None:return edited
            plain={'chat_id':chat_id,'message_id':mid,'text':plain_fallback,'parse_mode':'HTML',
                   'link_preview_options':{'is_disabled':True}}
            if keyboard:plain['reply_markup']=keyboard
            edited=await self._try_edit(chat_id,mid,plain)
            if edited is not None:return edited
        try:
            return await self.call('sendRichMessage',payload)
        except TelegramError as exc:
            # Unsupported/malformed rich markup: gracefully show safe basic Telegram HTML.
            if exc.code not in (400,404):raise
            log.warning('Rich Message fallback: HTTP %d',exc.code)
            return await self.send(chat_id,plain_fallback,keyboard)

    async def answer_callback(self,callback_id:str,text:str='')->None:
        await self.call('answerCallbackQuery',{'callback_query_id':callback_id,'text':text[:180]})

    async def updates(self,offset:int|None)->list[dict]:
        payload={'timeout':30,'allowed_updates':['message','callback_query','pre_checkout_query']}
        if offset is not None:payload['offset']=offset
        return await self.call('getUpdates',payload,timeout=35)

    async def invoice(self,chat_id:int,payload:str,stars:int,days:int)->None:
        term=days_label(days)
        await self.call('sendInvoice',{'chat_id':chat_id,'title':f'◇ Подписка · {term}',
         'description':f'Доступ к инструментам Proxima Soft на {term}',
         'payload':payload,'currency':'XTR', 'provider_token':'',
         'prices':[{'label':f'Доступ на {term}','amount':stars}],
         'reply_markup':{'inline_keyboard':[[{'text':f'✓ Оплатить · {number(stars)} Stars',
                                               'style':'success','pay':True}]]}})

    async def precheckout(self,query_id:str,ok:bool,reason:str='')->None:
        data={'pre_checkout_query_id':query_id,'ok':ok}
        if not ok:data['error_message']=reason[:180] or 'Не удалось проверить оплату.'
        await self.call('answerPreCheckoutQuery',data,timeout=4)

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
                    raise TelegramError(int(result.get('error_code',resp.status)),str(result.get('description','')))
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
