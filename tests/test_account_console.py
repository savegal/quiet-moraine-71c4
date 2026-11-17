import asyncio
import csv
import io
import json

import pytest

from proxima_bot.api import TelegramError
from test_extended import button_callback, new_app


def account(aid,username='public_user',status='active'):
    return {
        'accountId':aid,'status':status,'checkedAt':1_765_555_000_000,
        'profile':{'username':username,'bio':'private-profile','uid':'private-uid'},
        'proxy':'http://private-user:private-password@proxy.test:8080',
        'cookies':['private-session'],
        'lastError':'private-provider-message',
        'publicationHealth':{'attempts24h':7,'errors24h':2,'blockReason':'private-block'},
    }


def test_owner_inventory_preserves_search_and_status_across_pages():
    async def run():
        app = new_app()
        calls = []

        async def fetch(method,path,*,params):
            calls.append((method,path,params))
            return {'accounts':[account('result')],'total':25,
                    'counts':{'total':40,'active':30,'expired':10}}

        app.api.call = fetch
        try:
            await app.accounts(99)
            await app.callback(99,{'data':button_callback(app,'ac:search')})
            await app.input_draft(99,'Демо')
            await app.callback(99,{'data':button_callback(app,'ac:list:active:0')})
            await app.callback(99,{'data':button_callback(app,'ac:list:active:1')})
            assert calls == [
                ('GET','/v1/accounts',{'offset':0,'limit':10,'q':'Демо'}),
                ('GET','/v1/accounts',{'offset':0,'limit':10,'q':'Демо','status':'active'}),
                ('GET','/v1/accounts',{'offset':10,'limit':10,'q':'Демо','status':'active'}),
            ]
            assert 'Всего в кабинете' in app.tg.out[-1][1]
            await app.callback(99,{'data':button_callback(app,'ac:clear_search')})
            assert len(calls)==3 and 'account_search' not in app.db.preferences(99)
        finally:
            app.db.close()

    asyncio.run(run())


def test_owner_can_choose_folder_for_new_orders_without_widening_account_access():
    async def run():
        app=new_app()
        folders=[]

        async def fetch(method,path,**kwargs):
            assert (method,path)==('GET','/v1/account-folders')
            folders.append(path)
            return {'items':[{'id':'1234abcd','name':'Моя папка','active':1,'count':1}],
                    'unassigned':2,'available':True}

        async def scoped(folder):
            if folder=='1234abcd':
                return {'owner-free':{**account('owner-free'),'folderId':'1234abcd'}}
            if folder=='none':
                return {'no-folder-1':account('no-folder-1')}
            raise AssertionError(folder)

        async def page(offset,limit=200,**kwargs):
            assert kwargs=={}
            return {'accounts':[{**account('owner-free'),'folderId':'1234abcd'},account('no-folder-1')],'total':2}

        app.api.call=fetch
        app.api.accounts_in_folder=scoped
        app.api.account_page=page
        try:
            await app.callback(7,{'data':'ac:order_folder:1234abcd'})
            await app.callback(7,{'data':'ac:order_folder:none'})
            await app.callback(7,{'data':'ac:order_folder:all'})
            assert not folders and not app.db.preferences(7)

            await app.callback(99,{'data':'ac:folders:0'})
            assert 'Все аккаунты кабинета' in app.tg.out[-1][1]
            assert 'Моя папка' in app.tg.out[-1][1]
            assert button_callback(app,'ac:order_folder:none')=='ac:order_folder:none'

            await app.callback(99,{'data':button_callback(app,'ac:order_folder:1234abcd')})
            app.db.save_draft(99,'fetch','review',{})
            draft=app.db.draft(99)
            assert await app.review_order_accounts(99,draft)==(['owner-free'],'Папка кабинета · 1234abcd')
            assert app.db.draft(99)['data']['review_account_folder']=='1234abcd'

            # Assigning the account to a traffer does not lock out the owner.
            app.db.assign(7,'owner-free')
            assert await app.review_order_accounts(99,app.db.draft(99))==(['owner-free'],'Папка кабинета · 1234abcd')
            assert app.db.account_ids(99)==[]

            # Switching to unfoldered accounts and back to all workspace accounts works cleanly.
            await app.callback(99,{'data':button_callback(app,'ac:order_folder:none')})
            assert await app.review_order_accounts(99,app.db.draft(99))==(['no-folder-1'],'Аккаунты без папки')
            assert app.db.draft(99)['data']['review_account_folder']=='none'

            await app.callback(99,{'data':button_callback(app,'ac:order_folder:all')})
            assert not app.db.preferences(99).get('order_account_folder')
            assert await app.review_order_accounts(99,app.db.draft(99))==(['no-folder-1','owner-free'],'Все аккаунты кабинета')
            assert app.db.draft(99)['data']['review_account_folder']==''
        finally:
            app.db.close()

    asyncio.run(run())


