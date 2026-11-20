"""Private diagnostics through the production cabinet and Telegram routing."""
import asyncio
from contextlib import suppress

from proxima_bot.advanced_db import ExtendedStore
from proxima_bot.config import Config
from proxima_bot.workspace_app import WorkspaceApp
from test_transport_and_routes import Session
from test_workspaces import Transport, query


OWNER, MEMBER = 99, 7


class AlertTransport(Transport):
    def __init__(self):
        super().__init__()
        self.delivered = asyncio.Event()

    async def send(self, uid, text, markup=None):
        result = await super().send(uid,text,markup)
        if uid==OWNER and 'Tokora</b>' in text:
            self.delivered.set()
        return result


def new_workspace(*responses):
    store = ExtendedStore(':memory:')
    store.grant(MEMBER,7)
    store.assign(MEMBER,'member-account')
    app = WorkspaceApp(Config('fake','',OWNER,database_path=':memory:'),store,AlertTransport(),Session(responses))
    cabinet = app.cabinets['main']
    cabinet.api.set_keys({'first':'test-key-private-value'})
    return app,cabinet


async def deliver(app):
    task = asyncio.create_task(app.provider_alerts.run())
    try:
        await asyncio.wait_for(app.tg.delivered.wait(),1)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def messages(app, uid):
    return [data for method,data in app.tg.calls if method in ('sendMessage','sendRichMessage','editMessageText')
            and data.get('chat_id')==uid]


def test_member_receives_safe_message_and_owner_receives_api_diagnostics_once():
    async def run():
        trouble = ('Tokora: original <failure> using test-key-private-value\n'
                   'https://name:private-proxy-password@example.invalid/path?secret=hidden\n'
                   'Bearer abcdefghijklmnopqrstuvwxyz\nCookie:\nsecret-multiline-cookie')
        responses = [(403,{'error':{'code':'api_scope_denied','message':trouble,'requestId':request_id},
                           'scope':'tasks:read'}) for request_id in ('provider-first','provider-second')]
        app,_ = new_workspace(*responses)
        try:
            tid=app.db.queue_task(MEMBER,'Сбор','fetch',{'assets':{'accountIds':['member-account']}})
            app.db.transition(tid,'completed',upstream_id='remote-collection')
            for _ in responses:
                await app.handle_chat_update(query(MEMBER,1,'entries:'+tid))
            assert app.provider_alerts.pending.qsize()==1
            assert 'действие сейчас недоступно' in str(messages(app,MEMBER))
            assert all(word not in str(messages(app,MEMBER)) for word in ('Tokora','provider-first','api_scope_denied'))
            await deliver(app)
            notice, = messages(app,OWNER)
            text = notice['text']
            assert all(fragment in text for fragment in (
                'HTTP · 403','api_scope_denied','provider-first','tasks:read',
                'GET /v1/tasks/remote-collection/entries','original &lt;failure&gt;'))
            assert all(secret not in text for secret in (
                'test-key-private-value','private-proxy-password','abcdefghijklmnopqrstuvwxyz','secret-multiline-cookie'))
        finally:
            await app.close()
            app.db.close()

    asyncio.run(run())


def test_recovered_alternative_key_does_not_emit_failure_notice():
    async def run():
        app,cabinet = new_workspace((403,{'error':{'code':'api_scope_denied','message':'Read denied'}}),
                                    (200,{'items':[]}))
        cabinet.api.set_keys({'first':'test-key-private-value','second':'test-key-second-value'})
        try:
            assert await cabinet.api.call('GET','/v1/tasks')=={'items':[]}
            assert app.provider_alerts.pending.empty()
        finally:
            await app.close()
            app.db.close()

    asyncio.run(run())


def test_worker_reports_original_codes_in_terminal_snapshot_to_owner_only():
    async def run():
        snapshot = {'id':'remote-order','state':'completed_with_errors','finishedAt':'2026-11-20T00:00:00Z',
                    'error':'Original <report> from Tokora','failed':4,
                    'errorCounts':[{'code':'provider_specific_reason','count':4}]}
        app,cabinet = new_workspace((200,snapshot))
        try:
            tid = app.db.queue_task(MEMBER,'Заказ','publish',{'assets':{'accountIds':['member-account']}})
            app.db.transition(tid,'running',upstream_id='remote-order')
            await cabinet.worker.refresh_job(app.db.task(tid))
            assert app.db.task(tid)['state']=='completed'
            await cabinet.app.task(MEMBER,tid)
            assert 'Другие ошибки' in str(messages(app,MEMBER))
            assert all(detail not in str(messages(app,MEMBER))
                       for detail in ('Tokora','provider_specific_reason','Original'))
            await deliver(app)
            text, = [message['text'] for message in messages(app,OWNER)]
            assert f'<code>{tid}</code>' in text
            assert 'Original &lt;report&gt; from Tokora' in text
            assert '<code>provider_specific_reason</code> · 4' in text
        finally:
            await app.close()
            app.db.close()

    asyncio.run(run())
