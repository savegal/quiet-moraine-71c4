import asyncio
from datetime import datetime, timedelta
import json

import pytest

from proxima_bot.api import TelegramError
from proxima_bot.catalog import MODES
from proxima_bot.config import Config
from proxima_bot.operations import load_spec, valid_body
from proxima_bot.worker import Worker

from test_extended import FakeAPI, button_callback, new_app
from test_worker import FakeTokora


VIDEO_ID = '7420000000000000000'
COMMENT_ID = '7520000000000000000'
VIDEO = f'https://www.tiktok.com/@author/video/{VIDEO_ID}'
COMMENT = f'{VIDEO}?comment_id={COMMENT_ID}'
REPLY_TARGET = f'{VIDEO_ID}:{COMMENT_ID}'
OWNER = 99
MEMBER = 7
SCENARIOS = {
    'publish': ([VIDEO, 'Текст комментария', 'Текст ответа'], {
        'awemeIds': [VIDEO_ID],
        'assets': {'commentTexts': ['Текст комментария'], 'replyTexts': ['Текст ответа']},
    }),
    'reply_links': ([COMMENT, 'Текст ответа'], {
        'replyLinks': [REPLY_TARGET], 'assets': {'replyTexts': ['Текст ответа']},
    }),
    'fetch': (['https://vt.tiktok.com/example/'], {'awemeIds': [VIDEO_ID]}),
    'commentlike': ([COMMENT, '3'], {'likeLinks': [COMMENT], 'likesPerComment': 3}),
    'keyword_reply': (['Тестовая фраза', 'Текст ответа'], {
        'settings': {'fetchKeywords': ['Тестовая фраза']}, 'assets': {'replyTexts': ['Текст ответа']},
    }),
    'feedwarm': (['{"keywords":["обзор"],"search":true,"dryRun":true,"accounts":1}'], {
        'feedWarm': {'keywords': ['обзор'], 'search': True, 'dryRun': True, 'accounts': 1},
    }),
    'usernames': ([VIDEO_ID], {'awemeIds': [VIDEO_ID]}),
    'check': ([], {}),
}


class OrderProvider(FakeTokora, FakeAPI):
    def __init__(self):
        FakeTokora.__init__(self)
        FakeAPI.__init__(self)

    async def call(self, method, path, **kwargs):
        if path.endswith('/reply-links/resolve'):
            self.calls.append((method, path, kwargs))
            return {'items': [{'value': REPLY_TARGET}], 'ok': 1, 'failed': 0}
        if path.endswith('/entries'):
            return {'items': [{'kind': 'video', 'outcome': 'live', 'author': 'public.user',
                               'url': VIDEO, 'text': 'Результат'}], 'total': 1, 'more': False}
        return await super().call(method, path, **kwargs)

    async def publication(self, upstream_id):
        return {'comments': {'published': 1, 'attempts': 1}, 'likes': {'confirmed': 3, 'total': 3}}


async def click(app, uid, prefix):
    await app.callback(uid, {'data': button_callback(app, prefix)})


