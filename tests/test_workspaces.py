import asyncio
import re
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from proxima_bot.advanced_db import ExtendedStore
from proxima_bot.api import ApiError, TelegramError
from proxima_bot.config import Config
from proxima_bot.manage import cli_cabinet
from proxima_bot.presentation import keyboard
from proxima_bot.rich_ui import button_row
from proxima_bot.updates import UpdateInbox
from proxima_bot.workspace_api import CabinetAPI, CabinetEvidence, CabinetKey, cabinet_witnesses, same_cabinet
from proxima_bot.workspace_app import WorkspaceApp
from proxima_bot.workspace_registry import WorkspaceRegistry, workspace_database
from test_worker import FakeTokora


KEY_ONE = 'tk_test_first_key_12345'
KEY_TWO = 'tk_test_second_key_12345'
KEY_OTHER = 'tk_test_other_key_12345'


class Transport:
    def __init__(self):
        self.calls = []

    async def call(self, method, data, *, timeout=25):
        self.calls.append((method, data))
        if method in ('sendMessage', 'sendRichMessage', 'editMessageText'):
            return {'message_id': data.get('message_id', len(self.calls))}
        return True

    async def send(self, uid, text, markup=None):
        return await self.call('sendMessage', {'chat_id': uid, 'text': text, 'reply_markup': markup})

    async def rich(self, uid, rich, plain, markup=None):
        return await self.call('sendRichMessage', {'chat_id': uid, 'html': rich, 'reply_markup': markup})

    async def answer_callback(self, query_id, text=''):
        return await self.call('answerCallbackQuery', {'id': query_id, 'text': text})


def make_app(path=':memory:', legacy=''):
    config = Config('fake', legacy, 99, database_path=path, dispatch_enabled=True)
    return WorkspaceApp(config, ExtendedStore(path), Transport(), None)


def message(update_id, uid, text):
    return {'update_id': update_id, 'message': {'message_id': update_id, 'text': text,
            'chat': {'type': 'private', 'id': uid}, 'from': {'id': uid}}}


def query(uid, message_id, data):
    return {'callback_query': {'id': 'press', 'from': {'id': uid}, 'data': data,
            'message': {'message_id': message_id, 'chat': {'type': 'private', 'id': uid}}}}


def test_key_enrollment_through_inbox_keeps_secret_out_of_sqlite_and_checks_cabinet(monkeypatch, tmp_path):
    async def witnesses(api):
        key = next(iter(api.clients.values())).key if isinstance(api, CabinetAPI) else api.key
        marker = 'folder:abba0002:123' if key == KEY_OTHER else 'folder:abba0001:123'
        return CabinetEvidence(frozenset({marker}), True)

    monkeypatch.setattr('proxima_bot.workspace_app.cabinet_witnesses', witnesses)
    monkeypatch.setattr('proxima_bot.workspace_api.cabinet_witnesses', witnesses)
    path = str(tmp_path / 'orders.sqlite3')

    async def run():
        app = make_app(path, KEY_ONE)
        try:
            await app.handle_chat_update(message(1, 99, '/tokora_workspace Второй кабинет'))
            other = app.registry.workspaces()[1]['id']
            await app.handle_chat_update(query(99, 1, 'ws:addkey:main'))
            inbox = UpdateInbox(app)
            inbox.persist([message(2, 99, f'Ключ: `{KEY_TWO}`'),
                           message(3, 7, '/tokora_key main ' + KEY_OTHER),
                           message(30, 8, 'tk_regular_order_material')])
            rows = inbox.ready(2)
            assert all(KEY_TWO not in row['payload'] and KEY_OTHER not in row['payload'] for row in rows)
            assert 'tk_regular_order_material' in inbox.ready(3)[2]['payload']
            for row in rows:
                await inbox.process(row)
            assert ('deleteMessage', {'chat_id': 99, 'message_id': 2}) in app.tg.calls
            assert set(app.registry.credentials('main').values()) == {KEY_ONE, KEY_TWO}
            assert not app.registry.credentials(other)
            await app.handle_chat_update(message(4, 99, '/tokora_key ' + other + ' ' + KEY_OTHER))
            assert list(app.registry.credentials(other).values()) == [KEY_OTHER]
            assert all(KEY_TWO not in str(payload) and KEY_OTHER not in str(payload) for _, payload in app.tg.calls)
            with pytest.raises(ValueError, match='относится к кабинету'):
                await app.add_key(99, other, 'tk_test_foreign_keys_12345')
            db_secret = app.db.conn.execute('SELECT secret FROM tokora_keys WHERE workspace=?', (other,)).fetchone()[0]
            assert KEY_OTHER.encode() not in db_secret
        finally:
            await app.close()
            app.db.close()
        with closing(ExtendedStore(path)) as store:
            recovered = WorkspaceRegistry(store.conn, path)
            assert set(recovered.credentials('main').values()) == {KEY_TWO}
            assert list(recovered.credentials(other).values()) == [KEY_OTHER]
            assert Path(workspace_database(path, other)).is_file()
            assert recovered.distinct('main', other)
        monkeypatch.setenv('DATABASE_PATH', path)
        cli_path, keys = cli_cabinet(other, '')
        assert cli_path == workspace_database(path, other)
        assert list(keys.values()) == [KEY_OTHER]

    asyncio.run(run())


