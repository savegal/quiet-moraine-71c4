import asyncio
import json

import pytest

from proxima_bot.api import ApiError
from proxima_bot.catalog import build_payload
from proxima_bot.config import Config
from proxima_bot.presentation import empty_video_search
from proxima_bot.worker import Worker
from test_extended import new_app
from test_guided_order_form import click, options
from test_order_flows import OrderProvider, VIDEO_ID
from test_worker import FakeTelegram


def test_video_search_theme_edit_preserves_filters_and_failed_forecast_does_not_block_queue():
    class Provider(OrderProvider):
        async def call(self, method, path, **kwargs):
            if path == '/v1/tasks/feedwarm/forecast':
                raise ApiError('upstream_error', 502)
            return await super().call(method, path, **kwargs)

    async def run():
        app = new_app()
        app.api = Provider()
        try:
            await options(app, 'feedwarm')
            await app.input_draft(7, 'Максимум результатов: 75\nПериод поиска: 30\nМинимум лайков: 20\nМинимум комментариев: 5')
            await click(app, 'wiz:quick:')
            assert 'не удалось получить' in app.tg.out[-1][1]
            await click(app, 'wiz:edit_search:')
            await app.input_draft(7, 'Новая тема\nНовая тема')
            assert app.db.draft(7)['step'] == 'review'
            await click(app, 'wiz:submit:')
            queued, = app.db.recent_tasks(7)
            assert queued['state'] == 'queued'
            await Worker(app.db, app.api, FakeTelegram(), Config('fake', 'fake', 99, dispatch_enabled=True)).tick()
            sent, = app.api.started
            assert sent['feedWarm'] == {
                'topics': ['Новая тема'], 'topTag': True, 'phraseSearch': True,
                'searchLimit': 75, 'periodDays': 30, 'minLikes': 20, 'minComments': 5,
                'comment': False, 'like': False, 'dryRun': True, 'search': True, 'noFeed': True,
            }
        finally:
            app.db.close()
    asyncio.run(run())


def test_deferring_search_theme_in_template_keeps_filters_and_requires_new_text():
    async def run():
        app = new_app()
        try:
            tid = app.db.save_template(7, 'Поиск', 'feedwarm', {'draft': {'warm_json': {
                'keywords': ['Старая фраза'], 'searchLimit': 75, 'periodDays': 30,
            }}})
            await app.edit_local_template(7, 'feedwarm', tid)
            await click(app, 'lt:edit_warm_json:')
            await click(app, 'lt:skip:')
            await click(app, 'lt:save:')
            await app.from_template(7, tid)
            assert app.db.draft(7)['step'] == 'warm_json'
            await app.input_draft(7, 'Новая тема')
            await click(app, 'wiz:quick:')
            await click(app, 'wiz:submit:')
            task, = app.db.recent_tasks(7)
            settings = json.loads(task['payload_json'])['feedWarm']
            assert settings['topics'] == ['Новая тема']
            assert 'keywords' not in settings
            assert settings['searchLimit'] == 75 and settings['periodDays'] == 30
        finally:
            app.db.close()
    asyncio.run(run())


def test_video_search_with_only_filters_cannot_be_queued():
    async def run():
        app = new_app()
        try:
            data = {'name': 'Поиск', 'warm_json': {'searchLimit': 75}}
            app.save_wizard_step(7, 'feedwarm', 'review', data)
            await app.preview(7)
            assert app.db.draft(7)['step'] == 'warm_json'
            assert 'Укажи тему поиска' in app.tg.out[-1][1]
            with pytest.raises(ValueError, match='хотя бы одну тему'):
                await build_payload(app.api, 'feedwarm', data, ['acc7'], defer_resolution=True)
            assert app.db.recent_tasks(7) == []
        finally:
            app.db.close()
    asyncio.run(run())


def test_username_order_rejects_profile_and_sends_comment_and_reply_scope():
    async def run():
        app = new_app()
        try:
            await app.callback(7, {'data': 'new:usernames'})
            await app.input_draft(7, 'https://www.tiktok.com/@example')
            assert app.db.draft(7)['step'] == 'videos'
            assert 'конкретное видео' in app.tg.out[-1][1]
            await app.input_draft(7, VIDEO_ID)
            await app.input_draft(7, 'Лимит сбора: 1000')
            await click(app, 'wiz:quick:')
            assert 'Комментарии и ответы' in app.tg.out[-1][1]
            await click(app, 'wiz:submit:')
            task, = app.db.recent_tasks(7)
            payload = json.loads(task['payload_json'])
            assert payload['awemeIds'] == [VIDEO_ID]
            assert payload['settings']['includeReplies'] is True
            assert payload['settings']['collectAll'] is False
            assert payload['settings']['fetchCount'] == 1000
        finally:
            app.db.close()
    asyncio.run(run())


@pytest.mark.parametrize(('state','snapshot','expected'), [
    ('completed', {'collected': 0}, True),
    ('completed', {'warm': {'searchDone': True, 'matched': 0}}, True),
    ('completed', {'collected': 0, 'warm': {'searchDone': True, 'matched': 3}}, False),
    ('completed', {'collected': 0, 'warm': {'failed': 2}}, False),
    ('completed', {}, False),
    ('running', {'collected': 0}, False),
    ('stopped', {'collected': 0}, False),
])
def test_empty_video_search_requires_confirmed_completion_and_zero_matches(state, snapshot, expected):
    assert empty_video_search({'kind': 'feedwarm', 'state': state, 'snapshot_json': json.dumps(snapshot)}) is expected


def test_video_results_offer_link_from_id_and_empty_later_page_has_no_search_restart():
    class Provider(OrderProvider):
        async def call(self, method, path, **kwargs):
            assert method == 'GET' and path.endswith('/entries')
            offset = kwargs['params']['offset']
            items = [] if offset else [{'kind': 'video', 'awemeId': VIDEO_ID, 'outcome': 'matched'}]
            return {'items': items, 'total': 1, 'more': False}

    async def run():
        app = new_app()
        app.api = Provider()
        try:
            tid = app.db.queue_task(7, 'Сбор', 'feedwarm', {'assets': {'accountIds': ['acc7']}})
            app.db.transition(tid, 'completed', upstream_id='search', snapshot={'collected': 1})
            await app.task_entries(7, tid)
            assert 'https://www.tiktok.com/share/video/' + VIDEO_ID in app.tg.out[-1][1]
            await app.task_entries(7, tid, offset=10)
            assert 'Результатов на этой странице пока нет.' in app.tg.out[-1][1]
            assert 'new:feedwarm' not in str(app.tg.out[-1][2])
        finally:
            app.db.close()
    asyncio.run(run())
