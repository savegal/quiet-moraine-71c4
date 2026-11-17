"""Regressions for mismatches found against the supplied Tokora contract."""
import asyncio
import csv
import io
from types import SimpleNamespace

import aiohttp
from aiohttp import web
from jsonschema import Draft202012Validator
import pytest

from proxima_bot.api import ApiError, Tokora
from proxima_bot.operations import load_spec
from test_extended import button_callback, new_app


def test_resolve_post_callback_sends_the_required_resolution():
    async def run():
        requests = []
        spec = load_spec()
        operation = spec['paths']['/v1/posts/{id}/resolve']['post']
        schema = operation['requestBody']['content']['application/json']['schema']

        async def handler(request):
            body = await request.json()
            Draft202012Validator(schema).validate(body)
            requests.append((request.match_info['id'], body))
            assert request.headers['Authorization'] == 'Bearer audit-test'
            return web.json_response({'id': request.match_info['id'], 'outcome': 'not_published'})

        server = web.Application()
        server.router.add_post('/v1/posts/{id}/resolve', handler)
        runner = web.AppRunner(server)
        await runner.setup()
        app = new_app()
        try:
            site = web.TCPSite(runner, '127.0.0.1', 0)
            await site.start()
            port = runner.addresses[0][1]
            async with aiohttp.ClientSession() as session:
                app.api = Tokora(session, f'http://127.0.0.1:{port}', 'audit-test')
                await app.callback(7, {'data': 'post_go:resolve:post7'})
                assert requests == []
                await app.callback(99, {'data': 'post_go:resolve:post7'})
            assert requests == [('post7', {'resolution': 'not_published'})]
            assert '▣ Публикация' in app.tg.out[-1][1]
            assert 'Публикация не состоялась' in app.tg.out[-1][1]
        finally:
            app.db.close()
            await runner.cleanup()

    asyncio.run(run())


def test_tokora_shares_budget_across_transfers_and_preserves_error_ids(monkeypatch, caplog):
    async def run():
        hits = []
        clock = SimpleNamespace(value=100.0)
        monkeypatch.setattr('proxima_bot.api.time', SimpleNamespace(monotonic=lambda: clock.value))
        monkeypatch.setattr('proxima_bot.api.TOKORA_REQUESTS_PER_MINUTE', 3)

        async def handler(request):
            hits.append(request.path)
            if request.path == '/v1/download':
                return web.Response(body=b'file')
            if request.path == '/v1/scope':
                return web.json_response({'error':{'code':'api_scope_denied','requestId':'body-123'}}, status=403)
            if request.path == '/v1/limited':
                return web.Response(status=429, headers={'Retry-After':'17','X-Request-Id':'header-456'})
            return web.json_response({})

        server = web.Application()
        server.router.add_route('*','/{tail:.*}',handler)
        runner = web.AppRunner(server)
        await runner.setup()
        try:
            site = web.TCPSite(runner,'127.0.0.1',0)
            await site.start()
            port = runner.addresses[0][1]
            async with aiohttp.ClientSession() as session:
                api = Tokora(session,f'http://127.0.0.1:{port}','audit-test')
                await api.upload('/v1/upload',b'file')
                await api.download('/v1/download')
                with pytest.raises(ApiError) as error:
                    await api.call('GET','/v1/scope')
                assert error.value.request_id == api.last_error_request_id == 'body-123'
                with pytest.raises(ApiError) as error:
                    await api.call('POST','/v1/blocked')
                assert error.value.status == 429 and error.value.retry_after == 60
                assert hits == ['/v1/upload','/v1/download','/v1/scope']

                clock.value += 60
                with pytest.raises(ApiError) as error:
                    await api.call('GET','/v1/limited')
                assert error.value.request_id == api.last_error_request_id == 'header-456'
                with pytest.raises(ApiError) as error:
                    await api.download('/v1/blocked')
                assert error.value.retry_after == 17 and error.value.request_id == 'header-456'
                assert hits[-1] == '/v1/limited'
                app = new_app()
                try:
                    app.api = api
                    await app.health(7)
                    assert not app.tg.out
                    await app.health(99)
                    assert 'header-456' in app.tg.out[-1][1]
                finally:
                    app.db.close()
        finally:
            await runner.cleanup()

    asyncio.run(run())
    assert 'body-123' in caplog.text and 'header-456' in caplog.text


