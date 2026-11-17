"""Edit the latest open order card in each private chat."""
import asyncio
from dataclasses import dataclass, field
import logging
import time

from .api import ApiError, TelegramError
from .account_checks import read_workspace_account_check
from .db import task_updates_live

log = logging.getLogger(__name__)
POLL_SECONDS = 5


def workspace_task_updates_live(task:dict) -> bool:
    return task_updates_live(task) or task['state']=='unknown'

@dataclass
class LiveCard:
    task_id: str|None
    message_id: int
    admin: bool
    view: tuple[str, str, dict]
    rich: bool
    retry_at: float = 0
    kind: str = 'task'
    edit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class LiveCards:
    def __init__(self, app):
        self.app = app
        self.cards: dict[int, LiveCard] = {}
        self.edit_slots = asyncio.Semaphore(8)
        listeners = getattr(app.tg, 'edit_listeners', None)
        if listeners is not None:
            listeners.append(self.forget_message)

    def forget_message(self, chat_id: int, message_id: int):
        # The card message was reused for another screen: stop live-updating it.
        card = self.cards.get(chat_id)
        if card and card.message_id == message_id:
            self.cards.pop(chat_id, None)

    async def leave_message(self,uid:int,message_id:int):
        card = self.cards.get(uid)
        if card and card.message_id==message_id:
            self.forget_message(uid,message_id)
            # Finish any edit already sent before the next screen edits this message.
            async with card.edit_lock:
                pass

    async def open(self, uid: int, task: dict, view: tuple[str, str, dict], *, admin: bool = False):
        self.cards.pop(uid, None)
        message = await self.app.tg.rich(uid, *view)
        if isinstance(message, dict) and message.get('message_id') and task_updates_live(task):
            self.cards[uid] = LiveCard(task['task_id'], message['message_id'], admin, view,
                                       bool(message.get('rich_message')))

    async def open_account_check(self,uid:int,status:dict):
        view=self.app.render_account_check(status)
        return await self.open_check(uid, 'accounts', view, self.app.account_check_active(status))

    async def open_proxy_check(self,uid:int,status:dict):
        view=self.app.render_proxy_check(status)
        return await self.open_check(uid, 'proxies', view, self.app.proxy_check_active(status))

    async def open_pool_check(self,uid:int,job:dict):
        view = self.app.pool_checks.render(uid,job)
        return await self.open_check(uid, 'pool', view, job['state']=='running', job['check_id'])

    async def open_account_import(self,uid:int,job:dict):
        view = self.app.account_imports.render(job)
        return await self.open_check(uid, 'import', view, job['state']=='running', job['import_id'])

    async def open_workspace_task(self,uid:int,task:dict):
        if not self.app.owner(uid):
            return
        view = self.app.render_workspace_task(task)
        return await self.open_check(uid,'workspace_task',view,workspace_task_updates_live(task),task['task_id'])

    async def open_check(self,uid:int,kind:str,view:tuple,active:bool,check_id:str|None=None):
        self.cards.pop(uid,None)
        message=await self.app.tg.rich(uid,*view)
        if isinstance(message,dict) and message.get('message_id') and active:
            self.cards[uid]=LiveCard(check_id,message['message_id'],self.app.owner(uid),view,
                                     bool(message.get('rich_message')),kind=kind)

    async def refresh(self, uid: int, card: LiveCard):
        async with self.edit_slots:
            if self.cards.get(uid) is not card or card.retry_at > time.monotonic():
                return
            is_admin = card.admin and self.app.owner(uid)
            if card.kind in ('media_post','media_plan'):
                if not is_admin:
                    self.cards.pop(uid,None)
                    return
                path='/v1/posts/'+('plan/' if card.kind=='media_plan' else '')+card.task_id
                try:
                    async with asyncio.timeout(POLL_SECONDS):
                        result=await self.app.api.call('GET',path,retries=0)
                    if result.get('id')!=card.task_id:raise ApiError('invalid_response')
                    if card.kind=='media_plan':
                        view=self.app.render_post_plan(result)
                        keep=result.get('state')=='checking'
                    else:
                        view=self.app.render_post_card(result)
                        keep=result.get('outcome') in ('queued','uploading','submitting','pending','moderation')
                except (ApiError,TimeoutError) as exc:
                    if isinstance(exc,ApiError) and exc.status in (401,403,404):
                        self.cards.pop(uid,None)
                    else:
                        card.retry_at=time.monotonic()+max(10,getattr(exc,'retry_after',0))
                    return
            elif card.kind in ('accounts','proxies','workspace_task'):
                if not is_admin:
                    self.cards.pop(uid,None)
                    return
                try:
                    if card.kind == 'accounts':
                        status = await read_workspace_account_check(self.app.api, self.app.db)
                        view = self.app.render_account_check(status)
                        keep = self.app.account_check_active(status)
                    elif card.kind == 'proxies':
                        status = await self.app.api.call('GET','/v1/proxies/check',retries=0)
                        view = self.app.render_proxy_check(status)
                        keep = self.app.proxy_check_active(status)
                    else:
                        async with asyncio.timeout(POLL_SECONDS):
                            task = await self.app.read_workspace_task(card.task_id)
                        view = self.app.render_workspace_task(task)
                        keep = workspace_task_updates_live(task)
                except TimeoutError:
                    card.retry_at = time.monotonic()+POLL_SECONDS
                    return
                except ApiError as exc:
                    if card.kind=='workspace_task' and exc.status in (401,403,404):
                        plain = ('Задача больше не найдена.' if exc.status==404
                                 else self.app.account_error_message(uid,exc))
                        view,keep = (f'<p>{plain}</p>',plain,card.view[2]),False
                    else:
                        card.retry_at=time.monotonic()+max(10,exc.retry_after)
                        return
                if self.cards.get(uid) is not card:return
            elif card.kind == 'pool':
                job = self.app.pool_checks.get(uid, card.task_id)
                if not job:
                    self.cards.pop(uid,None)
                    return
                view = self.app.pool_checks.render(uid,job)
                keep = job['state'] == 'running'
            elif card.kind == 'import':
                job = self.app.db.account_import(uid,import_id=card.task_id) if is_admin else None
                if not job:
                    self.cards.pop(uid,None)
                    return
                view = self.app.account_imports.render(job)
                keep = job['state'] == 'running'
            else:
                task = self.app.db.task(card.task_id, None if is_admin else uid)
                if not task:
                    self.cards.pop(uid, None)
                    return
                render = self.app.render_admin_job if is_admin else self.app.render_task
                view = render(task)
                keep=task_updates_live(task)
            async with card.edit_lock:
                if self.cards.get(uid) is not card:
                    return
                if view != card.view:
                    rich_html,plain,markup=view
                    try:
                        if card.rich:
                            try:
                                await self.app.tg.edit_rich(uid,card.message_id,rich_html,markup)
                            except TelegramError as exc:
                                if exc.code!=400 or 'not modified' in exc.description.lower():raise
                                card.rich=False
                        if not card.rich:
                            await self.app.tg.edit(uid,card.message_id,plain,markup)
                    except TelegramError as exc:
                        if exc.code in (400, 403, 404) and 'not modified' not in exc.description.lower():
                            if self.cards.get(uid) is card:
                                self.cards.pop(uid, None)
                            return
                        if 'not modified' not in exc.description.lower():
                            card.retry_at = time.monotonic() + max(5, exc.retry_after)
                            return
                    card.view = view
                if not keep and self.cards.get(uid) is card:
                    self.cards.pop(uid, None)

    async def tick(self):
        cards=list(self.cards.items())
        outcomes=await asyncio.gather(*(self.refresh(uid,card) for uid,card in cards),return_exceptions=True)
        for (uid,card), outcome in zip(cards,outcomes):
            if isinstance(outcome,Exception):
                log.warning('Live card refresh failed for task %s: %s',card.task_id,type(outcome).__name__)
                if self.cards.get(uid) is card:
                    card.retry_at=time.monotonic()+30

    async def run(self):
        while True:
            await asyncio.sleep(POLL_SECONDS)
            await self.tick()
