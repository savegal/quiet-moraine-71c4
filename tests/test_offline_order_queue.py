import asyncio
import json
from types import SimpleNamespace

import pytest

from proxima_bot.api import ApiError, Tokora
from proxima_bot.config import Config
from proxima_bot.db import Store
from proxima_bot.presentation import task_rich
from proxima_bot.worker import Worker
from test_extended import approved_order_data, button_callback, new_app, collected_entry, collected_cid, select_order_template
from test_order_flows import COMMENT, REPLY_TARGET, VIDEO_ID
from test_worker import FakeTelegram, FakeTokora


@pytest.mark.parametrize('mode,targets', [
    ('publish', {'videos': ['https://vt.tiktok.com/example/'], 'comments': ['Текст']}),
    ('fetch', {'videos': ['https://vt.tiktok.com/example/'], 'fetch_count': 100}),
    ('reply_links', {'reply_links': [COMMENT], 'replies': ['Ответ']}),
])
def test_confirmation_persists_order_without_any_provider_calls(mode, targets):
    async def run():
        app = new_app()
        calls = []
        async def unavailable(*args, **kwargs):
            calls.append((args, kwargs))
            raise ApiError('job_busy', 400)
        app.api.call = unavailable
        app.api.resolve = unavailable
        try:
            data={'name':'Очередь',**targets}
            if mode in ('publish','reply_links'):
                data=approved_order_data(app,7,mode,data)
            app.save_wizard_step(7, mode, 'review', data)
            await app.preview(7)
            submit = button_callback(app, 'wiz:submit:')
            await app.callback(7, {'data': submit})
            task, = app.db.recent_tasks(7)
            assert task['state'] == 'queued'
            assert app.db.draft(7) is None
            assert calls == []
            assert 'Заказ поставлен в очередь' in app.tg.out[-1][1]
            await app.callback(7, {'data': submit})
            assert len(app.db.recent_tasks(7)) == 1
        finally:
            app.db.close()
    asyncio.run(run())


@pytest.mark.parametrize('links', ['video', 'reply'])
def test_worker_resolves_only_after_slot_is_free_and_retries_busy_resolver(links):
    class Provider(FakeTokora):
        def __init__(self):
            super().__init__()
            self.resolutions = 0
            self.busy = True
        async def resolve(self, link):
            self.resolutions += 1
            if self.busy: raise ApiError('job_busy', 400)
            return VIDEO_ID
        async def call(self, method, path, **kwargs):
            assert path == '/v1/tasks/reply-links/resolve'
            self.resolutions += 1
            if self.busy: raise ApiError('job_busy', 400)
            return {'items': [{'value': REPLY_TARGET}], 'failed': 0}
    async def run():
        db = Store(':memory:')
        try:
            db.grant(7, 30); db.assign(7, 'a')
            raw = {'awemeIds': ['https://vt.tiktok.com/example/']} if links == 'video' else {'replyLinks': [COMMENT]}
            tid = db.queue_task(7, 'Ждём', 'publish', {'assets': {'accountIds': ['a']}, **raw})
            api = Provider(); api.active = [{'id': 'other', 'state': 'running'}]
            worker = Worker(db, api, FakeTelegram(), Config('fake', 'fake', 99, dispatch_enabled=True, max_active_external=1))
            await worker.tick()
            assert api.resolutions == 0 and api.started == []
            api.active = []
            await worker.tick()
            assert db.task(tid)['state'] == 'queued'
            assert db.task(tid)['launch_payload_json'] is None
            assert api.started == []
            api.busy = False; worker.next_dispatch_at = 0
            await worker.tick()
            assert db.task(tid)['state'] == 'running'
            expected = [VIDEO_ID] if links == 'video' else [REPLY_TARGET]
            assert api.started[0]['awemeIds' if links == 'video' else 'replyLinks'] == expected
            frozen = json.loads(db.task(tid)['launch_payload_json'])
            assert frozen == api.started[0]
        finally: db.close()
    asyncio.run(run())


