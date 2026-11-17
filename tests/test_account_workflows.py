"""End-to-end checks for team access and managed account workflows."""
import asyncio
import gzip
import io
import json
import tarfile

import pytest

from proxima_bot.account_checks import purge_workspace_accounts, start_workspace_account_check
from proxima_bot.account_import_ui import parse_upload
from proxima_bot.api import ApiError, lookup_accounts, scan_active_tasks
from proxima_bot.db import Store
from proxima_bot.imports import import_and_assign, parse_account_file
from proxima_bot.worker import snapshot_state

from test_extended import button_callback, new_app


def test_pool_check_never_switches_to_global_or_checks_after_revocation():
    async def run():
        app = new_app()
        app.db.assign(7,'acc7b')
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def provider(method,path,**kwargs):
            calls.append((method,path))
            assert kwargs['retries'] == 0 and kwargs['background']
            entered.set()
            await release.wait()
            return {'accountId':'acc7','status':'active','proxy':'private:password@example.invalid'}

        app.api.call = provider
        try:
            await app.callback(7,{'data':'check_pool:8'})
            assert app.db.draft(7) is None
            await app.callback(7,{'data':'check_pool'})
            confirm = button_callback(app,'pc:run:')
            await app.callback(7,{'data':confirm})
            await asyncio.wait_for(entered.wait(),timeout=2)
            check_id = app.pool_checks.running_id
            assert app.db.account_in_use('acc7') and not app.db.account_in_use('acc8')
            await app.callback(8,{'data':'pc:stop:'+check_id})
            assert not app.pool_checks.stop_requested.is_set()
            app.db.modify_access(7,99,'block')
            release.set()
            await asyncio.wait_for(app.pool_checks.task,timeout=2)
            assert calls == [('POST','/v1/accounts/acc7/check')]
            assert app.pool_checks.get(7,check_id) is None and app.pool_checks.get(8,check_id) is None
            result = app.pool_checks.get(99,check_id)
            assert result['state'] == 'stopped' and result['counts'] == {'active':1}
            assert 'password' not in json.dumps(result)
            assert not app.db.account_in_use('acc7')
        finally:
            await app.pool_checks.close()
            app.db.close()

    asyncio.run(run())


def test_workspace_check_adopts_existing_run_and_holds_ambiguous_start():
    async def run():
        db = Store(':memory:')
        calls = []
        status = 'checking'

        class Provider:
            async def call(self,method,path,**kwargs):
                calls.append((method,path))
                if path=='/v1/tasks':
                    return {'items':[]}
                if method=='POST':
                    raise ApiError('connection_unavailable')
                return {'state':status}

        try:
            assert (await start_workspace_account_check(Provider(),db))['state'] == 'checking'
            marker = db.account_check_marker()
            assert marker and calls == [('GET','/v1/accounts/check')]
            status = 'idle'
            with pytest.raises(ApiError,match='connection_unavailable'):
                await start_workspace_account_check(Provider(),db)
            assert db.account_check_marker() and db.account_check_marker() != marker
            assert calls[-3:] == [('GET','/v1/accounts/check'),('GET','/v1/tasks'),('POST','/v1/accounts/check')]
        finally:
            db.close()

    asyncio.run(run())


def test_account_purge_requires_empty_team_and_workspace():
    async def run():
        db = Store(':memory:')
        posts_total = 1
        calls = []

        class Provider:
            async def call(self,method,path,**kwargs):
                calls.append((method,path))
                if path=='/v1/tasks':
                    return {'items':[]}
                if path=='/v1/schedules':
                    assert kwargs['params']['tab'] == 'pending'
                    return {'items':[],'total':0}
                if path=='/v1/posts/queue':
                    return {'total':posts_total,'active':0,'retrying':0}
                if path=='/v1/accounts/check':
                    return {'state':'idle'}
                assert db.workspace_action_active()
                assert kwargs['payload'] == {'confirm':'DELETE','stopActiveTasks':False}
                assert kwargs['retries'] == 0
                return {'deleted':3}

        try:
            db.assign(7,'acc7')
            with pytest.raises(ValueError):
                await purge_workspace_accounts(Provider(),db,'DELETE')
            assert calls == []
            db.unassign(7,'acc7')
            with pytest.raises(ValueError):
                await purge_workspace_accounts(Provider(),db,'DELETE')
            assert ('POST','/v1/accounts/purge') not in calls
            posts_total = 0
            assert await purge_workspace_accounts(Provider(),db,'DELETE') == 3
        finally:
            db.close()

    asyncio.run(run())