def test_stale_callback_and_cross_cabinet_route_are_rejected():
    async def run():
        app = make_app()
        try:
            await app.handle_chat_update(message(1, 99, '/tokora_workspace Восток'))
            other = app.registry.workspaces()[1]['id']
            accessed = []

            async def default(update):
                accessed.append('main')

            async def secondary(update):
                accessed.append(other)

            app.cabinets['main'].app.handle_chat_update = default
            app.cabinets[other].app.handle_chat_update = secondary
            await app.cabinets['main'].app.tg.send(7, 'old primary screen')
            old_id = len(app.tg.calls)
            await app.handle_chat_update(query(7, 5, f'ws:open:{other}'))
            assert app.registry.selected(7, 99) == 'main'
            await app.handle_chat_update(message(2, 99, f'/tokora_member {other} 7'))
            await app.handle_chat_update(query(7, 5, f'ws:open:{other}'))
            assert app.registry.selected(7, 99) == other
            await app.handle_chat_update(query(7, old_id, 'task:aaaaaaaa'))
            assert not accessed
            await app.cabinets[other].app.tg.send(7, 'screen')
            fresh_id = len(app.tg.calls)
            await app.handle_chat_update(query(7, fresh_id, 'task:aaaaaaaa'))
            assert accessed == [other]
            assert app.cabinets[other].app.db.user(7) and not app.db.user(7)
        finally:
            await app.close()
            app.db.close()

    asyncio.run(run())


def test_cabinets_fill_two_slots_each_independently():
    class Executor(FakeTokora):
        async def start(self, payload):
            self.started.append(payload)
            result = {'id': f'job-{len(self.started)}', 'state': 'running'}
            self.active.append(result)
            return result

    async def run():
        app = make_app()
        try:
            await app.handle_chat_update(message(1, 99, '/tokora_workspace Восток'))
            for cabinet in app.cabinets.values():
                cabinet.worker.tokora = Executor()
                for user in (7, 8, 9):
                    cabinet.app.db.grant(user, 10)
                    cabinet.app.db.assign(user, f'account-{user}')
                    cabinet.app.db.queue_task(user, 'Сбор', 'fetch', {
                        'assets': {'accountIds': [f'account-{user}']},
                        'awemeIds': ['7420000000000000000'],
                    })
            for _ in range(3):
                await asyncio.gather(*(cabinet.worker.tick() for cabinet in app.cabinets.values()))
            assert [len(cabinet.app.db.running()) for cabinet in app.cabinets.values()] == [2, 2]
            assert [cabinet.app.db.counts()['queued'] for cabinet in app.cabinets.values()] == [1, 1]
        finally:
            await app.close()
            app.db.close()

    asyncio.run(run())