@pytest.mark.parametrize('mode', MODES)
def test_each_order_from_menu_through_template_dispatch_and_results(mode):
    async def run():
        app = new_app()
        app.api = OrderProvider()
        app.cfg = Config('fake', 'fake', OWNER, dispatch_enabled=True)
        uid = OWNER if mode == 'check' else MEMBER
        if uid == OWNER:
            await app.create(MEMBER)
            assert not any(b['callback_data'] == 'new:check'
                           for row in app.tg.out[-1][2]['inline_keyboard'] for b in row)
            await app.callback(MEMBER, {'data': 'new:check'})
            assert app.db.draft(MEMBER) is None
            app.db.assign(OWNER, 'owner-account')

        await app.create(uid)
        await click(app, uid, 'new:' + mode)
        inputs, expected = SCENARIOS[mode]
        await app.input_draft(uid, 'Заказ ' + mode)
        for value in inputs:
            await app.input_draft(uid, value)
        await click(app, uid, 'wiz:skip:')
        await click(app, uid, 'wiz:skip:')
        assert app.db.draft(uid)['step'] == 'review'
        assert 'Проверка заказа' in app.tg.out[-1][1]

        await click(app, uid, 'wiz:template:')
        template, = app.db.templates(uid)
        assert template['mode'] == mode
        assert app.db.template(8, template['template_id']) is None
        await click(app, uid, 'templates')
        await click(app, uid, 'tpl:')
        await click(app, uid, 'tpl_use:')
        assert not app.db.recent_tasks(uid)
        confirm = button_callback(app, 'wiz:submit:')
        await app.callback(uid, {'data': confirm})
        await app.callback(uid, {'data': confirm})
        task, = app.db.recent_tasks(uid)
        assert task['state'] == 'queued' and app.db.draft(uid) is None

        worker = Worker(app.db, app.api, app.tg, app.cfg)
        await worker.tick()
        sent, = app.api.started
        assert valid_body(load_spec()['components']['schemas']['TaskStart'], sent)
        assert sent['kind'] == ('publish' if mode == 'reply_links' else mode)
        assert sent['assets']['accountIds'] == app.db.account_ids(uid)
        for key, value in expected.items():
            if key == 'assets':
                assert {field: sent[key][field] for field in value} == value
            else:
                assert sent[key] == value
        app.api.final['job1'] = {'id': 'job1', 'state': 'completed', 'done': 1, 'total': 1, 'liked': 3}
        await worker.tick()
        await app.task(uid, task['task_id'])
        assert 'Завершено' in app.tg.out[-1][1]
        await click(app, uid, 'entries:')
        assert 'Результаты' in app.tg.out[-1][1]
        if mode == 'usernames':
            assert '@public.user' in app.tg.out[-1][1]
            await app.export_file(uid, task['task_id'], 'csv')
            assert b'public.user' in app.tg.documents[-1][1]
        if mode == 'feedwarm':
            assert VIDEO in app.tg.out[-1][1]
        if mode in ('publish', 'reply_links', 'keyword_reply', 'commentlike'):
            await click(app, uid, 'report:')
            assert 'Отчёт' in app.tg.out[-1][1]
        else:
            await click(app, uid, 'exports:')
            await click(app, uid, 'export_csv:')
            assert b'entry' not in app.tg.documents[-1][1]
            assert VIDEO.encode() in app.tg.documents[-1][1]
        app.db.close()

    asyncio.run(run())


def test_source_template_selects_own_collection_and_excludes_old_source():
    async def run():
        app = new_app()
        old = app.db.queue_task(MEMBER, 'Первый сбор', 'fetch', {'assets': {'accountIds': ['acc7']}})
        new = app.db.queue_task(MEMBER, 'Второй сбор', 'fetch', {'assets': {'accountIds': ['acc7']}})
        other = app.db.queue_task(8, 'Чужой сбор', 'fetch', {'assets': {'accountIds': ['acc8']}})
        for task_id, provider_id in [(old, 'old'), (new, 'new'), (other, 'other')]:
            app.db.transition(task_id, 'completed', upstream_id=provider_id)
        await app.callback(MEMBER, {'data': 'source_reply:' + old})
        await app.input_draft(MEMBER, 'Сохранённый текст')
        await click(app, MEMBER, 'source:skip:')
        await click(app, MEMBER, 'source:template:')
        template, = app.db.templates(MEMBER)
        tid = template['template_id']
        assert json.loads(app.db.template(MEMBER, tid)['payload']) == {'options': {}, 'replies': ['Сохранённый текст']}
        await app.from_template(MEMBER, tid)
        assert 'source_tpl_use:' + tid + ':' + new == button_callback(app, 'source_tpl_use:')
        await app.callback(MEMBER, {'data': f'source_tpl_use:{tid}:{other}'})
        assert 'недоступен' in app.tg.out[-1][1]
        await app.callback(MEMBER, {'data': f'source_tpl_use:{tid}:{new}'})
        confirm = button_callback(app, 'source:submit:')
        await app.callback(MEMBER, {'data': confirm})
        await app.callback(MEMBER, {'data': confirm})
        payload = json.loads(app.db.recent_tasks(MEMBER)[0]['payload_json'])
        assert len(app.db.recent_tasks(MEMBER)) == 3
        assert payload['sourceJob'] == 'new'
        assert payload['assets']['replyTexts'] == ['Сохранённый текст']
        app.db.close()

    asyncio.run(run())


def test_template_last_page_is_accessible_and_deletes_its_own_template():
    async def run():
        app = new_app()
        ids = [app.db.save_template(MEMBER, f'Шаблон {n}', 'fetch', {}) for n in range(50)]
        await app.templates(MEMBER)
        for page in range(1, 5):
            await click(app, MEMBER, 'templates_page:' + str(page))
        await click(app, MEMBER, 'tpl:' + ids[0])
        await click(app, MEMBER, 'tpl_delete_confirm:')
        await click(app, MEMBER, 'tpl_delete:')
        assert app.db.template(MEMBER, ids[0]) is None
        assert len(app.db.templates(MEMBER)) == 49
        app.db.close()

    asyncio.run(run())


