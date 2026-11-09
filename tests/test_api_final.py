import asyncio
import json
import time

import aiohttp
from aiohttp import web
import pytest

from proxima_bot.api import ApiError, Telegram, Tokora
from proxima_bot.operations import operation_plan
from proxima_bot.updates import OFFSET_IDLE_LIMIT, UpdateInbox
from proxima_bot.worker import marked_task_name

from test_extended import button_callback, new_app


def test_member_results_project_only_public_fields():
    async def run():
        app=new_app()
        app.db.assign(7,'tokora-internal-account')
        tid=app.db.queue_task(7,'Проверка','publish',{'assets':{'accountIds':['acc7']}})
        app.db.transition(tid,'reconcile',upstream_id='tokora-internal-task',error_code='reconcile_review')

        async def account_page(offset,limit=200):
            return {'accounts':[{'accountId':'tokora-internal-account','status':'active',
                                 'profile':{'username':'public_user'}}]}

        async def publication(task_id):
            return {'comments':{'published':1},'errors':[{'code':'tokora_internal','label':'Tokora secret','count':2}]}

        async def entries(method,path,**kwargs):
            return {'items':[
                {'text':'Публичный текст с Tokora','kind':'tokora internal','outcome':'Tokora refused',
                 'status':'tokora_runtime','awemeId':'tokora-internal-video','url':'https://tokora.online/private'},
                {'text':'Второй текст','kind':'comment','outcome':'live',
                 'url':'https://www.tiktok.com/@user/video/7420000000000000000?comment_id=123&token=secret#hidden'},
                {'text':'Третий текст','url':'https://token@tiktok.com/private'},
            ]}

        app.api.account_page=account_page
        app.api.publication=publication
        app.api.call=entries
        await app.accounts(7)
        await app.task(7,tid)
        await app.report(7,tid)
        await app.task_entries(7,tid)
        await app.export_file(7,tid,'csv')
        await app.export_file(7,tid,'links')
        outputs=[body for uid,body,_ in app.tg.out if uid==7]
        outputs.extend(content.decode('utf-8-sig') for _,content,_ in app.tg.documents)
        sanitized='\n'.join(outputs).replace('Публичный текст с Tokora','Публичный текст')
        assert 'tokora' not in sanitized.casefold()
        assert 'token' not in sanitized and '#hidden' not in sanitized
        assert '@public_user' in sanitized and 'comment_id=123' in sanitized
        assert 'Публичный текст с Tokora' in outputs[-2]
        assert 'Другие ошибки' in sanitized or 'Другая причина' in sanitized
        app.db.close()
    asyncio.run(run())


def test_revoked_member_cannot_verify_or_pause_and_finished_refresh_reads_snapshot():
    async def run():
        app=new_app()
        tid=app.db.queue_task(7,'Проверка','publish',{'assets':{'accountIds':['acc7']}})
        app.db.transition(tid,'running',upstream_id='test1')
        calls=[]

        async def verify(task_id):
            calls.append(('verify',task_id))
            return {'id':task_id,'state':'completed','published':2}

        async def snapshot(task_id):
            return {'id':task_id,'state':'completed','published':3}

        app.api.verify=verify
        app.api.task=snapshot
        app.db.modify_access(7,99,'block')
        await app.callback(7,{'data':'pause:'+tid})
        await app.callback(7,{'data':'verify:'+tid})
        assert not calls and not app.api.calls
        app.db.modify_access(7,99,'unblock')
        app.db.transition(tid,'completed')
        await app.callback(7,{'data':'verify:'+tid})
        assert json.loads(app.db.task(tid)['snapshot_json'])['published']==2
        await app.callback(7,{'data':'refresh:'+tid})
        assert json.loads(app.db.task(tid)['snapshot_json'])['published']==3
        app.db.close()
    asyncio.run(run())