def test_key_rotation_and_rejections_never_replay_an_ambiguous_write(monkeypatch):
    calls = []
    errors = {}

    async def request(self, method, path, **kwargs):
        calls.append(self.key)
        if self.key in errors:
            raise errors[self.key]
        return {'key': self.key}

    monkeypatch.setattr(CabinetKey, 'call', request)

    async def run():
        api = CabinetAPI(None, 'https://example.invalid', 5)
        api.set_keys({'a': KEY_ONE, 'b': KEY_TWO})
        assert [(await api.call('GET', '/v1/tasks'))['key'] for _ in range(2)] == [KEY_ONE, KEY_TWO]
        errors[KEY_ONE] = ApiError('rate_limited', 429, rate_scope='key')
        assert (await api.call('POST', '/v1/tasks'))['key'] == KEY_TWO
        for error in (ApiError('connection_unavailable'),
                      ApiError('rate_limited', 429, rate_scope='account')):
            errors[KEY_ONE] = error
            api.next_key = 0
            calls.clear()
            with pytest.raises(ApiError) as caught:
                await api.call('POST', '/v1/tasks')
            assert caught.value is error
            assert calls == [KEY_ONE]

    asyncio.run(run())


def test_changing_legacy_environment_key_cannot_switch_existing_cabinet(tmp_path):
    path = str(tmp_path / 'orders.sqlite3')
    with closing(ExtendedStore(path)) as db:
        assert WorkspaceRegistry(db.conn, path, KEY_ONE).credentials('main') == {'env': KEY_ONE}
    with closing(ExtendedStore(path)) as db:
        assert WorkspaceRegistry(db.conn, path, KEY_OTHER).credentials('main') == {}


def test_cancelled_key_confirmation_cannot_be_reused():
    async def run():
        app = make_app(legacy=KEY_ONE)
        try:
            await app.handle_chat_update(query(99, 1, 'ws:confirm:main:env:disable'))
            rows = app.tg.calls[-1][1]['reply_markup']['inline_keyboard']
            apply, cancel = [row[0]['callback_data'] for row in rows]
            await app.handle_chat_update(query(7, 1, apply))
            assert 'env' in app.cabinets['main'].api.clients
            await app.handle_chat_update(query(99, 1, cancel))
            await app.handle_chat_update(query(99, 1, apply))
            assert 'env' in app.cabinets['main'].api.clients
            await app.handle_chat_update(query(99, 1, 'ws:confirm:main:env:disable'))
            apply = app.tg.calls[-1][1]['reply_markup']['inline_keyboard'][0][0]['callback_data']
            await app.handle_chat_update(query(99, 1, apply))
            await app.handle_chat_update(query(99, 1, apply))
            assert not app.cabinets['main'].api.clients
            assert [entry['action'] for entry in app.db.audit_entries()] == ['tokora_key_disable']
        finally:
            await app.close()
            app.db.close()

    asyncio.run(run())


def test_empty_cabinets_cannot_be_added_twice(monkeypatch):
    async def empty(api):
        return CabinetEvidence(frozenset(), True)

    monkeypatch.setattr('proxima_bot.workspace_app.cabinet_witnesses', empty)

    async def run():
        app = make_app()
        try:
            await app.handle_chat_update(message(1, 99, '/tokora_workspace Восток'))
            other = app.registry.workspaces()[1]['id']
            await app.add_key(99, 'main', KEY_ONE)
            with pytest.raises(ValueError, match='Не удалось отличить'):
                await app.add_key(99, other, KEY_TWO)
            assert not app.registry.credentials(other)
            assert app.can_dispatch('main')
        finally:
            await app.close()
            app.db.close()

    asyncio.run(run())


