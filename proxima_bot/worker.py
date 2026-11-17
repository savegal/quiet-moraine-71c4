from __future__ import annotations
import asyncio
import json
import logging
import math
import sqlite3
import time
from collections.abc import Callable
from .accounts import account_counts,check_status,valid_task_folder
from .api import ACCOUNT_FOLDER_ID,ApiError,TASK_STATE_MAP,Tokora,Telegram,task_finish_pending
from .catalog import (validate_feedwarm, validate_task_options, resolve_payload_targets,
                      is_collection_reply, collected_reply_targets, valid_collection_targets,
                      ReplyLinkResolutionError, video_search_settings,
                      validate_username_targets, username_scan_settings)
from .db import (RECONCILE_HOLD_CODES, TERMINAL_STATES, UNKNOWN_TASK_POOL, PER_LINK_EXECUTION_SECONDS,
                 Store, now, payload_account_ids, remote_task_name, task_execution_limit_seconds)
from .config import Config
from .operations import VALID_ID
from .presentation import h

log=logging.getLogger(__name__)
BUSY={'job_busy','rate_limited'}
UNCERTAIN={'service_unavailable','connection_unavailable','invalid_response'}
AUTH_ERRORS={'subscription_required','api_scope_denied','api_key_invalid'}
RECONCILE_POLL_SECONDS = 30
RECONCILE_PAGE_SIZE = 100
RECONCILE_PAGE_LIMIT = 3
RECONCILE_RETRY_LIMIT = 3
EXECUTION_LIMIT_SECONDS = PER_LINK_EXECUTION_SECONDS
STATUS_RETRY_SECONDS = 30
SHORT_LINK_ATTEMPTS = 3
SHORT_LINK_RETRY_SECONDS = 15
FOLDER_RETRY_SECONDS = 30
INVALID_TARGET_CODES = {'video_not_resolved','invalid_link','invalid_links','invalid_url',
                        'invalid_reply_link','invalid_reply_links','too_many_reply_links','link_not_resolved'}


def snapshot_state(snapshot: dict, previous: str='running') -> str | None:
    raw_state = snapshot.get('state')
    if not isinstance(raw_state,str) or not raw_state:
        return None
    state = TASK_STATE_MAP.get(raw_state.strip().lower(),previous)
    if task_finish_pending(snapshot):
        return previous if previous in TERMINAL_STATES else 'stopping'
    if previous in TERMINAL_STATES and state not in TERMINAL_STATES:
        return previous
    if state not in TERMINAL_STATES and (previous=='stopping' or snapshot.get('stopRequested') is True):
        return 'stopping'
    return state


def marked_task_name(job: dict) -> str | None:
    try:
        payload = json.loads(job['payload_json'])
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
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


def verified_launch_folder(job: dict, owner_id: int | None = None) -> str | None:
    """Return the persisted launch accountFolder, allowing cabinet scopes only for the owner."""
    try:
        payload = json.loads(job.get('launch_payload_json') or '')
    except (TypeError, ValueError):
        return None
    folder = payload.get('accountFolder') if isinstance(payload,dict) else None
    allow_workspace = owner_id is not None and job.get('user_id') == owner_id
    return folder if valid_task_folder(folder, allow_workspace=allow_workspace) else None