def test_scheduled_order_waits_until_its_selected_time(monkeypatch):
    async def run():
        app = new_app()
        api = OrderProvider()
        await app.begin(MEMBER, 'fetch')
        await app.input_draft(MEMBER, 'Запланированный сбор')
        await app.input_draft(MEMBER, VIDEO_ID)
        await click(app, MEMBER, 'wiz:skip:')
        when = datetime.now(app.schedule_zone) + timedelta(hours=3)
        await app.input_draft(MEMBER, when.strftime('%d.%m.%Y %H:%M'))
        await click(app, MEMBER, 'wiz:submit:')
        task, = app.db.recent_tasks(MEMBER)
        assert task['fire_at'] == int(when.replace(second=0, microsecond=0).timestamp())
        worker = Worker(app.db, api, app.tg, Config('fake', 'fake', OWNER, dispatch_enabled=True))
        await worker.tick()
        assert not api.started
        monkeypatch.setattr('proxima_bot.db.now', lambda: task['fire_at'])
        await worker.tick()
        assert len(api.started) == 1
        app.db.close()

    asyncio.run(run())


@pytest.mark.parametrize('mode', ['reply', 'like'])
def test_topup_only_launches_once_for_each_confirmation(mode):
    async def run():
        app = new_app()
        api = OrderProvider()
        app.api = api
        source = app.db.queue_task(MEMBER, 'Публикация', 'publish',
                                   {'assets': {'accountIds': ['acc7'], 'replyTexts': ['Ответ']}})
        app.db.transition(source, 'completed', upstream_id='source')

        async def topup_info(method, path, **kwargs):
            assert method == 'GET' and path == f'/v1/tasks/source/{mode}-topup'
            return {'available': True, 'likesPerItem': 0, 'likesPerReply': 2,
                    'failedComments': 1, 'repliesPerComment': 1}

        api.call = topup_info
        await app.task(MEMBER, source)
        await click(app, MEMBER, mode + 'info:')
        confirm = button_callback(app, 'topup:')
        await app.callback(MEMBER, {'data': confirm})
        worker = Worker(app.db, api, app.tg, Config('fake', 'fake', OWNER, dispatch_enabled=True))
        await worker.tick()
        sent, = api.started
        if mode == 'like':
            assert sent['likeSourceJob'] == 'source' and sent['settings']['likesPerReply'] == 2
        else:
            assert sent['sourceJob'] == 'source' and sent['assets']['replyTexts'] == ['Ответ']
        api.final['job1'] = {'id': 'job1', 'state': 'completed'}
        await worker.tick()
        await app.callback(MEMBER, {'data': confirm})
        assert 'Подтверждение устарело' in app.tg.out[-1][1]
        assert len(app.db.recent_tasks(MEMBER)) == 2
        app.db.close()

    asyncio.run(run())


@pytest.mark.parametrize(('kind','mime'), [('video','application/octet-stream'), ('photo','image/png'),
                                          ('photo','image/webp')])