def test_old_duplicate_registrations_wait_for_verification_before_launching(monkeypatch):
    folder_visible = False

    async def witnesses(api):
        markers = {'folder:abba0001:123'} if folder_visible and 'first' in api.clients else set()
        return CabinetEvidence(frozenset(markers), True)

    monkeypatch.setattr('proxima_bot.workspace_app.cabinet_witnesses', witnesses)
    monkeypatch.setattr('proxima_bot.workspace_api.cabinet_witnesses', witnesses)

    async def run():
        nonlocal folder_visible
        app = make_app()
        try:
            await app.handle_chat_update(message(1, 99, '/tokora_workspace Восток'))
            other = app.registry.workspaces()[1]['id']
            # Pre-upgrade workspaces could already have active keys without verified separation.
            app.cabinets['main'].api.set_keys({'first': KEY_ONE})
            app.cabinets[other].api.set_keys({'second': KEY_TWO})
            for cabinet in app.cabinets.values():
                cabinet.worker.tokora = FakeTokora()
                cabinet.app.db.grant(7, 10)
                cabinet.app.db.assign(7, 'account-7')
                cabinet.app.db.queue_task(7, 'Сбор', 'fetch', {'assets': {'accountIds': ['account-7']}})

            await app.verify_existing_cabinets()
            await asyncio.gather(*(cabinet.worker.tick() for cabinet in app.cabinets.values()))
            assert all(not cabinet.worker.tokora.started for cabinet in app.cabinets.values())
            assert not app.registry.distinct('main', other)

            folder_visible = True
            await app.verify_existing_cabinets()
            assert app.registry.distinct('main', other)
            await asyncio.gather(*(cabinet.worker.tick() for cabinet in app.cabinets.values()))
            assert all(len(cabinet.worker.tokora.started) == 1 for cabinet in app.cabinets.values())
        finally:
            await app.close()
            app.db.close()

    asyncio.run(run())


def test_exact_task_probe_distinguishes_cabinets_with_different_recent_pages():
    async def run():
        left_evidence = CabinetEvidence(frozenset({'task:job-left'}), False)
        right_evidence = CabinetEvidence(frozenset({'task:job-right'}), False)
        left = SimpleNamespace(task=AsyncMock(return_value={'id': 'job-left'}))
        right = SimpleNamespace(task=AsyncMock(return_value={'id': 'job-left'}))
        assert await same_cabinet(left, left_evidence, right, right_evidence) is True
        right.task.side_effect = ApiError('job_not_found', 404)
        assert await same_cabinet(left, left_evidence, right, right_evidence) is False
        left.task.assert_awaited_once_with('job-left')

    asyncio.run(run())


def test_folder_evidence_uses_availability_and_can_include_unassigned_accounts():
    async def run():
        folders = {'available': True, 'items': [], 'total': 50, 'unassigned': 50}
        api = SimpleNamespace(call=AsyncMock(return_value=folders),
                              task_page=AsyncMock(return_value={'items': []}))
        assert (await cabinet_witnesses(api)).complete_folders
        folders['available'] = False
        assert not (await cabinet_witnesses(api)).complete_folders

    asyncio.run(run())


def test_folder_created_between_reads_does_not_create_a_false_cabinet_split(monkeypatch):
    async def run():
        left, right = object(), object()
        empty = CabinetEvidence(frozenset(), True)
        shared = CabinetEvidence(frozenset({'folder:abba0001:123'}), True)
        witnesses = AsyncMock(return_value=shared)
        monkeypatch.setattr('proxima_bot.workspace_api.cabinet_witnesses', witnesses)
        assert await same_cabinet(left, shared, right, empty) is True
        assert witnesses.await_args_list[0].args == (right,)

    asyncio.run(run())