@pytest.mark.parametrize('workspace_rows,expected', [
    ([{**account('active'),'folderId':'1234abcd'},{**account('expired',status='expired'),'folderId':'1234abcd'}],['active','expired']),
    ([{**account('active'),'folderId':'1234abcd'},{**account('acc8'),'folderId':'4321dcba'}],['acc8','active']),
    ([{**account('active'),'folderId':'1234abcd'},{**account('other'),'folderId':'4321dcba'}],['active','other']),
    ([account('without-folder'),{**account('second'),'folderId':''}],['second','without-folder']),
    ([{**account('expired',status='expired'),'folderId':'1234abcd'},account('dead',status='error')],None),
])
def test_owner_default_pool_includes_all_folders_unfoldered_and_traffer_accounts(workspace_rows,expected):
    async def run():
        app=new_app()
        app.db.assign(8,'acc8')

        async def page(offset,limit=200,**kwargs):
            assert kwargs=={}
            return {'accounts':workspace_rows,'total':len(workspace_rows)}

        app.api.account_page=page
        try:
            app.save_wizard_step(99,'fetch','review',{'review_account_ids':['formerly-confirmed'],'review_account_folder':'1234abcd'})
            result=await app.review_order_accounts(99,app.db.draft(99))
            if expected is None:
                assert result is None
                data=app.db.draft(99)['data']
                assert 'review_account_ids' not in data and 'review_account_folder' not in data
                assert button_callback(app,'ac:folders:0')=='ac:folders:0'
            else:
                assert result==(expected,'Все аккаунты кабинета')
                assert app.db.draft(99)['data']['review_account_ids']==expected
                assert app.db.draft(99)['data']['review_account_folder']==''
        finally:
            app.db.close()
    asyncio.run(run())


def test_member_account_card_and_csv_hide_foreign_accounts_and_sensitive_fields():
    async def run():
        app = new_app()
        aid = 'member-internal-id'
        app.db.assign(7,aid)
        app.db.grant(8,30)
        mine = account(aid)
        foreign = account('acc8','foreign_public')

        async def page(offset,limit=200,**kwargs):
            return {'accounts':[mine,foreign],'total':2}

        async def call(method,path,**kwargs):
            assert (method,path)==('GET','/v1/accounts/'+aid)
            app.api.calls.append(path)
            return mine

        app.api.account_page = page
        app.api.call = call
        try:
            await app.accounts(7)
            card = button_callback(app,'ac:member:')
            assert aid not in json.dumps(app.tg.out[-1][2])
            assert button_callback(app,'check_pool') == 'check_pool'
            await app.callback(8,{'data':card})
            assert not app.api.calls
            await app.callback(7,{'data':card})
            assert len(app.api.calls)==1
            await app.callback(7,{'data':'ac:export:8'})
            assert not app.tg.documents
            await app.callback(7,{'data':'ac:export'})
            assert len(app.tg.documents)==1
            report = app.tg.documents[-1][1].decode('utf-8-sig')
            all_member_data = report + ''.join(text for uid,text,_ in app.tg.out if uid==7)
            assert '@public_user' in all_member_data and 'active' in report
            for secret in (aid,'private-','proxy.test','foreign_public','account_id'):
                assert secret not in all_member_data

            async def revoked_result(method,path,**kwargs):
                app.db.unassign(7,aid)
                return await call(method,path,**kwargs)

            app.api.call = revoked_result
            await app.callback(7,{'data':card})
            assert 'Аккаунт недоступен' in app.tg.out[-1][1]
            await app.callback(7,{'data':card})
            assert len(app.api.calls)==2
        finally:
            app.db.close()

    asyncio.run(run())


def test_owner_workspace_csv_reads_every_provider_page_and_whitelists_fields():
    async def run():
        app = new_app()
        inventory = [account(f'acc{i}') for i in range(205)]
        offsets = []

        async def page(offset,limit=200,**kwargs):
            offsets.append(offset)
            return {'accounts':inventory[offset:offset+limit],'total':len(inventory)}

        app.api.account_page = page
        try:
            await app.callback(99,{'data':'ac:export'})
            assert offsets == [0,200]
            owner,raw,name = app.tg.documents[-1]
            assert owner==99 and name=='proxima_accounts.csv'
            content = raw.decode('utf-8-sig')
            rows = list(csv.DictReader(io.StringIO(content)))
            assert len(rows)==205 and rows[-1]['account_id']=='acc204'
            assert rows[7]['assigned_telegram_id']=='7'
            assert rows[7]['username']=="'@public_user" and rows[7]['proxy_bound']=='yes'
            assert 'private-' not in content and 'proxy.test' not in content
        finally:
            app.db.close()

    asyncio.run(run())


