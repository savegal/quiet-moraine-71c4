import asyncio
import json
import re

import pytest

from proxima_bot.api import ApiError, Telegram, TelegramError
from proxima_bot.catalog import parse_feedwarm
from proxima_bot.config import Config
from proxima_bot.db import now
from proxima_bot.operations import operation_plan
from proxima_bot.worker import Worker

from test_extended import new_app
from test_worker import FakeTokora


class TelegramTransport(Telegram):
    def __init__(self, supports_rich=True):
        self.supports_rich = supports_rich
        self.calls = []

    async def call(self, method, data, **kwargs):
        self.calls.append((method,data))
        if method == 'sendRichMessage':
            if not self.supports_rich:
                raise TelegramError(404)
            return {'message_id':len(self.calls),'rich_message':{'blocks':[]}}
        if method == 'sendMessage':
            return {'message_id':len(self.calls)}
        return True


def test_invoice_button_and_long_html_have_valid_transport_markup():
    async def run():
        tg=TelegramTransport()
        await tg.invoice(7,'invoice',1500,2)
        method,payload=tg.calls[-1]
        button=payload['reply_markup']['inline_keyboard'][0][0]
        assert method=='sendInvoice' and button=={'text':'✓ Оплатить · 1 500 Stars','pay':True,'style':'success'}
        text='<b>'+'&lt;'*1200+'</b>'
        await tg.send(7,text)
        assert tg.calls[-1][1]['text']==text
        assert tg.calls[-1][1]['parse_mode']=='HTML'
        await tg.send(7,'<b>'+'x'*4500+'</b>')
        assert len(tg.calls[-1][1]['text'])==4090
        assert 'parse_mode' not in tg.calls[-1][1]
    asyncio.run(run())


@pytest.mark.parametrize('supports_rich', [True,False])
def test_order_updates_in_place_through_completion(supports_rich):
    async def run():
        app = new_app()
        app.tg = TelegramTransport(supports_rich)
        api = FakeTokora()
        config = Config('fake','fake',99,dispatch_enabled=True)
        app.cfg = config
        worker = Worker(app.db,api,app.tg,config)
        task_id = app.db.queue_task(7,'Сбор','fetch',{'assets':{'accountIds':['acc7']}})
        await app.task(7,task_id)
        await app.admin_job(99,task_id)
        await app.live_cards.tick()
        assert all(method != 'editMessageText' for method,_ in app.tg.calls)
        member_card = app.live_cards.cards[7]
        owner_card = app.live_cards.cards[99]
        await worker.tick()
        api.final['job1'] = {'id':'job1','state':'paused','done':1,'total':2,'collected':14}
        await worker.tick()
        await app.live_cards.tick()
        edits = [body for method,body in app.tg.calls if method=='editMessageText']
        assert {(body['chat_id'],body['message_id']) for body in edits} == {
            (7,member_card.message_id),(99,owner_card.message_id)}
        for body in edits:
            output = body['rich_message']['html'] if supports_rich else body['text']
            assert 'На паузе' in output and '50%' in output and '14' in output
            assert ('<table bordered striped compact>' in output) == supports_rich
        api.final['job1']['state'] = 'completed'
        await worker.tick()
        await app.live_cards.tick()
        assert not app.live_cards.cards
        calls = len(app.tg.calls)
        await app.live_cards.tick()
        assert len(app.tg.calls) == calls
        app.db.close()
    asyncio.run(run())


def test_worker_does_not_resurrect_a_canceled_order_during_api_preflight():
    async def run():
        app = new_app()
        task_id = app.db.queue_task(7,'Тест','fetch',{'assets':{'accountIds':['acc7']}})
        api = FakeTokora()

        async def active_tasks():
            assert app.db.cancel_queued(task_id,7)
            return []

        api.active_tasks = active_tasks
        worker = Worker(app.db,api,app.tg,Config('fake','fake',99,dispatch_enabled=True))
        await worker.tick()
        assert app.db.task(task_id)['state'] == 'canceled'
        assert not api.started
        app.db.close()
    asyncio.run(run())


