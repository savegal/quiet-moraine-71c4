from __future__ import annotations
import asyncio
import json
import logging
import sqlite3
import time
from .api import ApiError,Tokora,Telegram
from .catalog import validate_feedwarm, validate_task_options
from .db import RECONCILE_HOLD_CODES, TERMINAL_STATES, Store, remote_task_name
from .config import Config
from .operations import VALID_ID
from .presentation import h

log=logging.getLogger(__name__)
PROVIDER_STATES={
    'finished':'completed','completed':'completed','complete':'completed','done':'completed','success':'completed',
    'stopped':'stopped','failed':'failed','error':'failed','canceled':'canceled','cancelled':'canceled','paused':'paused',
}
BUSY={'job_busy','rate_limited'}
UNCERTAIN={'service_unavailable','connection_unavailable','invalid_response'}
AUTH_ERRORS={'subscription_required','api_scope_denied','api_key_invalid'}
RECONCILE_POLL_SECONDS = 30
RECONCILE_PAGE_SIZE = 100
RECONCILE_PAGE_LIMIT = 3
RECONCILE_RETRY_LIMIT = 3


def snapshot_state(snapshot: dict, previous: str='running') -> str | None:
    raw_state = snapshot.get('state')
    if not isinstance(raw_state,str) or not raw_state:
        return None
    state = PROVIDER_STATES.get(raw_state.lower(),'running')
    if state not in TERMINAL_STATES and (previous=='stopping' or snapshot.get('stopRequested') is True):
        return 'stopping'
    return state


def marked_task_name(job: dict) -> str | None:
    payload = json.loads(job['payload_json'])
    name = remote_task_name(job['request_id'],job['display_name'])
    if payload.get('requestId')==job['request_id'] and payload.get('name')==name:
        return name
    return None


def matches_reconciliation(job: dict, snapshot: dict) -> bool:
    name = marked_task_name(job)
    upstream_id = snapshot.get('id')
    return bool(name and snapshot.get('name')==name and snapshot.get('kind')==job['kind']
                and isinstance(upstream_id,str) and VALID_ID.fullmatch(upstream_id)
                and (not job['upstream_id'] or job['upstream_id']==upstream_id))


