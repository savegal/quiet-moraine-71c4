"""Run isolated Tokora cabinets behind one Telegram bot and update inbox."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, replace
from functools import partial
from html import unescape
from itertools import combinations
from secrets import token_hex

from .advanced_db import ExtendedStore
from .admission import BotAdmission, ENTRY_OPEN, ENTRY_PANEL, ENTRY_ROUTE
from .api import ApiError, Telegram, TelegramError, Tokora
from .config import TOKORA_TASK_SLOTS
from .db import MAX_USER_ID
from .extended import ProximaApp
from .presentation import h, keyboard
from .provider_alerts import ProviderAlerts
from .rich_ui import screen
from .updates import TOKEN_TEXT, UpdateInbox
from .worker import Worker
from .workspace_api import CabinetAPI, cabinet_witnesses, has_task_witness, same_cabinet, task_witnesses
from .workspace_registry import (CREATE_COMMAND, KEY_COMMAND, KEY_FORMAT, LEGACY_COMMANDS, MEMBER_COMMAND,
                                 PRIMARY_WORKSPACE, WorkspaceRegistry, workspace_database)

log = logging.getLogger(__name__)
RICH_BUTTON = re.compile(r'<tg-button(?=\s)[^>]*>')
RICH_CALLBACK_DATA = re.compile(r'(?<=\s)data="([^"]*)"')


class CabinetTelegram(Telegram):
    def __init__(self, session, token: str, transport: Telegram, registry: WorkspaceRegistry, workspace: str):
        super().__init__(session, token)
        self.transport = transport
        self.registry = registry
        self.workspace = workspace

    async def call(self, method: str, data: dict, *, timeout: int = 25):
        if method in ('sendMessage', 'sendRichMessage', 'editMessageText', 'editMessageReplyMarkup'):
            data = self.scope_buttons(data)
        result = await self.transport.call(method, data, timeout=timeout)
        if method in ('sendMessage', 'sendRichMessage', 'editMessageText') and isinstance(result, dict):
            message_id = result.get('message_id')
            chat_id = data.get('chat_id')
            if type(message_id) is int and message_id > 0 and type(chat_id) is int:
                self.registry.bind_message(chat_id, message_id, self.workspace)
        return result

    def scope_buttons(self, data: dict) -> dict:
        def scoped(callback: str) -> str:
            return self.registry.pack_callback(self.workspace, callback)

        result = data.copy()
        markup = data.get('reply_markup') or {}
        if 'inline_keyboard' in markup:
            result['reply_markup'] = {**markup, 'inline_keyboard': [
                [{**button, 'callback_data': scoped(button['callback_data'])}
                 if 'callback_data' in button else button for button in row]
                for row in markup['inline_keyboard']
            ]}
        rich = data.get('rich_message') or {}
        if 'html' in rich:
            def rewrite(match: re.Match[str]) -> str:
                tag = match.group()
                if ' type="callback_data"' not in tag:
                    return tag
                return RICH_CALLBACK_DATA.sub(lambda value: f'data="{scoped(unescape(value[1]))}"', tag, count=1)

            result['rich_message'] = {**rich, 'html': RICH_BUTTON.sub(rewrite, rich['html'])}
        return result


@dataclass
class Cabinet:
    app: ProximaApp
    api: CabinetAPI
    worker: Worker


class WorkspaceApp:
    credential_command = KEY_COMMAND
    credential_commands = (KEY_COMMAND, *(alias for alias, command in LEGACY_COMMANDS.items() if command == KEY_COMMAND))

    def __init__(self, config, db: ExtendedStore, tg: Telegram, session):
        self.cfg, self.db, self.tg, self.session = config, db, tg, session
        self.registry = WorkspaceRegistry(db.conn, config.database_path, config.upstream_key)
        self.admission = BotAdmission(db,tg,config.owner_id)
        self.provider_alerts = ProviderAlerts(tg,config.owner_id)
        self.cabinets: dict[str, Cabinet] = {}
        self.services: asyncio.TaskGroup | None = None
        self.key_confirmation: tuple[tuple[str, str, str, str], float] | None = None
        self.key_input: tuple[str, float] | None = None
        for workspace in self.registry.workspaces():
            self.open_cabinet(workspace)
        self.admission.migrate_members(cabinet.app.db for cabinet in self.cabinets.values())

    def open_cabinet(self, workspace: dict) -> Cabinet:
        workspace_id = workspace['id']
        if workspace_id in self.cabinets:
            return self.cabinets[workspace_id]
        path = workspace_database(self.cfg.database_path, workspace_id)
        config = replace(self.cfg, database_path=path, upstream_key='', max_active_external=min(
            self.cfg.max_active_external, TOKORA_TASK_SLOTS))
        store = self.db if workspace_id == PRIMARY_WORKSPACE else ExtendedStore(path, timezone_name=config.timezone_name)
        tg = CabinetTelegram(self.session, config.telegram_token, self.tg, self.registry, workspace_id)
        api = CabinetAPI(self.session,config.base_url,config.upstream_timeout,
                         report_error=partial(self.provider_alerts.request,workspace['name']))
        try:
            api.set_keys(self.registry.credentials(workspace_id))
        except ValueError as exc:
            # Keep access to the owner panel if the key file needs to be restored.
            log.error('Cannot load Tokora credentials for cabinet %s: %s', workspace_id, exc)
        app = ProximaApp(config, store, tg, api)
        app.bot_admission = self.admission
        app.cabinet_name = workspace['name']
        app.cabinet_dispatch_allowed = lambda: self.can_dispatch(workspace_id)
        app.can_switch_cabinet = lambda uid: workspace_id != PRIMARY_WORKSPACE or any(
            row['id'] != PRIMARY_WORKSPACE and self.registry.can_open(row['id'], uid, config.owner_id)
            for row in self.registry.workspaces()
        )
        cabinet = Cabinet(app, api, Worker(store, api, tg, config,
                          dispatch_allowed=app.cabinet_dispatch_allowed,
                          report_task_error=partial(self.provider_alerts.task,workspace['name'])))
        self.cabinets[workspace_id] = cabinet
        if self.services is not None:
            self.start_cabinet(cabinet)
        return cabinet

    def start_cabinet(self, cabinet: Cabinet) -> None:
        self.services.create_task(cabinet.worker.run())
        self.services.create_task(cabinet.worker.run_settlements())
        self.services.create_task(cabinet.app.live_cards.run())

    def can_dispatch(self, workspace: str) -> bool:
        return all(other == workspace or not cabinet.api.clients or self.registry.distinct(workspace, other)
                   for other, cabinet in self.cabinets.items())

    async def verify_existing_cabinets(self) -> None:
        active = [(workspace, cabinet.api) for workspace, cabinet in self.cabinets.items() if cabinet.api.clients]
        for (left_id, left), (right_id, right) in combinations(active, 2):
            if self.registry.distinct(left_id, right_id):
                continue
            before = (tuple(left.clients.values()), tuple(right.clients.values()))
            try:
                left_evidence, right_evidence = await asyncio.gather(cabinet_witnesses(left), cabinet_witnesses(right))
                match = await same_cabinet(left, left_evidence, right, right_evidence)
            except ApiError:
                continue
            if match is False and before == (tuple(left.clients.values()), tuple(right.clients.values())):
                self.registry.confirm_distinct(left_id, right_id)

    async def run_cabinet_verification(self) -> None:
        while True:
            await self.verify_existing_cabinets()
            await asyncio.sleep(30)

    async def run(self) -> None:
        try:
            async with asyncio.TaskGroup() as services:
                self.services = services
                for cabinet in self.cabinets.values():
                    self.start_cabinet(cabinet)
                services.create_task(UpdateInbox(self).run())
                services.create_task(self.run_cabinet_verification())
                services.create_task(self.provider_alerts.run())
        finally:
            self.services = None

    async def close(self) -> None:
        await asyncio.gather(*(service.close() for cabinet in self.cabinets.values()
                               for service in (cabinet.app.account_imports, cabinet.app.pool_checks)),
                             return_exceptions=True)
        for cabinet in self.cabinets.values():
            if cabinet.app.db is not self.db:
                cabinet.app.db.close()

    async def menu(self, uid: int) -> None:
        is_owner = uid == self.cfg.owner_id
        selected = self.registry.selected(uid, self.cfg.owner_id)
        rows = []
        for item in self.registry.workspaces():
            workspace = item['id']
            if self.registry.can_open(workspace, uid, self.cfg.owner_id):
                prefix = '✓' if workspace == selected else '◇'
                command = 'view' if is_owner else 'open'
                label = f'{prefix} {item["name"]}'
                if is_owner:
                    counts = self.cabinets[workspace].app.db.counts()
                    label += f' · {counts["queued"]} / {counts["running"]}'
                rows.append([(label, f'ws:{command}:{workspace}', None)])
        if is_owner:
            rows.append([('＋ Добавить кабинет', 'ws:new', 'success')])
        rows.append([('↶ К текущему кабинету', f'ws:open:{selected}', None)])
        title = '▦ Кабинеты и ключи' if is_owner else '▦ Рабочие пулы'
        hint = 'У каждого кабинета показано число заказов в очереди и в работе.' if is_owner else 'Выбери пул для своих заказов.'
        await self.tg.rich(uid, *screen(title, subtitle=hint), keyboard(*rows))

    async def cabinet_screen(self, uid: int, workspace: str) -> None:
        workspace_info = self.registry.workspace(workspace)
        keys = self.registry.keys(workspace)
        online = len(self.cabinets[workspace].api.clients)
        counts = self.cabinets[workspace].app.db.counts()
        awaiting_verification = online > 0 and not self.can_dispatch(workspace)
        caption = ('Запуск ждёт сверки кабинетов. Проверь, что ключи относятся к разным кабинетам.'
                   if awaiting_verification
                   else f'Лимит кабинета · до {self.cabinets[workspace].app.cfg.max_active_external} задач одновременно.')
        rich, plain = screen('▦ ' + workspace_info['name'], [
            ('Код кабинета', workspace), ('Активных ключей', online),
            ('Заказы в очереди / в работе', f'{counts["queued"]} / {counts["running"]}'),
        ], caption=caption)
        rows = []
        for key in keys:
            label = 'Основной ключ' if key['id'] == 'env' else 'Ключ ' + key['id']
            prefix = '✓' if key['enabled'] else '○'
            rows.append([(f'{prefix} {label}', f'ws:key:{workspace}:{key["id"]}', None)])
        rows.extend([
            [('＋ Добавить ключ', f'ws:addkey:{workspace}', 'success')],
            [('▷ Открыть админку кабинета', f'ws:open:{workspace}', 'primary')],
            [('＋ Подключить трафера', f'ws:member:{workspace}', None)],
            [('↶ Кабинеты', 'ws:menu', None)],
        ])
        await self.tg.rich(uid, rich, plain, keyboard(*rows))

    async def key_screen(self, uid: int, workspace: str, key_id: str) -> None:
        key = next((entry for entry in self.registry.keys(workspace) if entry['id'] == key_id), None)
        if key is None:
            return await self.cabinet_screen(uid, workspace)
        action, label = ('disable', 'Выключить') if key['enabled'] else ('enable', 'Включить')
        rows = [[(('× ' if key['enabled'] else '✓ ') + label, f'ws:confirm:{workspace}:{key_id}:{action}', None)]]
        if key_id != 'env':
            rows.append([('× Удалить из бота', f'ws:confirm:{workspace}:{key_id}:delete', 'danger')])
        rows.append([('↶ К кабинету', f'ws:view:{workspace}', None)])
        title = 'Основной ключ' if key_id == 'env' else 'Ключ ' + key_id
        await self.tg.rich(uid, *screen(title, subtitle='Включён' if key['enabled'] else 'Выключен'), keyboard(*rows))

    def known_cabinet_witnesses(self, workspace: str) -> set[str]:
        known = self.registry.witnesses(workspace)
        store = self.cabinets[workspace].app.db
        known.update(f'task:{row[0]}' for row in store.conn.execute(
            'SELECT upstream_id FROM tasks WHERE upstream_id IS NOT NULL ORDER BY updated_at DESC LIMIT 100'))
        return known

    async def add_key(self, uid: int, workspace: str, token: str) -> None:
        if uid != self.cfg.owner_id:
            return
        if not self.registry.workspace(workspace):
            raise ValueError('Кабинет не найден.')
        if self.registry.duplicate_key(token):
            raise ValueError('Этот ключ уже подключён.')
        candidate = Tokora(self.session, self.cfg.base_url, token, self.cfg.upstream_timeout)
        observed = await cabinet_witnesses(candidate)
        cabinets = self.registry.workspaces()
        known_by_cabinet = {item['id']: self.known_cabinet_witnesses(item['id']) for item in cabinets}
        for item in cabinets:
            if item['id'] != workspace and observed.markers.intersection(known_by_cabinet[item['id']]):
                raise ValueError(f'Ключ относится к кабинету «{item["name"]}». Добавь его туда.')
        known = known_by_cabinet[workspace]
        existing = bool(known or self.registry.keys(workspace))
        target = self.cabinets[workspace].api
        if existing and not observed.markers.intersection(known):
            matches = False
            if target.clients:
                try:
                    current = await cabinet_witnesses(target)
                    matches = await same_cabinet(candidate, observed, target, current) is True
                except ApiError:
                    pass
            if not matches:
                for marker in task_witnesses(known)[:3]:
                    if await has_task_witness(candidate, marker):
                        matches = True
                        break
            if not matches:
                raise ValueError('Не удалось подтвердить общий кабинет. Проверь папку аккаунтов или задачу в кабинете.')
        distinct = []
        for item in cabinets:
            current = item['id']
            if current == workspace:
                continue
            peer = self.cabinets[current].api
            if existing and self.registry.distinct(workspace, current):
                continue
            if not peer.clients:
                if self.registry.keys(current):
                    raise ValueError(f'Для сверки включи действующий ключ кабинета «{item["name"]}».')
                continue
            try:
                peer_evidence = await cabinet_witnesses(peer)
                match = await same_cabinet(candidate, observed, peer, peer_evidence)
            except ApiError:
                match = None
            if match is True:
                raise ValueError(f'Ключ относится к кабинету «{item["name"]}». Добавь его туда.')
            if match is None:
                raise ValueError(f'Не удалось отличить этот кабинет от «{item["name"]}». '
                                 'Проверь доступ обоих ключей и создай папку в одном из кабинетов.')
            distinct.append(current)
        self.registry.store_key(workspace, token, observed.markers, distinct_from=tuple(distinct),
                                new_identity=not existing)
        target.set_keys(self.registry.credentials(workspace))
        self.db.log(uid, 'tokora_key_added', workspace)
        await self.cabinet_screen(uid, workspace)

    async def callback(self, uid: int, query: dict) -> None:
        await self.acknowledge(query)
        _, action, *parts = query.get('data', '').split(':')
        is_owner = uid == self.cfg.owner_id
        if action == 'menu' and not parts:
            return await self.menu(uid)
        if action == 'new' and is_owner and not parts:
            return await self.tg.send(uid, f'Отправь <code>{CREATE_COMMAND} Название</code>.')
        if not parts or not self.registry.can_open(parts[0], uid, self.cfg.owner_id):
            return
        workspace = parts[0]
        if action == 'open' and len(parts) == 1:
            if is_owner:
                self.key_input = None
            app = self.cabinets[workspace].app
            if is_owner:
                await app.admin_dashboard(uid)
            else:
                await app.show_home(uid)
            self.registry.select(uid, workspace)
            return
        if not is_owner:
            return
        if action == 'view' and len(parts) == 1:
            return await self.cabinet_screen(uid, workspace)
        if action == 'addkey' and len(parts) == 1:
            self.key_input = (workspace, time.monotonic() + 600)
            name = self.registry.workspace(workspace)['name']
            return await self.tg.send(uid, f'Вставь ключ доступа для кабинета «{h(name)}» одним сообщением. '
                                      'Нужны права чтения задач и аккаунтов.')
        if action == 'member' and len(parts) == 1:
            return await self.tg.send(uid, f'Отправь <code>{MEMBER_COMMAND} {workspace} ID</code>, '
                                      'затем выдай подписку и назначь аккаунты в админке этого кабинета.')
        if action == 'key' and len(parts) == 2:
            self.key_confirmation = None
            return await self.key_screen(uid, workspace, parts[1])
        if (action == 'confirm' and len(parts) == 3) or (action == 'apply' and len(parts) == 4):
            key_id, change = parts[1:3]
            labels = {'disable': 'Выключить', 'enable': 'Включить', 'delete': 'Удалить'}
            if change not in labels or not any(key['id'] == key_id for key in self.registry.keys(workspace)):
                return
            if key_id == 'env' and change == 'delete':
                return
            if action == 'confirm':
                nonce = token_hex(6)
                self.key_confirmation = ((workspace, key_id, change, nonce), time.monotonic() + 600)
                return await self.tg.send(uid, f'<b>{labels[change]} ключ?</b>', keyboard(
                    [(f'✓ {labels[change]}', f'ws:apply:{workspace}:{key_id}:{change}:{nonce}', 'danger')],
                    [('↶ Отмена', f'ws:key:{workspace}:{key_id}', None)]))
            confirmation = self.key_confirmation
            if not confirmation or confirmation[0] != tuple(parts) or time.monotonic() > confirmation[1]:
                return await self.tg.send(uid, 'Открой карточку ключа и подтверди действие заново.')
            self.key_confirmation = None
            self.registry.change_key(workspace, key_id, change)
            self.cabinets[workspace].api.set_keys(self.registry.credentials(workspace))
            self.db.log(uid, 'tokora_key_' + change, f'{workspace}:{key_id}')
            return await self.cabinet_screen(uid, workspace)

    async def command(self, uid: int, message: dict, command: str, args: str) -> None:
        if command == '/workspaces':
            return await self.menu(uid)
        if uid != self.cfg.owner_id:
            return
        if command == KEY_COMMAND and message.get('message_id'):
            try:
                await self.tg.call('deleteMessage', {'chat_id': uid, 'message_id': message['message_id']}, timeout=5)
            except TelegramError:
                await self.tg.send(uid, 'Telegram не удалил сообщение с ключом. Удали его из чата вручную.')
        try:
            if command == CREATE_COMMAND:
                workspace = self.registry.create_workspace(args)
                self.open_cabinet(self.registry.workspace(workspace))
                self.db.log(uid, 'tokora_workspace_added', workspace)
                return await self.cabinet_screen(uid, workspace)
            if command == KEY_COMMAND:
                parts = args.split()
                if len(parts) != 2 or not KEY_FORMAT.fullmatch(parts[1]):
                    raise ValueError('Открой «Кабинеты и ключи», выбери кабинет и нажми «Добавить ключ».')
                await self.add_key(uid, *parts)
                self.key_input = None
                return
            fields = args.split()
            if len(fields) != 2:
                raise ValueError('Укажи код кабинета и числовой Telegram ID.')
            workspace, member = fields
            if (not self.registry.workspace(workspace) or not member.isascii() or not member.isdecimal()
                    or len(member) > 19 or not 0 < int(member) <= MAX_USER_ID):
                raise ValueError('Укажи код кабинета и числовой Telegram ID.')
            target = int(member)
            self.cabinets[workspace].app.db.register(target)
            self.registry.allow_member(workspace, target)
            self.db.log(uid, 'tokora_workspace_member', f'{workspace}:{target}')
            return await self.tg.send(uid, '✓ Пул доступен участнику через /workspaces.', keyboard(
                [('▷ Открыть админку кабинета', f'ws:open:{workspace}', 'primary')]))
        except ValueError as exc:
            await self.tg.send(uid, h(exc))
        except ApiError as exc:
            self.provider_alerts.request('Подключение кабинета','Проверка ключа','',exc)
            message = 'Не удалось подтвердить ключ. Проверь его права и подключение.'
            if exc.status == 429:
                message = 'Превышена частота запросов. Добавь ключ чуть позже.'
            await self.tg.send(uid, message)

    async def acknowledge(self, query: dict) -> None:
        if query_id := query.get('id'):
            try:
                await self.tg.answer_callback(query_id)
            except TelegramError:
                pass

    async def entry_callback(self, actor: int, profile: dict, query: dict, data: str) -> None:
        await self.acknowledge(query)
        if (data==ENTRY_OPEN and self.admission.can_enter(actor)) or (data==ENTRY_PANEL and actor==self.cfg.owner_id):
            selected = self.registry.selected(actor,self.cfg.owner_id)
            app = self.cabinets[selected].app
            if data==ENTRY_PANEL:
                return await app.admin_dashboard(actor)
            app.db.touch_user(actor,profile.get('first_name',''),profile.get('last_name',''),profile.get('username',''))
            return await app.command(actor,'/start')
        await self.admission.callback(actor,data,profile)

    async def handle_chat_update(self, update: dict) -> None:
        query = update.get('callback_query')
        message = (query.get('message') if query else update.get('message')) or {}
        profile = (query or message).get('from') or {}
        actor = profile.get('id')
        chat = message.get('chat') or {}
        if type(actor) is not int or not 0<actor<=MAX_USER_ID or chat.get('type') != 'private' or chat.get('id') != actor:
            return
        data = str(query.get('data','')) if query else ''
        if query and data.startswith(ENTRY_ROUTE):
            return await self.entry_callback(actor,profile,query,data)
        if not self.admission.can_enter(actor):
            if query:await self.acknowledge(query)
            return await self.admission.status(actor)
        if not query:
            text = message.get('text') or ''
            words = text.split(maxsplit=1)
            if words:
                command = words[0].split('@', 1)[0].lower()
                command = LEGACY_COMMANDS.get(command, command)
                if command == KEY_COMMAND:
                    return await self.command(actor, message, command, words[1].strip() if len(words) == 2 else '')
                if actor == self.cfg.owner_id and (tokens := set(TOKEN_TEXT.findall(text))):
                    pending = self.key_input
                    args = ''
                    if pending and time.monotonic() <= pending[1] and len(tokens) == 1:
                        args = f'{pending[0]} {next(iter(tokens))}'
                    return await self.command(actor, message, KEY_COMMAND, args)
                if command in ('/workspaces', CREATE_COMMAND, MEMBER_COMMAND):
                    return await self.command(actor, message, command, words[1].strip() if len(words) == 2 else '')
        selected = self.registry.selected(actor, self.cfg.owner_id)
        unknown_origin = PRIMARY_WORKSPACE if len(self.cabinets) == 1 else None
        if query:
            if data.startswith('ws:'):
                return await self.callback(actor, query)
            if data.startswith('wc:'):
                route = self.registry.callback_route(data)
                origin, data = route if route else (None, '')
            else:
                origin = self.registry.message_workspace(actor, message.get('message_id', 0)) or unknown_origin
            if data.startswith(ENTRY_ROUTE):
                return await self.entry_callback(actor,profile,query,data)
            if origin != selected:
                try:
                    await self.tg.answer_callback(query['id'], 'Открой экран в текущем пуле через /workspaces.')
                except TelegramError:
                    pass
                return
            if data.startswith('ws:'):
                return await self.callback(actor, {**query, 'data': data})
            update = {**update, 'callback_query': {**query, 'data': data}}
        elif reply := message.get('reply_to_message'):
            if (reply.get('from') or {}).get('id') != actor:
                origin = self.registry.message_workspace(actor, reply.get('message_id', 0)) or unknown_origin
                if origin != selected:
                    return await self.tg.send(actor, 'Открой нужное поле в текущем пуле через /workspaces.')
        await self.cabinets[selected].app.handle_chat_update(update)