def test_resolved_launch_request_is_not_resolved_again_on_ambiguous_retry():
    class Provider(FakeTokora):
        resolutions = 0
        async def resolve(self, link):
            self.resolutions += 1
            return VIDEO_ID
    async def run():
        db = Store(':memory:')
        try:
            db.grant(7, 30); db.assign(7, 'a')
            tid = db.queue_task(7, 'Повтор', 'publish', {
                'assets': {'accountIds': ['a']}, 'awemeIds': ['https://vt.tiktok.com/example/'],
            })
            api = Provider(); api.issue = 'connection_unavailable'
            worker = Worker(db, api, FakeTelegram(), Config('fake', 'fake', 99, dispatch_enabled=True))
            await worker.tick()
            assert db.task(tid)['state'] == 'reconcile'
            api.issue = None; worker.next_dispatch_at = 0
            await worker.tick()
            assert api.resolutions == 1
            assert api.started[0] == api.started[1]
            assert db.task(tid)['state'] == 'running'
        finally: db.close()
    asyncio.run(run())


@pytest.mark.parametrize('status,code,state,error', [
    (400, 'invalid_request', 'failed', 'invalid_order_targets'),
    (404, 'not_found', 'failed', 'invalid_order_targets'),
    (403, 'api_scope_denied', 'queued', 'admin_action_required'),
    (503, 'service_unavailable', 'queued', 'connection_unavailable'),
])
def test_bad_target_resolution_does_not_block_an_independent_order(status, code, state, error):
    class Provider(FakeTokora):
        async def resolve(self, link):
            raise ApiError(code, status)

    async def run():
        db = Store(':memory:')
        try:
            db.grant(7, 30)
            db.assign(7, 'a')
            db.grant(8, 30)
            db.assign(8, 'b')
            first = db.queue_task(7, 'Короткая ссылка', 'publish', {
                'assets': {'accountIds': ['a']}, 'awemeIds': ['https://vt.tiktok.com/example/'],
            })
            second = db.queue_task(8, 'Известный ID', 'publish', {
                'assets': {'accountIds': ['b']}, 'awemeIds': [VIDEO_ID],
            })
            api = Provider()
            tg = FakeTelegram()
            worker = Worker(db, api, tg, Config('fake', 'fake', 99, dispatch_enabled=True))
            await worker.tick()
            first_order = db.task(first)
            assert first_order['state'] == state
            assert first_order['error_code'] == error
            assert bool(first_order['target_retry_at']) == (state == 'queued')
            assert not api.started
            if status == 403:
                assert tg.messages and tg.messages[-1][0] == 99 and code in tg.messages[-1][1]
            await worker.tick()
            assert db.task(second)['state'] == 'running'
            assert [payload['awemeIds'] for payload in api.started] == [[VIDEO_ID]]
        finally:
            db.close()

    asyncio.run(run())


def test_short_link_retry_survives_restart_and_does_not_block_next_order(tmp_path, monkeypatch):
    clock = [1_800_000_000]
    monkeypatch.setattr('proxima_bot.db.now', lambda: clock[0])
    monkeypatch.setattr('proxima_bot.worker.now', lambda: clock[0])

    class Provider(FakeTokora):
        resolutions = 0

        async def resolve(self, link):
            self.resolutions += 1
            if self.resolutions == 1:
                raise ApiError('short_link_unresolved', 400)
            return VIDEO_ID

        async def start(self, payload):
            self.started.append(payload)
            return {'id': f'job-{len(self.started)}', 'state': 'running'}

    async def run():
        path = str(tmp_path / 'queue.sqlite3')
        cfg = Config('fake', 'fake', 99, dispatch_enabled=True)
        api = Provider()
        with_db = Store(path)
        try:
            with_db.grant(7, 30)
            with_db.assign(7, 'a')
            first = with_db.queue_task(7, 'Короткая ссылка', 'publish', {
                'assets': {'accountIds': ['a']}, 'awemeIds': ['https://vt.tiktok.com/example/'],
            })
            second = with_db.queue_task(7, 'Полная ссылка', 'publish', {
                'assets': {'accountIds': ['a']}, 'awemeIds': [VIDEO_ID],
            })
            await Worker(with_db, api, FakeTelegram(), cfg).tick()
            pending = with_db.task(first)
            assert pending['state'] == 'queued'
            assert pending['target_retry_at'] == clock[0] + 15
            assert 'Бот повторит проверку' in task_rich(pending)
        finally:
            with_db.close()

        db = Store(path)
        try:
            worker = Worker(db, api, FakeTelegram(), cfg)
            await worker.tick()
            assert db.task(second)['state'] == 'running'
            assert api.resolutions == 1
            api.final['job-1'] = {'id': 'job-1', 'state': 'completed', 'finishedAt':'2026-11-16T16:00:00Z'}
            clock[0] += 15
            await worker.tick()
            assert db.task(first)['state'] == 'running'
            assert api.resolutions == 2
            assert api.started[-1]['awemeIds'] == [VIDEO_ID]
        finally:
            db.close()
    asyncio.run(run())