def test_delivered_button_remains_bound_when_telegram_response_is_lost():
    async def run():
        app = make_app()
        try:
            await app.handle_chat_update(message(1, 99, '/tokora_workspace Восток'))
            other = app.registry.workspaces()[1]['id']
            transport = app.tg.call

            async def lose_response(method, data, *, timeout=25):
                response = await transport(method, data, timeout=timeout)
                if method == 'sendRichMessage':
                    raise TelegramError(503)
                return response

            app.tg.call = lose_response
            with pytest.raises(TelegramError):
                await app.handle_chat_update(query(99, 1, f'ws:open:{other}'))
            data = app.tg.calls[-1][1]
            callback = data['reply_markup']['inline_keyboard'][0][1]['callback_data']
            lost_message = len(app.tg.calls)
            assert app.registry.message_workspace(99, lost_message) is None
            assert app.registry.callback_route(callback) == (other, 'adm:qpause:on')
            assert app.registry.selected(99, 99) == 'main'
            app.tg.call = transport
            await app.handle_chat_update(query(99, 1, 'ws:open:main'))
            await app.handle_chat_update(query(99, lost_message, callback))
            await app.handle_chat_update(query(99, lost_message, 'adm:qpause:on'))
            assert not app.db.dispatch_paused()
            assert not app.cabinets[other].app.db.dispatch_paused()
            await app.handle_chat_update(query(99, 1, f'ws:open:{other}'))
            await app.handle_chat_update(query(99, lost_message, callback))
            assert app.cabinets[other].app.db.dispatch_paused()
        finally:
            await app.close()
            app.db.close()

    asyncio.run(run())


def test_native_rich_and_inline_buttons_use_the_same_cabinet_route():
    async def run():
        app = make_app()
        try:
            markup = keyboard([('Открыть', 'task:long&value', None)])
            html = (button_row(('Открыть', 'task:long&value', None))
                    + '<tg-button type="copy_text" text="example">Copy</tg-button>')
            await app.cabinets['main'].app.tg.rich(99, html, 'Открыть', markup)
            payload = app.tg.calls[-1][1]
            encoded = payload['reply_markup']['inline_keyboard'][0][0]['callback_data']
            rich = payload['rich_message']['html']
            assert re.search(r'data="([^"]+)"', rich)[1] == encoded
            assert app.registry.callback_route(encoded) == ('main', 'task:long&value')
            assert 'type="copy_text" text="example"' in rich
            assert markup['inline_keyboard'][0][0]['callback_data'] == 'task:long&value'
        finally:
            await app.close()
            app.db.close()

    asyncio.run(run())


def test_reply_to_a_foreign_prompt_cannot_change_current_cabinet_settings():
    async def run():
        app = make_app()
        try:
            await app.handle_chat_update(message(1, 99, '/tokora_workspace Восток'))
            other = app.registry.workspaces()[1]['id']
            primary = await app.cabinets['main'].app.tg.send(99, 'Введи число минут')
            await app.handle_chat_update(query(99, 1, f'ws:open:{other}'))
            secondary = app.cabinets[other].app
            secondary.db.save_draft(99, 'owner_edit', 'settlement', {})
            secondary.api.call = AsyncMock(return_value={})
            reply = message(2, 99, '45')
            reply['message']['reply_to_message'] = {'message_id': primary['message_id']}
            await app.handle_chat_update(reply)
            secondary.api.call.assert_not_awaited()
            assert secondary.db.draft(99)['step'] == 'settlement'
            current = await secondary.tg.send(99, 'Введи число минут')
            reply['message']['reply_to_message']['message_id'] = current['message_id']
            await app.handle_chat_update(reply)
            secondary.api.call.assert_awaited_once_with('PATCH', '/v1/settings', payload={'settleMinutes': 45}, retries=0)
        finally:
            await app.close()
            app.db.close()

    asyncio.run(run())


def test_cabinet_cooldown_does_not_consume_the_key_quota():
    async def run():
        api = CabinetAPI(None, 'https://example.invalid', 5)
        api.set_keys({'first': KEY_ONE})
        client = api.clients['first']
        api.budget.blocked_until = float('inf')
        for _ in range(100):
            with pytest.raises(ApiError) as caught:
                await client.reserve_request(background=True)
            assert caught.value.rate_scope == 'account'
        assert not client._request_times and not api.budget.requests
        api.budget.blocked_until = 0
        await client.reserve_request(background=True)
        assert len(client._request_times) == len(api.budget.requests) == 1

    asyncio.run(run())