def test_owner_upload_caption_and_publish_plan(kind,mime):
    async def run():
        app = new_app()
        calls = []
        description = {}
        plan_state = 'checking'
        draft_path = '/v1/posts/drafts/media'

        async def fetch_upload(file_id, limit):
            if kind == 'video':
                return b'\x00\x00\x00\x14ftypisom0000', 'file.mp4'
            if mime == 'image/webp':
                return b'RIFF\x12\x00\x00\x00WEBPVP8L\x00', 'file.webp'
            return b'\x89PNG\r\n\x1a\n' + b'png data', 'file.png'

        async def upload(path, content, content_type='application/octet-stream', *, params=None):
            calls.append(('UPLOAD', path))
            assert content_type == mime
            if kind == 'video':
                assert path == '/v1/posts/drafts/upload' and params == {'kind': 'video', 'name': 'test..mp4'}
            else:
                assert path == draft_path + '/photos'
            return {'id': 'media', 'kind': kind}

        async def provider(method, path, **kwargs):
            nonlocal description
            calls.append((method, path))
            if path == '/v1/posts/drafts' and method == 'POST':
                assert kwargs['payload'] == {'kind': 'photo', 'name': 'Альбом'}
                return {'id': 'media', 'kind': 'photo'}
            if path == '/v1/posts/templates':
                if method == 'POST':
                    assert kwargs['payload']['name'] == 'Тест'
                    return {'id': 'caption'}
                return {'items': [{'id': 'caption', 'name': 'Тест', 'description': 'Текст', 'hashtags': '#tag'}]}
            if path == draft_path:
                if method == 'PATCH':
                    description = kwargs['payload']
                return {'id': 'media', 'kind': kind, **description}
            if path == '/v1/posts/plan':
                assert kwargs['payload'] == {'draftIds': ['media']}
                return {'id': 'plan', 'state': plan_state, 'postsTotal': 1}
            if path == '/v1/posts/plan/plan':
                return {'id': 'plan', 'state': plan_state, 'postsTotal': 1}
            if path == '/v1/posts/plan/plan/launch':
                return {'planId': 'plan', 'queued': 1}
            if path == '/v1/posts':
                return {'items': [{'id': 'published_item', 'outcome': 'queued'}], 'total': 1}
            raise AssertionError(path)

        app.api.call = provider
        app.api.upload = upload
        app.tg.fetch_upload = fetch_upload
        await app.callback(MEMBER, {'data': 'draft_plan:media'})
        assert calls == []
        await app.posts(OWNER)
        if kind == 'video':
            await click(app, OWNER, 'upload:video')
            await app.handle_upload(OWNER, {'document': {'file_id': 'video', 'file_name': 'test..mp4'}})
        else:
            await click(app, OWNER, 'owner_edit:album')
            await app.input_draft(OWNER, 'Альбом')
            await click(app, OWNER, 'upload:album_')
            await app.handle_upload(OWNER, {'photo': [{'file_id': 'photo'}]})
        await app.callback(OWNER, {'data': 'owner_edit:post_template'})
        await app.input_draft(OWNER, '{"name":"Тест","description":"Текст","hashtags":"#tag"}')
        await app.callback(OWNER, {'data': 'draft:media'})
        await click(app, OWNER, 'caption_pick:')
        await click(app, OWNER, 'caption_preview:')
        assert not description
        await click(app, OWNER, 'caption_apply:')
        assert description == {'description': 'Текст', 'hashtags': '#tag'}
        await click(app, OWNER, 'draft_plan:')
        assert 'План публикаций' in app.tg.out[-1][1]
        assert not any(b['callback_data'].startswith('plan_confirm:')
                       for row in app.tg.out[-1][2]['inline_keyboard'] for b in row)
        plan_state = 'ready'
        await click(app, OWNER, 'plan:')
        await click(app, OWNER, 'plan_confirm:')
        assert ('POST', '/v1/posts/plan/plan/launch') not in calls
        await click(app, OWNER, 'owner_do:plan_launch:')
        await click(app, OWNER, 'owner_show:post_queue')
        assert 'published_item' in app.tg.out[-1][1]
        app.db.close()

    asyncio.run(run())


def test_invalid_or_incomplete_reply_inputs_keep_the_draft_and_allow_retry():
    async def run():
        app = new_app()
        await app.begin(MEMBER, 'reply_links')
        await app.input_draft(MEMBER, 'Повтор ввода')
        await app.input_draft(MEMBER, 'https://not-tiktok.example/')
        assert app.db.draft(MEMBER)['step'] == 'reply_links'

        async def upload(file_id, limit):
            if file_id == 'unavailable':
                raise TelegramError(503)
            return (COMMENT + '\n' + REPLY_TARGET).encode(), 'input.txt'

        app.tg.fetch_upload = upload
        for file_id in ('unavailable', 'ok'):
            await app.handle_upload(MEMBER, {'document': {'file_id': file_id, 'file_name': 'input.txt'}})
            assert app.db.draft(MEMBER)['step'] == ('reply_links' if file_id == 'unavailable' else 'replies')
        await app.input_draft(MEMBER, 'Текст')
        await click(app, MEMBER, 'wiz:skip:')
        await click(app, MEMBER, 'wiz:skip:')
        await click(app, MEMBER, 'wiz:submit:')
        assert app.db.draft(MEMBER)['step'] == 'review'
        assert not app.db.recent_tasks(MEMBER)
        assert 'Не все ссылки' in app.tg.out[-1][1]
        app.db.close()

    asyncio.run(run())