def test_stale_stop_does_not_end_the_next_workspace_check():
    async def run():
        app = new_app()
        started_at = 100
        calls = []

        async def provider(method,path,**kwargs):
            calls.append((method,path))
            return {'state':'running','startedAt':started_at}

        app.api.call = provider
        try:
            await app.callback(99,{'data':'owner_go:accounts_check_stop'})
            confirm = button_callback(app,'owner_confirm:accounts_check_stop')
            started_at = 200
            await app.callback(99,{'data':confirm})
            assert calls == [('GET','/v1/accounts/check'),('GET','/v1/accounts/check')]
            assert 'другая проверка' in app.tg.out[-1][1]
        finally:
            app.db.close()

    asyncio.run(run())


def test_tar_and_gzip_import_preserve_records_and_reject_unsafe_members():
    raw = b'sessionid=first\nsessionid=second\n'
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w') as archive:
        item = tarfile.TarInfo('subfolder/accounts.txt')
        item.size = len(raw)
        archive.addfile(item,io.BytesIO(raw))
    for content,suffix in ((stream.getvalue(),'.tar'),(gzip.compress(stream.getvalue()),'.gz'),
                           (gzip.compress(raw),'.gzip')):
        accounts = parse_account_file(content,suffix)
        assert [account['cookies'][0]['value'] for account in accounts] == ['first','second']
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w') as archive:
        item = tarfile.TarInfo('symlink.txt')
        item.type = tarfile.SYMTYPE
        item.linkname = '../accounts.txt'
        archive.addfile(item)
    with pytest.raises(ValueError,match='обычные JSON и TXT'):
        parse_account_file(stream.getvalue(),'.tar')
    with pytest.raises(ValueError,match='серверный импорт'):
        parse_upload(b'{"cookies":[{"name":"sessionid","value":"secret"}],"proxy":"socks5://secret"}','.json')


def test_import_does_not_write_during_active_order():
    async def run():
        db = Store(':memory:')
        db.assign(7,'acc7')
        order = db.queue_task(7,'Сбор','fetch',{'assets':{'accountIds':['acc7']}})
        db.transition(order,'running',upstream_id='upstream')

        class Provider:
            async def call(self,*args,**kwargs):
                pytest.fail('import sent while another task was active')

        try:
            summary = await import_and_assign(Provider(),db,7,[{'cookies':[{'name':'sid','value':'test'}]}])
            assert summary.stopped and summary.attempted == 0 and summary.assigned == 0
        finally:
            db.close()

    asyncio.run(run())


def test_account_lookup_respects_total_across_short_pages():
    async def run():
        offsets = []

        class Provider:
            async def account_page(self, offset):
                offsets.append(offset)
                return {'accounts':[{'accountId':str(offset),'status':'active'}],'total':3}

        assert (await lookup_accounts(Provider(),['2']))['2']['status'] == 'active'
        assert offsets == [0,1,2]

    asyncio.run(run())


def test_live_task_state_variants_do_not_keep_the_queue_blocked():
    async def run():
        offsets = []

        class Provider:
            async def call(self,method,path,*,params,retries):
                assert (method,path,params['active']) == ('GET','/v1/tasks','true')
                offset = params.get('offset',0)
                offsets.append(offset)
                if offset==0:
                    return {'offset':0,'more':True,'items':[
                        {'id':'x','state':'completed_with_errors','finishedAt':'2026-11-16T16:00:00Z'},
                        {'id':'y','state':'stopped','finishedAt':'2026-11-16T16:00:00Z'},
                    ]}
                return {'offset':2,'more':False,'items':[{'id':'z','state':'paused'}]}

        assert await scan_active_tasks(Provider(),first_only=True) == [{'id':'z','state':'paused'}]
        assert offsets == [0,2]
        assert snapshot_state({'state':'completed_with_errors','finishedAt':'2026-11-16T16:00:00Z'},'running') == 'completed'

    asyncio.run(run())


