import asyncio
from contextlib import closing

from proxima_bot.advanced_db import ExtendedStore
from proxima_bot.workspace_registry import WorkspaceRegistry, workspace_database

from test_workspaces import make_app, message, query


OWNER = 99
NEWCOMER = 7


def messages_to(app, uid):
    return [data for method,data in app.tg.calls
            if method in ('sendMessage','sendRichMessage','editMessageText') and data.get('chat_id')==uid]


def callbacks(payload):
    return [button['callback_data'] for row in (payload.get('reply_markup') or {}).get('inline_keyboard',[])
            for button in row if 'callback_data' in button]


def test_opening_the_bot_requires_approval_and_keeps_order_subscription_separate():
    async def run():
        app = make_app()
        try:
            await app.handle_chat_update(message(1, NEWCOMER, '/start'))
            assert callbacks(messages_to(app,NEWCOMER)[-1])==['entry:request']
            assert not messages_to(app,OWNER)
            assert not app.db.user(NEWCOMER)

            request = query(NEWCOMER,1,'entry:request')
            request['callback_query']['from'].update(first_name='<Саша>',username='requester')
            await app.handle_chat_update(request)
            await app.handle_chat_update(request)
            assert app.admission.state(NEWCOMER)=='pending'
            notice, = messages_to(app,OWNER)
            assert '&lt;Саша&gt;' in notice['text'] and '@requester' in notice['text']
            view, = callbacks(notice)

            code = app.db.issue_promo(OWNER,7)
            packed = app.registry.pack_callback('main','new:publish')
            document = message(4,NEWCOMER,'')
            document['message']['document']={'file_id':'ignored','file_name':'accounts.txt'}
            for update in (
                message(2,NEWCOMER,'/workspaces'),message(3,NEWCOMER,'/promo '+code),
                query(NEWCOMER,2,'home'),query(NEWCOMER,2,packed),document,
            ):
                await app.handle_chat_update(update)
                assert callbacks(messages_to(app,NEWCOMER)[-1])==['entry:status']
            await app.handle_chat_update(query(NEWCOMER,3,f'entry:allow:{NEWCOMER}:pending'))
            assert app.admission.state(NEWCOMER)=='pending'
            assert app.db.draft(NEWCOMER) is None and not app.db.user(NEWCOMER)
            assert app.db.conn.execute('SELECT used_by FROM promo_codes').fetchone()[0] is None

            await app.handle_chat_update(query(OWNER,1,view))
            allow, deny, _ = callbacks(messages_to(app,OWNER)[-1])
            await app.handle_chat_update(query(OWNER,2,allow))
            await app.handle_chat_update(query(OWNER,2,deny))
            assert app.admission.state(NEWCOMER)=='approved'
            notification = messages_to(app,NEWCOMER)[-1]
            assert callbacks(notification)==['entry:open']
            assert not app.db.user(NEWCOMER) and not app.db.subscriptions(NEWCOMER)

            await app.handle_chat_update(query(NEWCOMER,8,'entry:open'))
            assert any(app.registry.callback_route(button)==('main','create')
                       for button in callbacks(messages_to(app,NEWCOMER)[-1]))
            assert app.db.user(NEWCOMER)['expires_at']==0
            assert not app.db.is_active(NEWCOMER,OWNER) and not app.db.account_ids(NEWCOMER)
            await app.handle_chat_update(query(NEWCOMER,9,packed))
            assert app.db.draft(NEWCOMER) is None
            assert not app.db.recent_tasks(NEWCOMER)

            await app.handle_chat_update(message(10,NEWCOMER,'/promo '+code))
            assert app.db.is_active(NEWCOMER,OWNER)
        finally:
            await app.close()
            app.db.close()

    asyncio.run(run())


def test_owner_can_review_a_rejection_after_restart_from_another_cabinet(tmp_path):
    path = str(tmp_path/'bot.db')

    async def reject():
        app = make_app(path)
        try:
            await app.handle_chat_update(query(NEWCOMER,1,'entry:request'))
            await app.handle_chat_update(query(OWNER,2,f'entry:deny:{NEWCOMER}:pending'))
            assert app.admission.state(NEWCOMER)=='rejected'
            await app.handle_chat_update(query(NEWCOMER,3,'entry:request'))
            assert callbacks(messages_to(app,NEWCOMER)[-1])==['entry:status']
        finally:
            await app.close()
            app.db.close()

    asyncio.run(reject())

    async def reconsider():
        app = make_app(path)
        try:
            other = app.registry.create_workspace('Второй')
            cabinet = app.open_cabinet(app.registry.workspace(other))
            # The button in a cabinet dashboard is scoped, but requests are bot-wide.
            await cabinet.app.admin_dashboard(OWNER)
            queue_button = next(button for button in callbacks(messages_to(app,OWNER)[-1])
                                if app.registry.callback_route(button)==(other,'entry:list:pending:0'))
            await app.handle_chat_update(query(OWNER,4,queue_button))
            assert 'entry:list:rejected:0' in callbacks(messages_to(app,OWNER)[-1])
            await app.handle_chat_update(query(OWNER,5,'entry:list:rejected:0'))
            assert f'entry:view:{NEWCOMER}' in callbacks(messages_to(app,OWNER)[-1])
            await app.handle_chat_update(query(OWNER,6,f'entry:view:{NEWCOMER}'))
            approve,back = callbacks(messages_to(app,OWNER)[-1])
            assert approve==f'entry:allow:{NEWCOMER}:rejected' and back=='entry:list:rejected:0'
            await app.handle_chat_update(query(OWNER,6,approve))
            assert app.admission.can_enter(NEWCOMER)
            assert callbacks(messages_to(app,NEWCOMER)[-1])==['entry:open']
        finally:
            await app.close()
            app.db.close()

    asyncio.run(reconsider())


def test_existing_participants_keep_entry_and_new_subscriptions_do_not_bypass_approval(tmp_path):
    path = str(tmp_path/'legacy.db')
    with closing(ExtendedStore(path)) as store:
        store.grant(7,30)
        store.register(8)
        store.grant(9,7)
        store.modify_access(9,OWNER,'revoke')
        registry = WorkspaceRegistry(store.conn,path)
        other = registry.create_workspace('Другой')
        registry.allow_member(other,10)
        with closing(ExtendedStore(workspace_database(path,other))) as secondary:
            secondary.assign(11,'assigned-account')

    async def run():
        app = make_app(path)
        try:
            assert all(app.admission.can_enter(uid) for uid in (OWNER,7,9,10,11))
            assert not app.admission.can_enter(8)
            app.db.grant(8,30)
            await app.handle_chat_update(message(1,8,'/start'))
            assert callbacks(messages_to(app,8)[-1])==['entry:request']
            assert app.db.is_active(8,OWNER)
        finally:
            await app.close()
            app.db.close()
        app = make_app(path)
        try:
            assert not app.admission.can_enter(8)
        finally:
            await app.close()
            app.db.close()

    asyncio.run(run())
