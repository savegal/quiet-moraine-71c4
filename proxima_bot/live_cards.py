"""Edit the latest open order card in each private chat."""
import asyncio
from dataclasses import dataclass
import logging
import time

from .api import TelegramError
from .db import OPEN_STATES

log = logging.getLogger(__name__)

@dataclass
class LiveCard:
    task_id: str
    message_id: int
    admin: bool
    view: tuple[str, str, dict]
    rich: bool
    retry_at: float = 0


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

    async def open(self, uid: int, task: dict, view: tuple[str, str, dict], *, admin: bool = False):
        self.cards.pop(uid, None)
        message = await self.app.tg.rich(uid, *view)
        if isinstance(message, dict) and message.get('message_id') and task['state'] in OPEN_STATES:
            self.cards[uid] = LiveCard(task['task_id'], message['message_id'], admin, view,
                                       bool(message.get('rich_message')))

    async def refresh(self, uid: int, card: LiveCard):
        async with self.edit_slots:
            if self.cards.get(uid) is not card or card.retry_at > time.monotonic():
                return
            is_admin = card.admin and self.app.owner(uid)
            task = self.app.db.task(card.task_id, None if is_admin else uid)
            if not task:
                self.cards.pop(uid, None)
                return
            render = self.app.render_admin_job if is_admin else self.app.render_task
            view = render(task)
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
            if task['state'] not in OPEN_STATES and self.cards.get(uid) is card:
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
            await asyncio.sleep(5)
            await self.tick()