def test_managed_proxy_pool_without_record_ids_can_be_checked():
    async def run():
        app = new_app()
        writes = []

        async def provider(method,path,**kwargs):
            if (method,path)==('GET','/v1/proxies'):
                return {'items':[],'mode':'engine','tokoraLines':2000,'total':2000}
            if (method,path)==('GET','/v1/proxies/check'):
                return {'state':'unchecked','checked':0,'total':0}
            writes.append((method,path,kwargs['payload']))
            return {'state':'running','checked':0,'total':2000}

        app.api.call = provider
        try:
            await app.proxies(99)
            assert 'Линии общего пула</td><td>2 000' in app.tg.out[-1][1]
            assert 'Рабочие</td>' not in app.tg.out[-1][1]
            assert 'px:select' not in str(app.tg.out[-1][2])
            await app.callback(99,{'data':'px:select'})
            assert app.db.draft(99) is None
            assert 'Линии общего пула</td><td>2 000' in app.tg.out[-1][1]
            await app.callback(99,{'data':button_callback(app,'px:check')})
            assert writes == [('POST','/v1/proxies/check',{})]
        finally:
            app.db.close()

    asyncio.run(run())


def test_import_for_idle_member_works_while_another_member_runs_order():
    async def run():
        db = Store(':memory:')
        db.assign(8,'acc8')
        order = db.queue_task(8,'Активный другого','fetch',{'assets':{'accountIds':['acc8']}})
        db.transition(order,'running',upstream_id='upstream')
        calls = []

        class Provider:
            async def call(self,method,path,**kwargs):
                calls.append((method,path))
                if path=='/v1/accounts/import/check':
                    return {'exists':False,'accountId':None}
                return {'account':{'accountId':'new-for-7'},'ready':True}

        try:
            summary = await import_and_assign(Provider(),db,7,[{'cookies':[{'name':'sid','value':'test'}]}])
            assert summary.assigned == 1 and 'new-for-7' in db.account_ids(7)
            assert ('POST','/v1/accounts/import') in calls
        finally:
            db.close()

    asyncio.run(run())


def test_lookup_accounts_fetches_few_missing_concurrently_after_first_page():
    async def run():
        offsets = []
        direct_calls = []
        in_flight = 0
        max_in_flight = 0

        class Provider:
            async def account_page(self, offset):
                offsets.append(offset)
                return {
                    'accounts': [{'accountId': f'other-{offset + i}', 'status': 'active'} for i in range(200)],
                    'total': 1600,
                }

            async def call(self, method, path, *, retries=0):
                nonlocal in_flight, max_in_flight
                assert method == 'GET' and path.startswith('/v1/accounts/')
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                try:
                    await asyncio.sleep(0.01)
                    aid = path.rsplit('/', 1)[-1]
                    direct_calls.append(aid)
                    if aid == 'acc-missing':
                        raise ApiError('not_found', 404)
                    return {'accountId': aid, 'status': 'active'}
                finally:
                    in_flight -= 1

        result = await lookup_accounts(Provider(), ['other-1', 'acc-900', 'acc-901', 'acc-missing'])
        assert offsets == [0]
        assert set(direct_calls) == {'acc-900', 'acc-901', 'acc-missing'}
        assert max_in_flight >= 2
        assert set(result) == {'other-1', 'acc-900', 'acc-901'}

    asyncio.run(run())


def test_lookup_accounts_batches_remaining_pages_concurrently_for_large_pools():
    async def run():
        offsets = []
        in_flight = 0
        max_in_flight = 0

        class Provider:
            async def account_page(self, offset):
                nonlocal in_flight, max_in_flight
                offsets.append(offset)
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                try:
                    if offset > 0:
                        await asyncio.sleep(0.01)
                    return {
                        'accounts': [{'accountId': f'acc-{offset + i}', 'status': 'active'} for i in range(200)],
                        'total': 1200,
                    }
                finally:
                    in_flight -= 1

        wanted = [f'acc-{i}' for i in range(980, 1005)]
        result = await lookup_accounts(Provider(), wanted)
        assert set(result) == set(wanted)
        assert max_in_flight >= 2
        assert offsets == [0, 200, 400, 600, 800, 1000]

    asyncio.run(run())