def test_unresolved_short_link_exhausts_retry_budget_without_launch(monkeypatch):
    clock = [1_800_000_000]
    monkeypatch.setattr('proxima_bot.db.now', lambda: clock[0])
    monkeypatch.setattr('proxima_bot.worker.now', lambda: clock[0])

    class Provider(FakeTokora):
        async def resolve(self, link):
            raise ApiError('short_link_unresolved', 400)

    async def run():
        db = Store(':memory:')
        try:
            db.grant(7, 30)
            db.assign(7, 'a')
            tid = db.queue_task(7, 'Короткая ссылка', 'publish', {
                'assets': {'accountIds': ['a']}, 'awemeIds': ['https://vt.tiktok.com/example/'],
            })
            api = Provider()
            worker = Worker(db, api, FakeTelegram(), Config('fake', 'fake', 99, dispatch_enabled=True))
            for elapsed in (0, 15, 30):
                clock[0] += elapsed
                await worker.tick()
            failed = db.task(tid)
            assert failed['state'] == 'failed'
            assert failed['error_code'] == 'short_link_unresolved'
            assert failed['target_resolution_attempts'] == 3
            assert failed['launch_payload_json'] is None
            assert api.started == []
        finally:
            db.close()
    asyncio.run(run())


def test_replace_failed_order_links_opens_existing_settings_without_launch():
    async def run():
        app = new_app()
        try:
            tid = app.db.queue_task(7, 'Сбор', 'fetch', {
                'assets': {'accountIds': ['acc7']}, 'awemeIds': ['https://vt.tiktok.com/example/'],
                'settings': {'fetchCount': 1000, 'includeReplies': True, 'fetchKeywords': ['камера']},
            })
            app.db.transition(tid, 'failed', error_code='short_link_unresolved')
            await app.task(7, tid)
            assert 'Заменить ссылки' in str(app.tg.out[-1][2])
            await app.callback(7, {'data': button_callback(app, 'repeat:')})
            draft = app.db.draft(7)
            assert draft['step'] == 'videos'
            assert draft['data']['fetch_count'] == 1000
            assert draft['data']['fetch_replies'] is True
            assert draft['data']['fetch_keywords'] == ['камера']
            assert len(app.db.recent_tasks(7)) == 1
        finally:
            app.db.close()
    asyncio.run(run())


