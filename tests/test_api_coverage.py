import asyncio
from collections import Counter
import json
from types import SimpleNamespace

import aiohttp
from aiohttp import web
import pytest

from proxima_bot.api import ApiError, TelegramError, Tokora
from proxima_bot.catalog import build_payload
from proxima_bot.manage import run_operation
from proxima_bot.operations import declared_operations, load_spec, operation_channel, operation_plan

from test_edit_in_place import Recorder
from test_extended import new_app


def sample(schema):
    if '$ref' in schema:
        return sample(load_spec()['components']['schemas'][schema['$ref'].split('/')[-1]])
    if 'enum' in schema:
        return schema['enum'][0]
    if 'anyOf' in schema:
        return sample(schema['anyOf'][0])
    kind = schema.get('type')
    if isinstance(kind, list):
        kind = kind[0]
    if kind == 'object':
        return {key: sample(schema['properties'][key]) for key in schema.get('required', [])}
    if kind == 'array':
        return [sample(schema['items'])] * schema.get('minItems', 0)
    if kind in ('integer', 'number'):
        return max(1, schema.get('minimum', 1))
    if kind == 'boolean':
        return True
    if kind == 'null':
        return None
    return 'test-id'


def test_every_declared_operation_has_a_verified_route(tmp_path):
    async def run():
        expected = None
        seen = []

        async def handler(request):
            verb, path, arguments, file_type, status, mime = expected
            seen.append((request.method, request.path))
            assert request.method == verb.upper() and request.path == path
            assert dict(request.query) == {key: str(value) for key, value in arguments['query'].items()}
            assert request.headers['Authorization'] == 'Bearer unit-test'
            if file_type:
                assert request.content_type == file_type
                assert await request.read() == b'test media'
            elif 'body' in arguments:
                assert await request.json() == arguments['body']
            elif request.can_read_body:
                assert await request.read() == b'null'
            if not mime:
                return web.Response(status=status)
            if mime == 'application/json':
                return web.json_response({'accepted': True}, status=status)
            return web.Response(body=b'test result', content_type='image/jpeg' if mime=='image/*' else mime,
                                status=status)

        app = web.Application()
        app.router.add_route('*', '/{tail:.*}', handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = runner.addresses[0][1]
        counts = Counter()
        ids = set()
        upload = tmp_path / 'upload.bin'
        upload.write_bytes(b'test media')
        params = tmp_path / 'params.json'
        try:
            async with aiohttp.ClientSession() as session:
                api = Tokora(session, f'http://127.0.0.1:{port}', 'unit-test')
                for pattern, verb, op in declared_operations():
                    operation = op['operationId']
                    channel = operation_channel(op)
                    assert operation not in ids
                    ids.add(operation)
                    counts[channel] += 1
                    arguments = {'path': {}, 'query': {}}
                    path = pattern
                    for item in op.get('parameters', []):
                        field = item['in']
                        if field == 'path' or field == 'query' and item.get('required'):
                            name = item['name']
                            value = sample(item['schema'])
                            arguments[field][name] = value
                            if field == 'path':
                                path = path.replace('{'+name+'}', value)
                    body_types = op.get('requestBody', {}).get('content', {})
                    file_type = ''
                    if 'application/json' in body_types:
                        arguments['body'] = sample(body_types['application/json']['schema'])
                    elif body_types:
                        file_type = next(iter(body_types))
                        if file_type == 'image/*':
                            file_type = 'image/jpeg'
                    if channel in ('managed', 'cabinet'):
                        with pytest.raises(ValueError):
                            operation_plan(operation, arguments, local=True)
                        continue
                    if channel == 'cli':
                        with pytest.raises(ValueError):
                            operation_plan(operation, arguments)
                    else:
                        assert operation_plan(operation, arguments)['path'] == path
                    status, response = next((int(code), value) for code, value in op['responses'].items()
                                            if code.startswith('2'))
                    mime = next(iter(response.get('content', {})), '')
                    expected = (verb, path, arguments, file_type, status, mime)
                    output = tmp_path / f'{operation}.out'
                    params.write_text(json.dumps(arguments), encoding='utf-8')
                    await run_operation(api, SimpleNamespace(operation_id=operation, json_file=params, output=output,
                                        upload=upload if file_type else None, content_type=file_type, max_download_mib=32))
                    assert seen[-1] == (verb.upper(), path)
                    if mime == 'application/json':
                        assert json.loads(output.read_bytes()) == {'accepted': True}
                    elif mime:
                        assert output.read_bytes() == b'test result'
                    else:
                        assert json.loads(output.read_bytes()) == {}
        finally:
            await runner.cleanup()
        assert len(ids) == 106 and len(seen) == 101
        assert counts == {'op/cli': 77, 'cli': 24, 'managed': 4, 'cabinet': 1}

    asyncio.run(run())


def test_api_console_validates_path_query_and_keeps_binary_local():
    async def run():
        app = new_app()
        await app.command(99, '/api getAssetsByKind {"kind":"bad"}')
        await app.command(99, '/api getTasks {"limit":201}')
        await app.command(99, '/api getTasksByIdArchive {"id":"job"}')
        await app.command(7, '/op getStatistics')
        assert not app.api.calls
        await app.command(99, '/api getAssetsByKind {"kind":"awemeIds"}')
        assert app.api.calls[-1][:2] == ('GET', '/v1/assets/awemeIds')
        app.db.close()

    asyncio.run(run())


def test_rich_card_downgrades_and_keeps_updating():
    async def run():
        app = new_app()

        class RejectRichEdit(Recorder):
            async def call(self, method, data, **kwargs):
                self.calls.append((method, data))
                if method == 'editMessageText' and 'rich_message' in data:
                    raise TelegramError(400, 'unsupported tag')
                return {'message_id': 11, 'rich_message': method == 'sendRichMessage'}

        app.tg = RejectRichEdit()
        task_id = app.db.queue_task(7, 'Сбор', 'fetch', {'assets': {'accountIds': ['acc7']}})
        await app.task(7, task_id)
        app.db.transition(task_id, 'running', upstream_id='job', snapshot={'done':1,'total':2})
        await app.live_cards.tick()
        assert app.live_cards.cards[7].rich is False
        app.db.transition(task_id, 'completed', snapshot={'done':2,'total':2})
        await app.live_cards.tick()
        assert 'Завершено' in app.tg.calls[-1][1]['text']
        assert app.live_cards.cards == {}
        assert [method for method, _ in app.tg.calls] == ['sendRichMessage', 'editMessageText',
                                                         'editMessageText', 'editMessageText']
        app.db.close()

    asyncio.run(run())


def test_rate_limited_edit_does_not_fall_through_to_send():
    async def run():
        class RateLimitedEdit(Recorder):
            async def call(self, method, data, **kwargs):
                self.calls.append(method)
                raise TelegramError(429, 'Too Many Requests', retry_after=30)

        tg = RateLimitedEdit()
        token = tg.edit_in_place(7, 5)
        try:
            with pytest.raises(TelegramError, match='429'):
                await tg.rich(7, '<p>Карточка</p>', 'Карточка')
            assert tg.calls == ['editMessageText']
        finally:
            tg.reset_edit(token)

    asyncio.run(run())


def test_duplicate_reply_targets_return_one_validated_target():
    async def run():
        class Resolver:
            async def call(self, method, path, **kwargs):
                return {'failed': 0, 'items': [{'value':'1234567890:1234567891'}, {'duplicate':True}]}

        data = {'name':'Ответы', 'replies':['Текст'],
                'reply_links':['1234567890:1234567891','https://vt.tiktok.com/example/']}
        assert (await build_payload(Resolver(), 'reply_links', data, ['acct']))['replyLinks'] == ['1234567890:1234567891']

    asyncio.run(run())


def test_health_distinguishes_missing_scope_and_invalid_key():
    async def run():
        app = new_app()
        requested = []

        async def active_tasks():
            return []

        async def read(method, path, **kwargs):
            assert method == 'GET'
            requested.append(path)
            if path == '/v1/posts':
                raise ApiError('api_scope_denied', 403)
            return {}

        app.api.active_tasks = active_tasks
        app.api.call = read
        await app.health(99)
        text = app.tg.out[-1][1]
        assert 'posts:read' in text and 'Нет права' in text and 'Отключена' in text
        assert len(requested) == 5

        async def invalid_key():
            raise ApiError('api_key_invalid', 401)

        app.api.active_tasks = invalid_key
        await app.health(99)
        assert 'отклонил ключ' in app.tg.out[-1][1]
        assert len(requested) == 5
        app.db.close()

    asyncio.run(run())


def test_oversized_stream_is_rejected_without_replaying_write(monkeypatch):
    async def run():
        class OversizedResponse:
            status = 201
            headers = {}

            @property
            def content(self):
                return self

            async def iter_chunked(self, size):
                yield b'{"'
                yield b'x' * 64
                pytest.fail('oversized JSON stream was not stopped')

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class Session:
            requests = 0

            def request(self, *args, **kwargs):
                self.requests += 1
                return OversizedResponse()

        session = Session()
        monkeypatch.setattr('proxima_bot.api.MAX_JSON_BYTES', 32)
        api = Tokora(session, 'https://example.invalid', 'unit-test')
        with pytest.raises(ApiError) as error:
            await api.start({'requestId': 'test'})
        assert error.value.code == 'invalid_response' and session.requests == 1

    asyncio.run(run())