def test_owner_navigates_publications_and_local_lists(monkeypatch):
    async def run():
        app = new_app()
        hits = []

        async def call(method,path,**kwargs):
            hits.append((method,path,kwargs['params']))
            if path == '/v1/posts':
                offset = kwargs['params']['offset']
                return {'total':17,'paused':False,'items':[
                    {'id':f'post_{i}','outcome':'queued'} for i in range(offset,min(offset+15,17))]}
            if path == '/v1/posts/queue':
                return {'total':17,'paused':True,'active':2,'activeLimit':3,'retrying':4,'subscriptionActive':True}
            return {'items':[{'id':f'draft_{i}'} for i in range(27)],'total':27}

        monkeypatch.setattr(app.api,'call',call)
        try:
            await app.callback(7,{'data':'owner_show:post_queue:1'})
            assert hits == []
            await app.posts(99)
            await app.callback(99,{'data':button_callback(app,'owner_show:post_queue')})
            assert button_callback(app,'post:post_14') == 'post:post_14'
            await app.callback(99,{'data':button_callback(app,'owner_show:post_queue:')})
            assert hits[-1] == ('GET','/v1/posts',{'limit':15,'offset':15})
            assert 'стр. 2' in app.tg.out[-1][1] and 'post_16' in app.tg.out[-1][1]
            await app.callback(99,{'data':button_callback(app,'owner_show:queue_state')})
            assert hits[-1][1] == '/v1/posts/queue'
            assert 'На паузе' in app.tg.out[-1][1] and '2 / 3' in app.tg.out[-1][1]

            await app.callback(99,{'data':'owner_show:drafts'})
            await app.callback(99,{'data':button_callback(app,'owner_show:drafts:')})
            assert hits[-1] == ('GET','/v1/posts/drafts',None)
            assert button_callback(app,'draft:draft_26') == 'draft:draft_26'
        finally:
            app.db.close()

    asyncio.run(run())


def test_full_csv_export_reads_past_2000_and_keeps_rows_intact(monkeypatch):
    async def run():
        app = new_app()
        task = app.db.queue_task(7,'Сбор','fetch',{'assets':{'accountIds':['acc7']}})
        app.db.transition(task,'completed',upstream_id='remote')
        total = 2123
        offsets = []
        monkeypatch.setattr('proxima_bot.extended.EXPORT_PART_BYTES', 80_000)

        async def entries(method,path,*,params,background):
            assert method == 'GET' and path == '/v1/tasks/remote/entries'
            assert background
            assert params['limit'] == 100
            offset = params['offset']
            offsets.append(offset)
            stop = min(total,offset+params['limit'])
            return {'offset':offset,'total':total,'more':stop<total,'items':[
                {'kind':'comment','text':f'=строка {i}\nпродолжение','outcome':'live',
                 'accountId':'private-account','url':'https://tiktok.com/@user/video/123?token=secret'}
                for i in range(offset,stop)]}

        monkeypatch.setattr(app.api,'call',entries)
        try:
            await app.callback(8,{'data':'export_csv:'+task})
            assert offsets == []
            await app.callback(7,{'data':'export_csv:'+task})
            rows = []
            assert len(app.tg.documents)>1
            for uid,data,name in app.tg.documents:
                assert uid == 7 and '_part' in name and len(data)<=80_000
                assert b'private-account' not in data and b'token=secret' not in data
                rows.extend(csv.DictReader(io.StringIO(data.decode('utf-8-sig'))))
            assert offsets == list(range(0,total,100))
            assert [row['text'] for row in rows] == [f"'=строка {i}\nпродолжение" for i in range(total)]
        finally:
            app.db.close()

    asyncio.run(run())


def test_prepare_report_requires_owner_confirmation_and_refresh_reads(monkeypatch):
    async def run():
        app = new_app()
        task = app.db.queue_task(7,'Публикации','publish',{'assets':{'accountIds':['acc7']}})
        app.db.transition(task,'completed',upstream_id='report-job')
        hits = []

        async def call(method,path,**kwargs):
            hits.append((method,path,kwargs))
            return {'state':'running','checked':1,'total':3}

        async def publication(upstream_id):
            assert upstream_id == 'report-job'
            return {'preparation':{'state':'running','checked':2,'total':3}}

        monkeypatch.setattr(app.api,'call',call)
        monkeypatch.setattr(app.api,'publication',publication,raising=False)
        try:
            await app.callback(99,{'data':'adm:report:'+task})
            prepare = button_callback(app,'adm:jobedit:prepare_report:')
            await app.callback(7,{'data':prepare})
            await app.callback(99,{'data':prepare})
            assert hits == []
            confirm = button_callback(app,'adm:jobapply:')
            await app.callback(99,{'data':confirm})
            await app.callback(99,{'data':confirm})
            assert hits == [('POST','/v1/tasks/report-job/report/prepare',{'retries':0})]
            await app.callback(99,{'data':button_callback(app,'adm:report:')})
            assert '2 / 3' in app.tg.out[-1][1]
            assert len(hits) == 1
        finally:
            app.db.close()

    asyncio.run(run())