def test_recover_rejected_collection_reply_preserves_targets_and_settings():
    class Provider(FakeTokora):
        async def call(self, method, path, **kwargs):
            if method=='POST' and path=='/v1/tasks/collection/reply-plan':
                assert kwargs['payload']=={'targetIds':[collected_cid(3),collected_cid(1)],'withPhotos':False}
                return {'selected':2,'videos':1}
            assert method == 'GET' and path == '/v1/tasks/collection/entries'
            return {'items': [collected_entry(i) for i in range(1, 5)], 'more': False}

    async def run():
        app = new_app()
        app.api = Provider()
        try:
            source = app.db.queue_task(7, 'Сбор', 'fetch', {'assets': {'accountIds': ['acc7']}})
            app.db.transition(source, 'completed', upstream_id='collection')
            failed = app.db.queue_task(7, 'Ответы', 'publish', {
                'sourceJob': 'collection', 'replySourceMode': 'all', 'targetIds': ['entry-3', 'entry-1'],
                'assets': {'accountIds': ['acc7'], 'replyTexts': ['Текст из заказа']},
                'settings': {'repliesPerComment': 5, 'likesPerReply': 20},
            })
            app.db.transition(failed, 'failed', error_code='invalid_target')
            await app.callback(8, {'data': 'repeat:' + failed})
            assert app.db.draft(8) is None
            await app.task(7, failed)
            assert 'Проверить и повторить' in str(app.tg.out[-1][2])
            await app.callback(7, {'data': button_callback(app, 'repeat:')})
            assert app.db.draft(7)['step']=='replies'
            await select_order_template(app,7,'source_reply',replies=['Текст из заказа'])
            await app.callback(7,{'data':button_callback(app,'source:skip:')})
            assert app.db.draft(7)['data']['target_cids'] == [collected_cid(3),collected_cid(1)]
            assert 'Текст из заказа' in app.tg.out[-1][1]
            assert '20 минут' in app.tg.out[-1][1]
            assert len(app.db.recent_tasks(7)) == 2
            await app.callback(7, {'data': button_callback(app, 'source:submit:')})
            assert app.api.started == []
            await Worker(app.db, app.api, FakeTelegram(), Config('fake', 'fake', 99, dispatch_enabled=True)).tick()
            sent, = app.api.started
            assert sent['sourceJob']=='collection' and sent['targetIds']==[collected_cid(3),collected_cid(1)]
            assert not {'replyLinks','replySourceMode'} & sent.keys()
            assert sent['assets']['replyTexts'] == ['Текст из заказа']
            assert sent['settings']['repliesPerComment'] == 5
            assert sent['settings']['likesPerReply'] == 20
            assert sent['settings']['repliesPerVideo'] == 10
            assert app.db.task(failed)['state'] == 'failed'
        finally:
            app.db.close()
    asyncio.run(run())


def test_queued_legacy_collection_reply_resolves_exact_entries_before_freezing():
    class Provider(FakeTokora):
        async def call(self, method, path, *, params):
            assert method == 'GET' and path == '/v1/tasks/collection/entries'
            if params['offset'] == 0:
                return {'items': [collected_entry(1), collected_entry(2, kind='reply')], 'more': True}
            assert params['offset'] == 2
            return {'items': [collected_entry(3), collected_entry(4)], 'more': False}

    async def run():
        db = Store(':memory:')
        try:
            db.grant(7, 30)
            db.assign(7, 'a')
            source = db.queue_task(7, 'Сбор', 'fetch', {'assets': {'accountIds': ['a']}})
            db.transition(source, 'completed', upstream_id='collection')
            tid = db.queue_task(7, 'Ответы', 'publish', {
                'sourceJob': 'collection', 'replySourceMode': 'all', 'targetIds': ['entry-4', 'entry-1'],
                'assets': {'accountIds': ['a'], 'replyTexts': ['Ответ']},
            })
            api = Provider()
            await Worker(db, api, FakeTelegram(), Config('fake', 'fake', 99, dispatch_enabled=True)).tick()
            sent, = api.started
            assert sent['sourceJob']=='collection' and sent['targetIds']==[collected_cid(4),collected_cid(1)]
            assert not {'replyLinks','replySourceMode'} & sent.keys()
            assert json.loads(db.task(tid)['launch_payload_json']) == sent
            assert json.loads(db.task(tid)['payload_json'])['targetIds'] == ['entry-4', 'entry-1']
        finally:
            db.close()
    asyncio.run(run())


@pytest.mark.parametrize('body', [{'error': 'job_busy'}, {'code': 'job_busy'}, {'error': {'code': 'job_busy'}}])
def test_error_code_is_read_from_supported_response_shapes(body):
    api = Tokora(None, 'https://example.invalid', 'fake')
    error = api.response_error(SimpleNamespace(status=400, headers={'X-Request-Id': 'request-1'}), body, 'upstream_error')
    assert error.code == 'job_busy'
    assert error.request_id == 'request-1'