def test_owner_reconciliation_refuses_to_close_existing_order():
    async def run():
        app=new_app()
        tid=app.db.queue_task(7,'Тест','publish',{'assets':{'accountIds':['acc7']}})
        app.db.transition(tid,'reconcile',error_code='connection_unavailable')
        remote={'id':'remote-test','name':marked_task_name(app.db.task(tid)),'kind':'publish','state':'running'}
        active=[remote]

        async def active_tasks():return active
        async def task(task_id):return remote
        app.api.active_tasks=active_tasks
        app.api.task=task
        await app.callback(7,{'data':'adm:absent:'+tid})
        assert app.db.draft(7) is None
        await app.callback(99,{'data':'adm:absent:'+tid})
        await app.callback(99,{'data':button_callback(app,'adm:settle:')})
        assert app.db.task(tid)['state']=='reconcile'
        await app.callback(99,{'data':'adm:bind:'+tid})
        await app.input_draft(99,'wrong-id')
        assert app.db.task(tid)['state']=='reconcile'
        await app.input_draft(99,remote['id'])
        assert app.db.task(tid)['upstream_id']==remote['id']
        assert app.db.task(tid)['state']=='running'
        app.db.close()
    asyncio.run(run())


@pytest.mark.parametrize('operation,arguments',[
    ('deleteAccountsById',{'path':{'id':'acc7'}}),
    ('postAccountsPurge',{'body':{'confirm':'DELETE','stopActiveTasks':True}}),
    ('deleteTasksById',{'path':{'id':'job7'}}),
])
def test_raw_deletes_cannot_bypass_managed_routes(operation,arguments):
    for local in (False,True):
        with pytest.raises(ValueError):
            operation_plan(operation,arguments,local=local)


def test_http_contract_rich_send_edit_and_single_start_with_retry_after():
    async def run():
        requests=[]

        async def handler(request):
            body=await request.json() if request.can_read_body else None
            requests.append((request.method,request.path,body,request.headers.get('Authorization')))
            if request.path=='/v1/tasks':
                return web.json_response({'error':{'code':'rate_limited'}},status=429,headers={'Retry-After':'45'})
            return web.json_response({'ok':True,'result':{'message_id':9,'rich_message':{'blocks':[]}}})

        server=web.Application()
        server.router.add_route('*','/{tail:.*}',handler)
        runner=web.AppRunner(server)
        await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0)
        await site.start()
        port=runner.addresses[0][1]
        url=f'http://127.0.0.1:{port}'
        try:
            async with aiohttp.ClientSession() as session:
                telegram=Telegram(session,'test')
                telegram.base=url+'/bot-test/'
                markup={'inline_keyboard':[[{'text':'↶ Назад','callback_data':'home'}]]}
                rich='<table bordered striped compact><tr><td>Обработано</td><td>5</td></tr></table>'
                result=await telegram.rich(7,rich,'Обработано 5',markup)
                await telegram.edit_rich(7,result['message_id'],rich,markup)
                tokora=Tokora(session,url,'fake-key')
                with pytest.raises(ApiError) as error:
                    await tokora.start({'requestId':'test-request','kind':'fetch'})
                assert error.value.status==429 and error.value.retry_after==45
                with pytest.raises(ApiError) as blocked:await tokora.active_tasks()
                assert blocked.value.status==429
            assert [req[1] for req in requests]==['/bot-test/sendRichMessage','/bot-test/editMessageText','/v1/tasks']
            assert requests[1][2]['rich_message']['html']==rich and requests[1][2]['message_id']==9
            assert requests[2][2]['requestId']=='test-request' and requests[2][3]=='Bearer fake-key'
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_polling_discards_an_offset_after_telegram_id_reset_window():
    async def run():
        app=new_app()
        inbox=UpdateInbox(app)
        app.db.update_offset(123456)
        app.db.conn.execute("INSERT INTO metadata VALUES ('telegram_offset_at',?)",
                            (str(time.time()-OFFSET_IDLE_LIMIT-1),))
        received=[]

        async def updates(offset):
            received.append(offset)
            raise asyncio.CancelledError

        app.tg.updates=updates
        with pytest.raises(asyncio.CancelledError):
            await inbox.poll()
        assert received==[None]
        app.db.close()
    asyncio.run(run())