class Worker:
    def __init__(self,store:Store,tokora:Tokora,tg:Telegram,config:Config,
                 *, dispatch_allowed: Callable[[], bool] | None = None):
        self.store,self.tokora,self.tg,self.config=store,tokora,tg,config
        self.dispatch_allowed = dispatch_allowed
        self.warned:dict[str,float]={}
        self.next_dispatch_at=0.0
        self.next_reconcile_at=0.0
        self.next_account_check_at=0.0
        self.next_balance_check_at=0.0
        self.status_retry_at: dict[str, float] = {}

    async def alert(self, code:str,task_id:str='',detail:str='')->None:
        timestamp=time.monotonic()
        if timestamp-self.warned.get(code,float('-inf'))<1800:return
        self.warned[code]=timestamp
        text='<b>! Proxima Soft · API</b>\nНужна проверка: <code>'+h(code[:100])+'</code>'
        if code=='reconcile_scope_unknown':
            text+='\n'+h(UNKNOWN_TASK_POOL)
        elif code.startswith('account_folder_mismatch'):
            text+='\nПул трафера в боте не совпадает с папкой в кабинете. Открой /admin → Траферы → «Синхронизировать с папкой».'
        elif code.startswith('no_active_accounts'):
            text+='\nУ трафера нет активных сессий для запуска заказа.'
        elif code=='proxy_balance_low':
            text+='\nТрафик прокси заканчивается. Пополни пакет в кабинете.'
        elif code=='proxy_balance_exhausted':
            text+='\nТрафик прокси исчерпан. Заказы не смогут выполняться до пополнения.'
        elif code=='missing_proxies':
            text+='\nНе хватает прокси для запуска. Проверь раздел прокси в /admin.'
        if detail:
            text+='\n'+h(detail[:250])
        if task_id:text+='\nЗаказ: <code>'+h(task_id)+'</code> · /admin'
        try:await asyncio.wait_for(self.tg.send(self.config.owner_id,text),timeout=5)
        except Exception:log.exception('Failed to deliver admin alert')

    async def check_proxy_balance(self)->None:
        if (not self.config.dispatch_enabled
                or (hasattr(self.store,'dispatch_paused') and self.store.dispatch_paused())
                or not hasattr(self.tokora,'call')):
            return
        timestamp=time.monotonic()
        if timestamp<self.next_balance_check_at:
            return
        self.next_balance_check_at=timestamp+600.0
        try:
            data=await self.tokora.call('GET','/v1/proxies/balance',retries=0)
        except Exception:
            return
        if not isinstance(data,dict) or data.get('hasPackage') is not True:
            return
        status=data.get('status')
        if status not in ('low','exhausted'):
            return
        remaining=data.get('remainingBytes')
        detail=''
        if type(remaining) is int and remaining>=0:
            detail=f'Осталось: {remaining/1_000_000_000:.2f} ГБ' if remaining>=1_000_000_000 else f'Осталось: {remaining/1_000_000:.1f} МБ'
        await self.alert('proxy_balance_'+status,detail=detail)

    async def refresh(self)->None:
        timestamp = time.monotonic()
        self.status_retry_at = {task_id: retry_at for task_id, retry_at in self.status_retry_at.items()
                                if retry_at > timestamp}
        await asyncio.gather(*(self.refresh_job(job) for job in self.store.running() if job['upstream_id']))

    async def refresh_job(self, job:dict)->None:
        task_id = job['task_id']
        if time.monotonic() < self.status_retry_at.get(task_id, 0):
            return
        started = job.get('execution_started_at') or 0
        limit_seconds = task_execution_limit_seconds(job)
        deadline = started + limit_seconds if started else 0
        try:
            try:
                result=await self.tokora.task(job['upstream_id'])
            except ApiError as exc:
                if not deadline or now() < deadline or exc.status == 404 or job['state']=='stopping':
                    raise
                result={'id':job['upstream_id'],'state':job['state']}
            if result.get('id')!=job['upstream_id']:raise ApiError('invalid_response')
            state=snapshot_state(result,job['state'])
            if deadline and now() >= deadline and state not in TERMINAL_STATES and state!='stopping':
                result=await self.tokora.stop(job['upstream_id'])
                result={**result,'stopRequested':True,'executionTimeLimit':True,'executionLimitSeconds':limit_seconds}
                state=snapshot_state(result,'stopping')
            if result.get('id')!=job['upstream_id'] or not state:
                raise ApiError('invalid_response')
            prev_snap=json.loads(job.get('snapshot_json') or '{}')
            if prev_snap.get('executionTimeLimit'):
                result={**result,'executionTimeLimit':True,'executionLimitSeconds':prev_snap.get('executionLimitSeconds') or limit_seconds}
            self.store.transition(job['task_id'],state,snapshot=result,
                error_code='execution_failed' if state=='failed' else '',expected_state=job['state'])
            self.status_retry_at.pop(task_id, None)
        except ApiError as exc:
            if exc.status==404 or exc.code in ('job_not_found','not_found'):
                if job['state']!='reconcile':
                    self.store.transition(job['task_id'],'reconcile',error_code='job_not_found',expected_state=job['state'])
                await self.alert('job_not_found',job['task_id'])
            else:
                delay = max(STATUS_RETRY_SECONDS, exc.retry_after)
                if deadline > now():
                    delay = min(delay, deadline - now())
                self.status_retry_at[task_id] = time.monotonic() + delay
                log.warning('Refresh upstream job failed: %s',exc.code)

    async def refresh_settlement(self,job:dict)->None:
        observed=now()
        try:
            result=await self.tokora.task(job['upstream_id'])
            state=snapshot_state(result,job['state'])
            if result.get('id')!=job['upstream_id'] or not state:
                raise ApiError('invalid_response')
            self.store.transition(job['task_id'],state,snapshot=result,observed_at=observed,
                                  expected_state=job['state'],expected_snapshot=job['snapshot_json'],
                                  error_code='execution_failed' if state=='failed' else '')
        except ApiError as exc:
            retry_at=0 if exc.status==404 else now()+max(15,exc.retry_after)
            self.store.defer_settlement(job,retry_at)

    async def refresh_settlements(self)->None:
        jobs=self.store.due_settlements(now())
        await asyncio.gather(*(self.refresh_settlement(job) for job in jobs))

    async def run_settlements(self)->None:
        while True:
            try:await self.refresh_settlements()
            except Exception:log.exception('Could not refresh late publication confirmations')
            await asyncio.sleep(self.config.poll_seconds)

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
            elif verified_launch_folder(job, self.config.owner_id) is None and job['error_code'] not in RECONCILE_HOLD_CODES:
                self.store.hold_reconciliation(job['task_id'])
                await self.alert('reconcile_scope_unknown',job['task_id'])
            if job['error_code'] in RECONCILE_HOLD_CODES:
                await self.alert(job['error_code'],job['task_id'])

    async def tick(self)->None:
        await self.refresh()
        await self.reconcile()
        if self.dispatch_allowed is not None and not self.dispatch_allowed():
            return
        if await self.account_check_blocks_dispatch():return
        limit = self.config.max_active_external
        paused = hasattr(self.store,'dispatch_paused') and self.store.dispatch_paused()
        if not self.config.dispatch_enabled or paused or time.monotonic()<self.next_dispatch_at or len(self.store.running()) >= limit:
            return
        queued=self.store.next_eligible(self.config.owner_id)
        if not queued:return
        task_id=queued['task_id']
        previous=queued['state']
        try:
            active=await self.tokora.active_tasks()
        except ApiError as exc:
            auth=exc.code in AUTH_ERRORS or exc.status in (401,402,403)
            self.next_dispatch_at=time.monotonic()+max(60 if auth else 15,self.config.poll_seconds,exc.retry_after)
            if auth:
                if previous=='queued':
                    self.store.transition(task_id,'queued',error_code='admin_action_required',expected_state=previous)
                await self.alert(exc.code,task_id)
            return
        # An idempotent replay can create the first remote job. Apply the same
        # workspace and subscription guards as on a fresh launch.
        running = self.store.running()
        running_ids = {job['upstream_id'] for job in running if job['upstream_id']}
        remote_only = [job for job in active if job.get('kind')!='import' and job['id'] not in running_ids]
        local_limit = limit - len(remote_only)
        if len(running) >= local_limit:
            return
        # Tokora can list the current folder but does not expose the historical launch set.
        unresolved = self.store.unresolved_tasks()
        known_ids = {job['upstream_id'] for job in unresolved if verified_launch_folder(job, self.config.owner_id) is not None}
        if (any(job['id'] not in known_ids for job in remote_only)
                or any(verified_launch_folder(job, self.config.owner_id) is None for job in (*running,*unresolved)
                       if job['task_id'] != task_id)):
            if previous=='queued' and queued['error_code']!='external_pool_unknown':
                self.store.transition(task_id,'queued',error_code='external_pool_unknown',expected_state='queued')
            return
        if previous=='reconcile' and queued['reconcile_attempts']>=RECONCILE_RETRY_LIMIT:
            self.store.hold_reconciliation(task_id)
            await self.alert('reconcile_review',task_id)
            return
        current=self.store.task(task_id,queued['user_id'])
        if not current or current['state']!=previous or not self.store.is_active(queued['user_id'],self.config.owner_id):
            return
        frozen=current.get('launch_payload_json')
        folder = verified_launch_folder(current, self.config.owner_id)
        if previous=='reconcile' and folder is None:
            self.store.hold_reconciliation(task_id)
            await self.alert('reconcile_scope_unknown',task_id)
            return
        try:
            payload=json.loads(frozen or current['payload_json'])
            requested=payload_account_ids(payload)
        except (TypeError, ValueError):
            requested=[]
        if not requested or requested != self.store.eligible_task_accounts(current['user_id'],requested,self.config.owner_id):
            if previous=='reconcile':
                self.store.hold_reconciliation(task_id)
                await self.alert('account_assignment_changed' if requested else 'reconcile_scope_unknown',task_id)
            else:
                self.store.transition(task_id,'failed',error_code='account_assignment_changed',expected_state=previous)
            return
        if self.store.account_actions_pending(requested):return
        try:
            validate_task_options(payload.get('settings'))
            is_owner = current['user_id'] == self.config.owner_id
            saved_folder = payload.get('accountFolder')
            if ((folder is None and 'accountFolder' in payload
                 and (not is_owner or not valid_task_folder(saved_folder, allow_workspace=True)))
                    or any(payload.get(key) for key in ('checkerFolder','likerFolder','likersOnly'))
                    or payload.get('assets',{}).get('folderIds')):
                raise ValueError('Launch requires explicit assigned account IDs')
            photo_allowed=(self.store.can_use_photos(queued['user_id'])
                           if hasattr(self.store,'can_use_photos') else queued['user_id']==self.config.owner_id)
            if payload.get('usePhoto') and not photo_allowed:
                raise ValueError('Shared photos require owner access')
            if 'feedWarm' in payload:
                validate_feedwarm(payload['feedWarm'],account_count=len(requested))
        except ValueError:
            if previous=='reconcile':
                self.store.hold_reconciliation(task_id)
            else:
                self.store.transition(task_id,'failed',error_code='task_limits',expected_state=previous)
            await self.alert('task_limits',task_id)
            return
        if not frozen:
            try:
                if previous == 'queued' and is_collection_reply(payload):
                    payload = await self.prepare_collected_reply(current, payload)
                if current['kind'] == 'usernames':
                    validate_username_targets(payload.get('awemeIds'))
                    payload['settings'] = username_scan_settings(payload.get('settings') or {})
                if current['kind'] == 'feedwarm':
                    payload['feedWarm'] = video_search_settings(payload.get('feedWarm'), account_count=len(requested))
                payload=await resolve_payload_targets(self.tokora,payload)
            except ApiError as exc:
                if previous == 'queued' and exc.status == 400 and exc.code == 'short_link_unresolved':
                    attempt = current['target_resolution_attempts'] + 1
                    delay = max(SHORT_LINK_RETRY_SECONDS * attempt, exc.retry_after)
                    retry_at = now() + math.ceil(delay) if attempt < SHORT_LINK_ATTEMPTS else 0
                    self.store.record_target_resolution_failure(task_id, attempt, retry_at)
                    log.info('Short link resolution for order %s: attempt %d/%d, request_id=%s',
                             task_id, attempt, SHORT_LINK_ATTEMPTS, exc.request_id)
                elif exc.status == 429 or exc.code in BUSY:
                    self.next_dispatch_at=time.monotonic()+max(15,exc.retry_after)
                elif exc.code in AUTH_ERRORS or exc.status in (401,402,403):
                    retry_at=now()+max(60,self.config.poll_seconds,math.ceil(exc.retry_after))
                    self.store.defer_launch(task_id,previous,'admin_action_required',retry_at,{})
                    await self.alert(exc.code,task_id)
                elif exc.status in (400,404) or exc.code in INVALID_TARGET_CODES:
                    if previous == 'reconcile':
                        self.store.hold_reconciliation(task_id)
                    else:
                        self.store.transition(task_id,'failed',error_code='invalid_order_targets',expected_state=previous)
                else:
                    retry_at=now()+max(15,self.config.poll_seconds,math.ceil(exc.retry_after))
                    self.store.defer_launch(task_id,previous,'connection_unavailable',retry_at,{})
                    log.warning('Deferred target resolution: HTTP %s code=%s request_id=%s',
                                exc.status,exc.code,exc.request_id)
                return
            except ReplyLinkResolutionError as exc:
                self.store.transition(task_id,'failed',error_code='invalid_order_targets',
                                      preflight={'reply_link_error_lines':exc.lines},expected_state=previous)
                return
            except ValueError:
                if previous == 'reconcile':
                    self.store.hold_reconciliation(task_id)
                else:
                    self.store.transition(task_id,'failed',error_code='invalid_order_targets',expected_state=previous)
                return
            payload['settings']={**(payload.get('settings') or {}),'delaySeconds':2,'uniqueText':False}
            payload['settings'].pop('accountsPerVideo',None)
        account_versions = self.store.account_action_versions(requested)
        launch = await self.check_launch_pool(current,payload,requested,folder,account_versions)
        if launch is None:return
        if self.dispatch_allowed is not None and not self.dispatch_allowed():
            return
        payload, replay_scope = launch
        if not self.store.claim_start(task_id,previous,self.config.owner_id,launch_payload=payload,
                                      account_versions=account_versions,max_active=local_limit,
                                      replay_scope=replay_scope):
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

    async def prepare_collected_reply(self, job: dict, payload: dict) -> dict:
        source = self.store.task_by_upstream(payload['sourceJob'], job['user_id'])
        if not source:
            raise ValueError('Исходный сбор недоступен.')
        if source['kind'] != 'fetch':
            if source['kind']=='publish' and payload.get('replySourceMode')=='all':
                return payload
            raise ValueError('Выбранные комментарии должны относиться к заданию на сбор.')
        if source['state'] != 'completed':
            raise ValueError('Исходный сбор ещё не завершён.')
        if payload.get('replyLinks'):
            raise ValueError('Выбери исходный сбор или ссылки на комментарии.')
        targets=payload['targetIds']
        if payload.get('replySourceMode')=='all':
            targets=await collected_reply_targets(self.tokora,source['upstream_id'],len(targets),previous=targets)
        if not valid_collection_targets(targets):
            raise ValueError('Не удалось проверить ID комментариев из сбора.')
        result={**payload,'targetIds':targets}
        result.pop('replySourceMode',None)
        return result

    async def account_check_blocks_dispatch(self)->bool:
        if self.store.workspace_action_active():
            return True
        marker = self.store.account_check_marker()
        if marker is None:
            return False
        if time.monotonic() < self.next_account_check_at:
            return True
        self.next_account_check_at = time.monotonic() + self.config.poll_seconds
        try:
            status = await self.tokora.call('GET','/v1/accounts/check',retries=0)
        except ApiError as exc:
            delay = 60 if exc.status in (401,402,403) else 15
            self.next_account_check_at = time.monotonic() + max(delay,self.config.poll_seconds,exc.retry_after)
            log.warning('Account check status unavailable: %s',exc.code)
            return True
        if check_status(status) != 'idle':
            return True
        self.store.clear_account_check_pending(marker)
        return self.store.account_check_pending() or self.store.workspace_action_active()

    async def _defer_pool_api_error(self,task_id:str,state:str,stage:dict,exc:ApiError)->None:
        auth = exc.code in AUTH_ERRORS or exc.status in (401,402,403)
        delay=max(60 if auth else FOLDER_RETRY_SECONDS,self.config.poll_seconds,math.ceil(exc.retry_after))
        self.store.defer_launch(task_id,state,'account_check_unavailable',now()+delay,{**stage,'phase':'waiting'})
        if auth:await self.alert(exc.code,task_id)

    async def _fail_no_active(self,job:dict,state:str,requested_count:int,result:dict)->None:
        task_id=job['task_id']
        role='Владелец' if job['user_id']==self.config.owner_id else 'Трафер'
        self.store.transition(task_id,'failed',error_code='no_active_accounts',preflight=result,expected_state=state)
        await self.alert(f'no_active_accounts:{task_id}',task_id,detail=f'{role} ID {job["user_id"]} · активных 0 из {requested_count}')

    async def _confirm_owner_cabinet_scope(self,job:dict,payload:dict,requested:list[str],explicit_folder:str,
                                           scoped:dict[str,dict],account_versions:dict[str,str],
                                           stage:dict)->tuple[list[str],dict[str,dict],dict]|None:
        """Refresh live cabinet/folder accounts when owner membership drifted while queued."""
        task_id=job['task_id']
        state=job['state']
        live_ids=sorted(scoped)
        if not live_ids:
            await self._fail_no_active(job,state,0,{'phase':'checked','requested':0,'active':0,'expired':0,'error':0,'total':0})
            return None
        if set(live_ids)!=set(requested):
            refreshed_versions=self.store.account_action_versions(live_ids)
            try:
                scoped=await self.tokora.accounts_in_folder(explicit_folder,expected_count=None)
            except ApiError as exc:
                await self._defer_pool_api_error(task_id,state,stage,exc)
                return None
            live_ids2=sorted(scoped)
            if not live_ids2:
                await self._fail_no_active(job,state,0,{'phase':'checked','requested':0,'active':0,'expired':0,'error':0,'total':0})
                return None
            if (live_ids2!=live_ids
                    or self.store.account_action_versions(live_ids2)!=refreshed_versions):
                self.store.transition(task_id,state,preflight={**stage,'requested':len(live_ids2),'phase':'waiting'},expected_state=state)
                return None
            # Mutate caller's dict in place so claim_start verifies versions across the refreshed scope.
            account_versions.clear()
            account_versions.update(refreshed_versions)
            requested=live_ids2
            if 'feedWarm' in payload:
                try:
                    validate_feedwarm(payload['feedWarm'],account_count=len(requested))
                except ValueError:
                    self.store.transition(task_id,'failed',error_code='task_limits',expected_state=state)
                    await self.alert('task_limits',task_id)
                    return None
            payload={**payload,'assets':{**payload.get('assets',{}),'accountIds':requested},'accountFolder':explicit_folder}
        return requested,scoped,payload

    async def check_launch_pool(self,job:dict,payload:dict,requested:list[str],folder:str|None,
                                account_versions:dict[str,str])->tuple[dict, list[str] | None] | None:
        """Verify live Tokora folder/cabinet scope before claim_start; mutates account_versions if owner scope drifts."""
        task_id=job['task_id']
        state=job['state']
        is_owner=job['user_id']==self.config.owner_id
        explicit_folder=folder if folder is not None else (
            payload.get('accountFolder') if is_owner and 'accountFolder' in payload else None
        )
        owner_cabinet=is_owner and explicit_folder is not None
        replay_scope = None
        stage={'phase':'checking','requested':len(requested)}
        if state=='queued' and not self.store.transition(task_id,state,error_code='',preflight=stage,expected_state=state):
            return None
        try:
            if owner_cabinet:
                scoped=await self.tokora.accounts_in_folder(explicit_folder,expected_count=None)
                folder=explicit_folder
            else:
                if folder is None:
                    accounts=await self.tokora.accounts_for(requested)
                    folders={account.get('folderId') or ('none' if is_owner else None)
                             for account in accounts.values()}
                    if len(folders)==1:
                        candidate=next(iter(folders))
                        if isinstance(candidate,str) and (
                                ACCOUNT_FOLDER_ID.fullmatch(candidate) or (is_owner and candidate=='none')):
                            folder=candidate
                scoped=await self.tokora.accounts_in_folder(folder,expected_count=len(requested)) if folder is not None else {}
        except ApiError as exc:
            await self._defer_pool_api_error(task_id,state,stage,exc)
            return None
        if owner_cabinet and state=='queued':
            refreshed=await self._confirm_owner_cabinet_scope(job,payload,requested,explicit_folder,scoped,account_versions,stage)
            if refreshed is None:
                return None
            requested,scoped,payload=refreshed
            stage={'phase':'checking','requested':len(requested)}
        elif owner_cabinet and state=='reconcile':
            if not scoped:
                self.store.defer_launch(task_id,state,'account_folder_mismatch',now()+FOLDER_RETRY_SECONDS,
                                        {**stage,'phase':'waiting'})
                return None
            extra = sorted(set(scoped) - set(requested))
            if extra:
                extra_versions = self.store.account_action_versions(extra)
                account_versions.update({account_id: extra_versions[account_id] for account_id in extra})
                try:
                    refreshed_scope = await self.tokora.accounts_in_folder(explicit_folder,expected_count=None)
                except ApiError as exc:
                    await self._defer_pool_api_error(task_id,state,stage,exc)
                    return None
                if refreshed_scope.keys() != scoped.keys():
                    self.store.defer_launch(task_id,state,'account_folder_mismatch',now()+FOLDER_RETRY_SECONDS,
                                            {**stage,'phase':'waiting'})
                    return None
                replay_scope = sorted(scoped)
                requested = sorted(set(requested).union(extra))
        if (self.store.account_actions_pending(requested) or self.store.account_check_pending()
                or self.store.account_action_versions(requested)!=account_versions):
            self.store.transition(task_id,state,preflight={**stage,'phase':'waiting'},expected_state=state)
            return None
        if owner_cabinet:
            used,unknown_pool=self.store._task_accounts(exclude_task=task_id)
            if unknown_pool or set(requested).intersection(used):
                self.store.transition(task_id,state,preflight={**stage,'phase':'waiting'},expected_state=state)
                return None
        elif set(scoped)!=set(requested):
            self.store.defer_launch(task_id,state,'account_folder_mismatch',now()+FOLDER_RETRY_SECONDS,
                                    {**stage,'phase':'waiting'})
            extra_in_folder=len(set(scoped)-set(requested))
            missing_from_folder=len(set(requested)-set(scoped))
            detail=f'Трафер ID {job["user_id"]} · папка {folder or "не найдена"} · не хватает в папке: {missing_from_folder}, лишних в папке: {extra_in_folder}'
            await self.alert(f'account_folder_mismatch:{task_id}',task_id,detail=detail)
            return None
        if state=='queued':
            result={'phase':'checked','requested':len(requested),**account_counts(requested,scoped)}
            active=result['active']
            if not active and job['kind']!='check':
                await self._fail_no_active(job,state,len(requested),result)
                return None
            if 'feedWarm' in payload and payload['feedWarm'].get('accounts',0)>active:
                self.store.transition(task_id,'failed',error_code='not_enough_active_accounts',preflight=result,expected_state=state)
                return None
            if not self.store.transition(task_id,state,preflight=result,expected_state=state):return None
        return {**payload,'accountFolder':folder}, replay_scope

    async def start_failed(self, task_id:str, previous:str, exc:ApiError)->None:
        delay=0.0
        alert=''
        if exc.status==429 or exc.code in BUSY:
            state,error=previous,'' if exc.code=='job_busy' else exc.code
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
        elif exc.status==409 and exc.code=='job_duplicate':
            state,error='failed','job_duplicate'
        elif (exc.status in (400,409) and exc.code in
              {'invalid_target','invalid_source','missing_source','empty_job','invalid_reply_links','too_many_reply_links'}):
            state,error='failed',exc.code
        else:
            state,error='failed',exc.code
            alert=exc.code
        # A definite rejection of the first launch leaves no remote task.
        # Recheck readiness on the next attempt, while preserving ambiguous replays.
        self.store.transition(task_id,state,error_code=error,expected_state='starting',
                              clear_launch_payload=previous=='queued' and state=='queued')
        if delay:self.next_dispatch_at=time.monotonic()+delay
        if alert:await self.alert(alert,task_id)

    async def run(self)->None:
        self.store.recover()
        while True:
            started=time.monotonic()
            try:
                await self.tick()
                await self.check_proxy_balance()
            except asyncio.CancelledError:raise
            except Exception:log.exception('Uncaught worker exception')
            await asyncio.sleep(max(0.5,self.config.poll_seconds-(time.monotonic()-started)))