def test_account_export_parts_keep_headers_and_retry_only_the_rejected_part(monkeypatch):
    async def run():
        app = new_app()
        inventory = [account(f'acc{i}') for i in range(10)]
        attempts = []
        delays = []

        async def page(offset,limit=200,**kwargs):
            return {'accounts':inventory,'total':len(inventory)}

        async def attachment(uid,data,filename,caption=''):
            attempts.append(filename)
            if len(attempts)==1:
                raise TelegramError(429,retry_after=1)
            app.tg.documents.append((uid,data,filename))

        async def sleep(delay):
            delays.append(delay)

        app.api.account_page = page
        app.tg.attachment = attachment
        monkeypatch.setattr('proxima_bot.account_console.EXPORT_PART_BYTES',420)
        monkeypatch.setattr('proxima_bot.export_parts.sleep',sleep)
        try:
            await app.callback(99,{'data':'ac:export'})
            rows = []
            assert len(app.tg.documents)>1 and delays==[1]
            for index,(uid,data,filename) in enumerate(app.tg.documents,1):
                assert uid==99 and filename==f'proxima_accounts_part{index:03d}.csv'
                assert len(data)<=420 and data.startswith(b'\xef\xbb\xbfaccount_id,')
                assert b'private-' not in data
                rows.extend(csv.DictReader(io.StringIO(data.decode('utf-8-sig'))))
            assert [row['account_id'] for row in rows]==[item['accountId'] for item in inventory]
            assert attempts == [app.tg.documents[0][2], *[doc[2] for doc in app.tg.documents]]
        finally:
            app.db.close()

    asyncio.run(run())


def test_individual_checks_and_refresh_hold_the_account_and_display_returned_status():
    async def run():
        app = new_app()
        calls = []

        async def call(method,path,**kwargs):
            assert app.db.account_in_use('acc7')
            calls.append((method,path,kwargs))
            return account('acc7',status='expired')

        app.api.call = call
        try:
            await app.callback(7,{'data':'ac:check:acc7'})
            await app.callback(99,{'data':'ac:check:acc7'})
            assert not app.db.account_in_use('acc7')
            assert 'Сессия истекла' in app.tg.out[-1][1]
            await app.callback(99,{'data':button_callback(app,'ac:profile:')})
            assert calls == [
                ('POST','/v1/accounts/acc7/check',{'retries':0}),
                ('POST','/v1/accounts/acc7/profile/refresh',{'retries':0}),
            ]
            with app.db.account_action('acc7'):
                await app.callback(99,{'data':'ac:check:acc7'})
                assert len(calls)==2
        finally:
            app.db.close()

    asyncio.run(run())


def test_owner_comment_diagnostics_page_and_confirm_like_once():
    async def run():
        app = new_app()
        video = '7420000000000000000'
        comment = '7420000000000000001'
        calls = []
        base = f'/v1/accounts/acc7/videos/{video}/comments'

        async def call(method,path,**kwargs):
            assert app.db.account_in_use('acc7')
            calls.append((method,path,kwargs))
            if method=='PUT':
                return {'videoId':video,'commentId':comment,'requestedLiked':True,'accepted':True,'confirmed':False}
            return {'items':[{'id':comment,'videoId':video,'text':'<b>Public comment</b>',
                              'author':{'username':'author'},'replyCount':1,'liked':False}],
                    'nextCursor':17,'hasMore':'cursor' not in kwargs['params']}

        app.api.call = call
        try:
            await app.callback(7,{'data':f'ac:comments:acc7:{video}:0'})
            assert not calls
            await app.callback(99,{'data':'ac:video:acc7'})
            await app.input_draft(99,video)
            assert '&lt;b&gt;Public comment&lt;/b&gt;' in app.tg.out[-1][1]
            await app.callback(99,{'data':button_callback(app,'ac:comments:')})
            assert calls[-1]==('GET',base,{'params':{'limit':6,'cursor':17}})
            await app.callback(99,{'data':button_callback(app,'ac:replies:')})
            assert calls[-1]==('GET',f'{base}/{comment}/replies',{'params':{'limit':6}})
            await app.callback(99,{'data':button_callback(app,'ac:like:')})
            assert calls[-1][0]=='GET'
            canceled = button_callback(app,'ac:apply_like:')
            await app.callback(99,{'data':button_callback(app,'ac:cancel_like:')})
            await app.callback(99,{'data':canceled})
            assert all(call[0]!='PUT' for call in calls)
            await app.callback(99,{'data':button_callback(app,'ac:like:')})
            confirm = button_callback(app,'ac:apply_like:')
            await app.callback(7,{'data':confirm})
            await app.callback(99,{'data':confirm})
            await app.callback(99,{'data':confirm})
            assert [call for call in calls if call[0]=='PUT'] == [
                ('PUT',f'{base}/{comment}/like',{'payload':{'liked':True},'retries':0}),
            ]
            assert 'Запрос принят' in app.tg.out[-1][1]
        finally:
            app.db.close()

    asyncio.run(run())
