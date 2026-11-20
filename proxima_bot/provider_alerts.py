"""Private diagnostics for failed cabinet requests and completed orders."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict

from .api import ApiError, Telegram, TelegramError
from .presentation import h


log = logging.getLogger(__name__)
REPEAT_SECONDS = 120
QUEUE_LIMIT = 100
URL = re.compile(r"""(?:https?|socks[45]h?)://[^\s<>"']+""",re.I)
TOKEN = re.compile(r'\b(?:tk_[A-Za-z0-9_-]{12,}|[0-9]{6,}:[A-Za-z0-9_-]{20,}|Bearer[ \t]+\S+)',re.I)
SENSITIVE_FIELD = re.compile(
    r'\b(?:set[-_ ]?cookie|cookies?|session[_-]?id|sid_guard|sid_tt|'
    r'authorization|(?:x[-_ ]?)?api[-_ ]?key|(?:access|refresh)[-_ ]?token|'
    r'token|password|passwd|secret|proxy[-_ ]?(?:user(?:name)?|pass(?:word)?))'
    r"""["']?\s*[:=]""", re.I)
PROXY_LOGIN = re.compile(r'\b[A-Za-z0-9.-]+:[0-9]{1,5}:[^:\s]+:[^\s]+')


def diagnostic_text(value:object, limit:int=1000)->str:
    if not isinstance(value,str):return ''
    if match := SENSITIVE_FIELD.search(value):
        value = value[:match.start()]+'[реквизиты скрыты]'
    value = URL.sub('[ссылка скрыта]',value)
    value = TOKEN.sub('[токен скрыт]',value)
    cleaned = PROXY_LOGIN.sub('[прокси скрыт]',value).strip()
    escaped = h(cleaned)
    return escaped if len(escaped)<=limit else h(cleaned[:limit//6])+'…'


class ProviderAlerts:
    def __init__(self, tg:Telegram, owner_id:int):
        self.tg = tg
        self.owner_id = owner_id
        self.pending:asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self.recent:OrderedDict[tuple,float] = OrderedDict()

    def offer(self, key:tuple, message:str)->None:
        now = time.monotonic()
        while self.recent and now-next(iter(self.recent.values()))>=REPEAT_SECONDS:
            self.recent.popitem(last=False)
        if key in self.recent or self.pending.full():return
        # Bound memory even if many distinct requests fail during the cooldown.
        if len(self.recent)>=QUEUE_LIMIT*4:self.recent.popitem(last=False)
        self.recent[key] = now
        self.pending.put_nowait(message)

    def request(self, cabinet:str, method:str, path:str, error:ApiError)->None:
        if not error.from_provider and error.code!='connection_unavailable':return
        operation = diagnostic_text(' '.join(filter(None,(method,path))),420)
        detail = diagnostic_text(error.provider_message,1500)
        code = diagnostic_text(error.code,128)
        rows = ['<b>Ошибка API Tokora</b>', 'Кабинет · '+h(cabinet),
                'Операция · <code>'+operation+'</code>', 'Код · <code>'+code+'</code>']
        if error.status:rows.append('HTTP · '+str(error.status))
        if request_id:=diagnostic_text(error.request_id,200):
            rows.append('ID запроса · <code>'+request_id+'</code>')
        if scope:=diagnostic_text(error.required_scope,100):
            rows.append('Нужно право · <code>'+scope+'</code>')
        if error.status==429 and error.rate_scope:
            rows.append('Лимит · <code>'+h(error.rate_scope)+'</code>')
        if detail:rows.append('\n'+detail)
        self.offer((cabinet,operation,error.status,code,detail,error.required_scope,error.rate_scope),'\n'.join(rows))

    def task(self, cabinet:str, task_id:str, snapshot:dict, previous_snapshot:dict|None=None)->None:
        if previous_snapshot and all(snapshot.get(key)==previous_snapshot.get(key)
                                     for key in ('state','error','errorCounts','failed')):
            return
        detail = diagnostic_text(snapshot.get('error'),900)
        codes = []
        breakdown = snapshot.get('errorCounts')
        if isinstance(breakdown,list):
            for item in breakdown:
                if not isinstance(item,dict):continue
                code = diagnostic_text(item.get('code'),100)
                count = item.get('count')
                if code and type(count) is int and 0<count<=2**63-1:
                    codes.append(f'<code>{code}</code> · {count}')
        failed = snapshot.get('failed')
        failed = failed if type(failed) is int and 0<failed<=2**63-1 else 0
        raw_state = snapshot.get('state')
        state = raw_state.strip().lower() if isinstance(raw_state,str) else ''
        if not (detail or codes or failed or state in ('failed','error','interrupted','completed_with_errors')):
            return
        rows = ['<b>Ошибки выполнения Tokora</b>', 'Кабинет · '+h(cabinet),
                'Заказ · <code>'+h(task_id)+'</code>']
        if upstream:=diagnostic_text(snapshot.get('id'),180):
            rows.append('ID выполнения · <code>'+upstream+'</code>')
        if detail:rows.append(detail)
        rows.extend(codes[:15])
        if len(codes)>15:rows.append(f'Других кодов · {len(codes)-15}')
        if not codes and failed:rows.append(f'Ошибок · {failed}')
        rendered = '\n'.join(rows)
        self.offer((cabinet,task_id,rendered),rendered)

    async def run(self)->None:
        while True:
            message = await self.pending.get()
            for attempt in range(2):
                try:
                    await self.tg.send(self.owner_id,message)
                    break
                except TelegramError as error:
                    if not attempt and (error.code==429 or error.code>=500) and error.retry_after<=60:
                        await asyncio.sleep(max(1,error.retry_after))
                        continue
                    log.warning('Provider alert delivery failed: HTTP %d',error.code)
                    break
            await asyncio.sleep(1)