def test_read_failure_keeps_queue_cancellable_and_schedule_is_atomic():
    async def run():
        app = new_app()
        data = {'assets':{'accountIds':['acc7']}}
        with pytest.raises(ValueError):
            app.db.queue_task(7,'Слишком рано','fetch',data,fire_at=now()+1)
        assert not app.db.recent_tasks(7)
        task_id = app.db.queue_task(7,'Тест','fetch',data)
        api = FakeTokora()

        async def active_tasks():
            raise ApiError('service_unavailable',503)

        api.active_tasks = active_tasks
        worker = Worker(app.db,api,app.tg,Config('fake','fake',99,dispatch_enabled=True))
        await worker.tick()
        assert app.db.cancel_queued(task_id,7)
        app.db.close()
    asyncio.run(run())


def test_expired_member_cannot_submit_old_source_reply_confirmation():
    async def run():
        app = new_app()
        task_id = app.db.queue_task(7,'Тест','fetch',{'assets':{'accountIds':['acc7']}})
        app.db.transition(task_id,'completed',upstream_id='source')
        app.db.save_draft(7,'source_reply','review',{'source_task':task_id,'replies':['Текст']})
        app.db.modify_access(7,99,'revoke')
        await app.callback(7,{'data':'source_reply_submit'})
        assert len(app.db.recent_tasks(7)) == 1
        app.db.close()
    asyncio.run(run())


def test_promo_and_payment_keep_access_ledger_consistent():
    async def run():
        app = new_app()
        app.tg = TelegramTransport()
        await app.callback(7,{'data':'promo_create'})
        assert app.db.draft(7) is None
        await app.callback(99,{'data':'promo_create'})
        await app.input_draft(99,'2')
        promo_html = app.tg.calls[-1][1]['rich_message']['html']
        assert 'type="copy_text"' in promo_html
        code = re.search(r'/promo ([0-9A-F]{16})',promo_html).group(1)
        previous = app.db.user(7)['expires_at']
        assert await app.command(7,'/promo '+code.lower())
        assert app.db.user(7)['expires_at'] == previous+2*86400
        assert not app.db.redeem_promo(8,code)
        invoice = app.db.create_invoice(7,50,days=1)
        app.db.conn.execute('UPDATE invoices SET expires_at=0 WHERE payload=?',(invoice,))
        app.db.modify_access(7,99,'block')
        assert not app.db.invoice_valid(7,invoice,50)
        assert app.db.paid_invoice(7,invoice,50,'charge',30) == previous+3*86400
        assert app.db.user(7)['blocked']
        assert app.db.paid_invoice(7,invoice,50,'charge',30) is None
        assert [(row['action'],row['days']) for row in app.db.subscriptions(7)][:3] == [
            ('stars',1),('block',0),('promo',2)]
        app.db.close()
    asyncio.run(run())


def test_post_queue_lists_documented_posts_and_proxy_summary_is_private():
    async def run():
        app = new_app()
        app.tg = TelegramTransport()

        async def page(offset):
            return {'accounts':[
                {'accountId':'acc7','proxy':'proxy.internal:1111','status':'active'},
                {'accountId':'acc8','proxy':'other.internal:1111','status':'active'},
            ]}

        async def call(method,path,**kwargs):
            assert method=='GET' and path=='/v1/posts'
            return {'total':1,'paused':True,'items':[{'id':'post_1','outcome':'queued'}]}

        app.api.account_page = page
        app.api.call = call
        await app.proxies(7)
        proxy_view = app.tg.calls[-1][1]['rich_message']['html']
        assert 'С привязкой прокси</td><td>1' in proxy_view
        assert 'internal' not in proxy_view
        await app.owner_show(99,'post_queue')
        assert 'post_1' in app.tg.calls[-1][1]['text']
        app.db.close()
    asyncio.run(run())


def test_schema_validation_handles_refs_utf8_and_file_operations():
    assert parse_feedwarm('{"keywords":["ролики"],"search":true}')['keywords'] == ['ролики']
    with pytest.raises(ValueError):
        operation_plan('postTasksReplyLinksResolve',{'body':{'links':42}})
    result = operation_plan('postTasksReplyLinksResolve',{'body':{'links':['abc']}})
    assert json.loads(json.dumps(result['payload'])) == {'links':['abc']}
    with pytest.raises(ValueError):
        operation_plan('postAccountsImportFile',{},upload_type='application/octet-stream')
    result = operation_plan('postAccountsImportFile',{},local=True,upload_type='application/octet-stream')
    assert result['path'] == '/v1/accounts/import/file'