class Worker:
    def __init__(self,store:Store,tokora:Tokora,tg:Telegram,config:Config):
        self.store,self.tokora,self.tg,self.config=store,tokora,tg,config
        self.warned:dict[str,float]={}
        self.next_dispatch_at=0.0
        self.next_reconcile_at=0.0

    async def alert(self, code:str,task_id:str='')->None:
        timestamp=time.monotonic()
        if timestamp-self.warned.get(code,float('-inf'))<1800:return
        self.warned[code]=timestamp
        text='<b>! Proxima Soft · API</b>\nНужна проверка: <code>'+h(code[:100])+'</code>'
        if task_id:text+='\nЗаказ: <code>'+h(task_id)+'</code> · /admin'
        try:await asyncio.wait_for(self.tg.send(self.config.owner_id,text),timeout=5)
        except Exception:log.exception('Failed to deliver admin alert')

    async def refresh(self)->None:
        for job in self.store.running():
            if job['upstream_id']:
                await self.refresh_job(job)

    async def refresh_job(self, job:dict)->None:
        try:
            result=await self.tokora.task(job['upstream_id'])
            state=snapshot_state(result,job['state'])
            if result.get('id')!=job['upstream_id'] or not state:
                raise ApiError('invalid_response')
            self.store.transition(job['task_id'],state,snapshot=result,
                error_code='execution_failed' if state=='failed' else '',expected_state=job['state'])
        except ApiError as exc:
            if exc.status==404 or exc.code in ('job_not_found','not_found'):
                if job['state']!='reconcile':
                    self.store.transition(job['task_id'],'reconcile',error_code='job_not_found',expected_state=job['state'])
                await self.alert('job_not_found',job['task_id'])
            else:
                log.warning('Refresh upstream job failed: %s',exc.code)

    async def reconcile(self)->None:
        if time.monotonic()<self.next_reconcile_at:return
        jobs=self.store.unresolved_tasks()
        if not jobs:return
        self.next_reconcile_at=time.monotonic()+RECONCILE_POLL_SECONDS

        pending=[]
        for job in jobs:
            if job['upstream_id']:
                await self.refresh_job(job)
                continue
            if (not self.store.is_active(job['user_id'],self.config.owner_id)
                    and job['error_code'] not in RECONCILE_HOLD_CODES):
                self.store.transition(job['task_id'],'reconcile',error_code='reconcile_access_revoked',
                                      expected_state='reconcile')
                job['error_code']='reconcile_access_revoked'
            pending.append(job)

        # The read API has no requestId filter. Match only our persisted unique name,
        # and keep an unmatched task unresolved even if the recent pages are empty.
        by_name:dict[str,list[dict]]={}
        names={name for job in pending if (name:=marked_task_name(job))}
        if names:
            for page_number in range(RECONCILE_PAGE_LIMIT):
                try:
                    page=await self.tokora.task_page(offset=page_number*RECONCILE_PAGE_SIZE,limit=RECONCILE_PAGE_SIZE)
                except ApiError as exc:
                    log.warning('Read-only task reconciliation failed: %s',exc.code)
                    break
                for result in page['items']:
                    name=result.get('name')
                    if isinstance(name,str) and name in names:
                        by_name.setdefault(name,[]).append(result)
                if not page['more']:
                    break

        for job in pending:
            candidates={result['id']:result for result in by_name.get(marked_task_name(job),[])
                        if matches_reconciliation(job,result)}
            if len(candidates)==1:
                result=next(iter(candidates.values()))
                state=snapshot_state(result)
                if state:
                    try:
                        if self.store.transition(job['task_id'],state,upstream_id=result['id'],snapshot=result,
                                error_code='execution_failed' if state=='failed' else '',expected_state='reconcile'):
                            continue
                    except sqlite3.IntegrityError:
                        self.store.hold_reconciliation(job['task_id'])
                        await self.alert('upstream_task_already_bound',job['task_id'])
            elif len(candidates)>1:
                self.store.hold_reconciliation(job['task_id'])
                await self.alert('multiple_reconciliation_matches',job['task_id'])
            if job['error_code'] in RECONCILE_HOLD_CODES:
                await self.alert(job['error_code'],job['task_id'])

    async def tick(self)->None:
        await self.refresh()
        await self.reconcile()
        if not self.config.dispatch_enabled or time.monotonic()<self.next_dispatch_at or self.store.running():return
        queued=self.store.next_eligible(self.config.owner_id)
        if not queued:return
        task_id=queued['task_id']
        previous=queued['state']
        if previous=='reconcile' and queued['reconcile_attempts']>=RECONCILE_RETRY_LIMIT:
            self.store.hold_reconciliation(task_id)
            await self.alert('reconcile_review',task_id)
            return
        try:
            active=await self.tokora.active_tasks()
        except ApiError as exc:
            self.next_dispatch_at=time.monotonic()+max(15,self.config.poll_seconds,exc.retry_after)
            if exc.code in AUTH_ERRORS:await self.alert(exc.code)
            return
        # An idempotent replay can create the first remote job. Apply the same
        # workspace and subscription guards as on a fresh launch.
        if active or self.store.running():return
        current=self.store.task(task_id,queued['user_id'])
        if not current or current['state']!=previous or not self.store.is_active(queued['user_id'],self.config.owner_id):
            return
        payload=json.loads(current['payload_json'])
        allowed=set(self.store.account_ids(queued['user_id']))
        requested=set(payload.get('assets',{}).get('accountIds',[]))
        if not requested or not requested.issubset(allowed):
            if previous=='reconcile':
                self.store.hold_reconciliation(task_id)
                await self.alert('account_assignment_changed',task_id)
            else:
                self.store.transition(task_id,'failed',error_code='account_assignment_changed',expected_state=previous)
            return
        try:
            validate_task_options(payload.get('settings'))
            if 'feedWarm' in payload:
                validate_feedwarm(payload['feedWarm'],account_count=len(requested))
        except ValueError:
            if previous=='reconcile':
                self.store.hold_reconciliation(task_id)
            else:
                self.store.transition(task_id,'failed',error_code='task_limits',expected_state=previous)
            await self.alert('task_limits',task_id)
            return
        if not self.store.claim_start(task_id,previous,self.config.owner_id):
            return
        try:
            result=await self.tokora.start(payload)
            upstream_id=result.get('id')
            if not isinstance(upstream_id,str) or not VALID_ID.fullmatch(upstream_id):
                raise ApiError('missing_response_id')
            state=snapshot_state(result) or 'running'
            self.store.transition(task_id,state,upstream_id=upstream_id,snapshot=result,
                                  error_code='execution_failed' if state=='failed' else '',expected_state='starting')
        except ApiError as exc:
            await self.start_failed(task_id,previous,exc)
        except sqlite3.IntegrityError:
            self.store.transition(task_id,'reconcile',error_code='reconcile_review',expected_state='starting')
            await self.alert('upstream_task_already_bound',task_id)
        except Exception:
            log.exception('Worker error')
            self.store.transition(task_id,'reconcile',error_code='connection_unavailable',expected_state='starting')

    async def start_failed(self, task_id:str, previous:str, exc:ApiError)->None:
        delay=0.0
        alert=''
        if exc.status==429 or (exc.code in BUSY and exc.status<500):
            state,error=previous,exc.code
            delay=max(self.config.poll_seconds,exc.retry_after)
        elif exc.code in UNCERTAIN or exc.status==0 or exc.status>=500:
            state,error='reconcile',exc.code
            delay=max(self.config.poll_seconds,15,exc.retry_after)
        elif exc.code in AUTH_ERRORS or exc.status in (401,402,403):
            state,error=previous,'admin_action_required'
            delay=300
            alert=exc.code
        elif previous=='reconcile':
            # A later rejection does not disprove an earlier successful start.
            state,error='reconcile','reconcile_review'
            alert=error
        else:
            state,error='failed',exc.code
            alert=exc.code
        self.store.transition(task_id,state,error_code=error,expected_state='starting')
        if delay:self.next_dispatch_at=time.monotonic()+delay
        if alert:await self.alert(alert,task_id)

    async def run(self)->None:
        self.store.recover()
        while True:
            started=time.monotonic()
            try:await self.tick()
            except asyncio.CancelledError:raise
            except Exception:log.exception('Uncaught worker exception')
            await asyncio.sleep(max(0.5,self.config.poll_seconds-(time.monotonic()-started)))
