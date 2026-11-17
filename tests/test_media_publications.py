import asyncio
import time

import aiohttp
from aiohttp import web
import pytest

from proxima_bot.api import ApiError, Tokora
from proxima_bot.operations import load_spec, valid_body
from test_api_coverage import sample
from test_extended import button_callback, new_app


@pytest.mark.parametrize('kind', ['video', 'photo'])
def test_media_plan_launch_cancel_retry_and_refresh_through_http(kind):
    async def run():
        app = new_app()
        spec = load_spec()
        schemas = spec['components']['schemas']
        post_page_schema = spec['paths']['/v1/posts']['get']['responses']['200']['content']['application/json']['schema']
        plan = {
            **sample(schemas['PostPlan']), 'id': 'media-plan', 'state': 'checking',
            'readyAt': None, 'expiresAt': int(time.time()*1000)+600_000,
            'postsTotal': 1, 'queued': 0, 'error': '',
        }
        post = {
            **sample(schemas['PostTask']), 'id': 'media-post', 'kind': kind, 'name': 'Проверка',
            'outcome': 'queued', 'errorCode': '', 'postUrl': '',
            'cancelRequested': False, 'attempts': 0,
        }
        changes = []

        def response(schema, body, status=200):
            assert valid_body(schemas[schema] if isinstance(schema, str) else schema, body)
            return web.json_response(body, status=status)

        async def handler(request):
            assert request.headers['Authorization'] == 'Bearer media-test'
            method, path = request.method, request.path
            if method == 'POST':
                body = await request.json()
                changes.append((path, body))
            if path == '/v1/posts/plan':
                assert body['draftIds']==['media']
                assert valid_body(schemas['PostSettings'],body['settings'])
                return response('PostPlan', plan, 202)
            if path=='/v1/posts/drafts/media':
                return web.json_response({'id':'media','kind':kind,'photos':[{'id':'photo'}]})
            if path=='/v1/posts/settings':return response('PostSettings',sample(schemas['PostSettings']))
            if path == '/v1/posts/plan/media-plan':
                return response('PostPlan', plan)
            if path == '/v1/posts/plan/media-plan/launch':
                assert plan['state'] == 'ready' and plan['queued'] == 0
                plan.update(queued=1)
                return web.json_response({'planId': plan['id'], 'queued': 1})
            if path == '/v1/posts':
                page = {
                        **sample(post_page_schema), 'items': [post], 'total': 1,
                    'offset': int(request.query['offset']), 'limit': int(request.query['limit']),
                }
                return response(post_page_schema, page)
            if path == '/v1/posts/media-post':
                return response('PostTask', post)
            if path == '/v1/posts/media-post/cancel':
                post.update(outcome='cancelled', cancelRequested=True)
                return response('PostTask', post)
            if path == '/v1/posts/media-post/retry':
                schema = spec['paths']['/v1/posts/{id}/retry']['post']['requestBody']['content']['application/json']['schema']
                assert valid_body(schema, body) and body == {'confirm': True}
                post.update(outcome='queued', cancelRequested=False, attempts=1)
                return response('PostTask', post)
            if path == '/v1/posts/media-post/refresh':
                post.update(outcome='published', postUrl='https://www.tiktok.com/@author/video/7420000000000000000')
                return response('PostTask', post, 202)
            raise AssertionError((method, path))

        server = web.Application()
        server.router.add_route('*', '/{tail:.*}', handler)
        runner = web.AppRunner(server)
        await runner.setup()
        try:
            site = web.TCPSite(runner, '127.0.0.1', 0)
            await site.start()
            async with aiohttp.ClientSession() as session:
                app.api = Tokora(session, f'http://127.0.0.1:{runner.addresses[0][1]}', 'media-test')
                await app.callback(7, {'data': 'draft_plan:media'})
                assert changes == []
                await app.callback(99, {'data': 'draft_plan:media'})
                assert changes==[]
                await app.callback(99,{'data':button_callback(app,'mo:prepare:')})
                assert 'Проверяется' in app.tg.out[-1][1]
                assert 'plan_confirm:' not in str(app.tg.out[-1][2])

                plan.update(state='ready', readyAt=int(time.time()*1000))
                await app.callback(99, {'data': button_callback(app, 'plan:')})
                await app.callback(99, {'data': button_callback(app, 'plan_confirm:')})
                confirm = button_callback(app, 'owner_do:plan_launch:')
                await app.callback(99, {'data': confirm})
                await app.callback(99, {'data': confirm})
                assert sum(path.endswith('/launch') for path, _ in changes) == 1
                await app.callback(99, {'data': 'owner_show:post_queue'})
                await app.callback(99, {'data': button_callback(app, 'post:')})
                assert 'В очереди' in app.tg.out[-1][1]
                assert 'post_retry_confirm:' not in str(app.tg.out[-1][2])

                await app.callback(99, {'data': button_callback(app, 'post_go:cancel:')})
                assert 'Отменено' in app.tg.out[-1][1]
                assert 'post_go:cancel:' not in str(app.tg.out[-1][2])

                post.update(outcome='failed')
                await app.callback(99, {'data': 'post:media-post'})
                await app.callback(99, {'data': button_callback(app, 'post_retry_confirm:')})
                retry = button_callback(app, 'owner_do:post_retry:')
                await app.callback(99, {'data': retry})
                await app.callback(99, {'data': retry})
                assert sum(path.endswith('/retry') for path, _ in changes) == 1
                assert 'В очереди' in app.tg.out[-1][1]

                post.update(outcome='pending')
                await app.callback(99, {'data': 'post:media-post'})
                await app.callback(99, {'data': button_callback(app, 'post_go:refresh:')})
                assert 'Опубликовано' in app.tg.out[-1][1]
                assert 'https://www.tiktok.com/@author/video/' in app.tg.out[-1][1]
        finally:
            app.db.close()
            await runner.cleanup()

    asyncio.run(run())


@pytest.mark.parametrize(('outcome', 'message'), [
    ({'planId': 'plan', 'queued': 0}, 'Новых публикаций в очередь не добавлено.'),
    ({'planId': 'different', 'queued': 1}, 'Запуск плана не подтверждён.'),
    (ApiError('queue_full', 400), 'Очередь публикаций заполнена.'),
    (ApiError('connection_unavailable'), 'Запуск плана не подтверждён.'),
])
def test_plan_launch_errors_preserve_queue_link_and_consume_confirmation(outcome, message):
    async def run():
        app = new_app()
        writes = []
        plan = {'id': 'plan', 'state': 'ready', 'postsTotal': 1}

        async def provider(method, path, **kwargs):
            if method == 'GET':
                assert path == '/v1/posts/plan/plan'
                return plan
            assert path == '/v1/posts/plan/plan/launch'
            writes.append(path)
            if isinstance(outcome, ApiError):
                raise outcome
            return outcome

        app.api.call = provider
        try:
            await app.callback(99, {'data': 'plan_confirm:plan'})
            confirm = button_callback(app, 'owner_do:plan_launch:')
            await app.callback(99, {'data': confirm})
            assert message in app.tg.out[-1][1]
            assert 'owner_show:post_queue' in str(app.tg.out[-1][2])
            await app.callback(99, {'data': confirm})
            assert len(writes) == 1
        finally:
            app.db.close()

    asyncio.run(run())
