"""Owner console permission, idempotency, subscription and usability regression tests."""
import asyncio
from dataclasses import replace
import tempfile
from pathlib import Path
import pytest
from proxima_bot.advanced_db import ExtendedStore
from proxima_bot.config import Config
from proxima_bot.extended import ProximaApp
from proxima_bot.db import Store, now

class TG:
    def __init__(self):self.sent=[];self.files=[]
    async def send(self,uid,text,keyboard=None):self.sent.append((uid,text,keyboard))
    async def rich(self,uid,rich,plain,keyboard=None):self.sent.append((uid,rich,keyboard))
    async def attachment(self,uid,content,name,caption=''):self.files.append((uid,content,name))

class API:
    def __init__(self):self.calls=[]
    async def account_page(self,offset,limit=200,**kwargs):return {'accounts':[],'total':0}
    async def call(self,method,path,**kwargs):
        self.calls.append((method,path))
        return {'id':'remote1'}
    async def publication(self,upstream):return {'comments':{'published':1,'attempts':2},'replies':{}}
    async def stop(self,upstream):self.calls.append(('stop',upstream))
    async def verify(self,upstream):self.calls.append(('verify',upstream))

def fresh():
    db=ExtendedStore(':memory:');tg=TG();api=API()
    app=ProximaApp(Config('bot','upstream',99),db,tg,api)
    db.touch_user(99,'Owner')
    db.touch_user(7,'Иван','Бондаренко','traffer7')
    db.touch_user(8,'Мария','Орлова','traffer8')
    db.grant(7,5)
    db.assign(7,'acc7');db.assign(8,'acc8')
    return app

def run(coro):return asyncio.run(coro)

def callbacks(app):
    return {button['callback_data'] for row in app.tg.sent[-1][2]['inline_keyboard'] for button in row}

def test_schema_migrates_existing_db_and_name_search_cyrillic():
    with tempfile.TemporaryDirectory() as tmp:
        fn=str(Path(tmp)/'old.db')
        d=Store(fn);d.register(7);d.close()
        d=ExtendedStore(fn);d.touch_user(7,'Иван','Бондаренко','test')
        assert d.members(search='иван')[1]==1
        assert d.members(search='БОНДАРЕНКО')[1]==1
        assert d.members(search='%')[1]==0
        assert d.user(7)['daily_limit'] is None
        d.close()

def test_status_filters_and_subscriptions():
    a=fresh();d=a.db
    assert d.members(status='active')[1]==1
    assert d.members(status='expired')[1]==2
    old=d.user(7)['expires_at']
    assert d.modify_access(7,99,'extend',2)==old+172800
    d.modify_access(7,99,'replace',760)
    assert d.user(7)['expires_at']>now()+759*86400
    d.modify_access(8,99,'block')
    assert d.members(status='blocked')[1]==1
    d.modify_access(8,99,'unblock')
    d.modify_access(7,99,'revoke')
    assert not d.is_active(7,99)
    with pytest.raises(ValueError):d.modify_access(99,99,'block')
    with pytest.raises(ValueError):d.modify_access(7,99,'extend',0)
    assert len(d.subscriptions(7))==3
    d.close()

def test_panel_renders_cards_and_filters():
    async def f():
        a=fresh()
        await a.admins(99);assert 'Центр управления' in a.tg.sent[-1][1]
        await a.callback(99,{'data':'adm:users:active:0'})
        assert 'Траферы' in a.tg.sent[-1][1]
        await a.callback(99,{'data':'adm:u:7'})
        assert 'Карточка трафера' in a.tg.sent[-1][1]
        assert 'traffer7' in a.tg.sent[-1][1]
        await a.callback(99,{'data':'adm:search'})
        await a.input_draft(99,'ИВАН')
        assert 'traffer7' in a.tg.sent[-1][1]
        a.db.close()
    run(f())

def test_confirm_once_and_nonowner_denied():
    async def f():
        a=fresh();old=a.db.user(7)['expires_at']
        assert await a.admin_callback(7,'adm:edit:extend:7:30') is True
        assert a.db.draft(7) is None
        await a.callback(99,{'data':'adm:edit:extend:7:2'})
        assert a.db.user(7)['expires_at']==old
        nonce=a.db.draft(99)['data']['nonce']
        await a.callback(7,{'data':'adm:apply:'+nonce})
        assert a.db.user(7)['expires_at']==old
        await a.callback(99,{'data':'adm:apply:'+nonce})
        assert a.db.user(7)['expires_at']==old+2*86400
        await a.callback(99,{'data':'adm:apply:'+nonce})
        assert len(a.db.subscriptions(7))==1
        a.db.close()
    run(f())

def test_subscription_rejects_an_id_outside_sqlite_range():
    async def f():
        app=fresh()
        await app.callback(99,{'data':'adm:pick:grant'})
        await app.input_draft(99,str(2**63))
        assert 'Проверь Telegram ID' in app.tg.sent[-1][1]
        assert app.db.draft(99)['mode']=='admin_pick'
        await app.input_draft(99,'12345678')
        assert app.db.draft(99)['data']['target']==12345678
        app.db.close()
    run(f())


def test_order_mode_settings_use_explicit_owner_confirmation():
    async def f():
        app=fresh()
        try:
            app.cfg=replace(app.cfg,restricted_modes=('feedwarm',))
            await app.callback(99,{'data':'admin'})
            assert 'adm:modes' in callbacks(app)
            await app.callback(99,{'data':'adm:modes'})
            assert {'adm:mode:feedwarm:0','adm:mode:reply_links:1'} <= callbacks(app)
            assert not any(route.startswith('adm:mode:check:') for route in callbacks(app))
            await app.callback(7,{'data':'adm:mode:feedwarm:0'})
            assert app.db.draft(7) is None

            await app.callback(99,{'data':'adm:mode:feedwarm:0'})
            cancel=next(route for route in callbacks(app) if route.startswith('adm:modecancel:'))
            await app.callback(99,{'data':cancel})
            assert app.db.draft(99) is None and app.mode_restricted(7,'feedwarm')

            await app.callback(99,{'data':'adm:mode:feedwarm:0'})
            confirm=next(route for route in callbacks(app) if route.startswith('adm:modeapply:'))
            await app.callback(7,{'data':confirm})
            assert app.mode_restricted(7,'feedwarm')
            await app.callback(99,{'data':confirm})
            await app.callback(99,{'data':confirm})
            assert not app.mode_restricted(7,'feedwarm')
            assert app.db.order_mode_access()=={'feedwarm':False}
            assert sum(row['action']=='order_mode_allow' for row in app.db.audit_entries())==1

            await app.callback(99,{'data':'adm:mode:reply_links:1'})
            confirm=next(route for route in callbacks(app) if route.startswith('adm:modeapply:'))
            await app.callback(99,{'data':confirm})
            assert app.mode_restricted(7,'source_reply')
            assert not app.mode_restricted(99,'source_reply')
            assert app.mode_restricted(7,'check')
        finally:
            app.db.close()
    run(f())


def test_assignment_explains_unready_session():
    async def f():
        app=fresh()

        async def fetch_account(method,path,**kwargs):
            assert (method,path)==('GET','/v1/accounts/new-account')
            return {'accountId':'new-account','status':'expired'}

        app.api.call=fetch_account
        await app.callback(99,{'data':'adm:edit:assign:7:custom'})
        await app.input_draft(99,'new-account')
        token=app.db.draft(99)['data']['nonce']
        await app.callback(99,{'data':'adm:apply:'+token})
        assert 'new-account' in app.db.account_ids(7)
        assert 'Сессия истекла' in app.tg.sent[-1][1]
        app.db.close()
    run(f())


def test_assigned_account_found_beyond_large_cabinet_page_cap():
    async def f():
        app=fresh()
        offsets=[]

        async def account_page(offset):
            offsets.append(offset)
            return {'accounts':[{'accountId':f'other-{offset+i}'} for i in range(200)]}

        async def get_account(method,path,**kwargs):
            assert (method,path)==('GET','/v1/accounts/acc7')
            return {'accountId':'acc7','status':'active'}

        app.api.account_page=account_page
        app.api.call=get_account
        assert (await app.assigned_accounts(['acc7']))['acc7']['status']=='active'
        assert offsets==list(range(0,2000,200))
        app.db.close()
    run(f())


def test_custom_grant_limits_and_daily_quota():
    async def f():
        a=fresh()
        await a.callback(99,{'data':'adm:edit:extend:7:custom'})
        await a.input_draft(99,'13')
        assert a.db.draft(99)['step']=='confirm'
        await a.callback(99,{'data':'adm:apply:'+a.db.draft(99)['data']['nonce']})
        assert a.db.user(7)['expires_at']>now()+17*86400
        await a.callback(99,{'data':'adm:edit:limit_daily:7:custom'})
        await a.input_draft(99,'1')
        await a.callback(99,{'data':'adm:apply:'+a.db.draft(99)['data']['nonce']})
        assert a.db.user(7)['daily_limit']==1
        a.db.queue_task(7,'task1','publish',{'assets':{'accountIds':['acc7']}})
        with pytest.raises(ValueError):a.db.queue_task(7,'task2','publish',{'assets':{'accountIds':['acc7']}})
        a.db.close()
    run(f())


def test_assign_command_requires_confirmation_and_writes_audit():
    async def f():
        app=fresh()

        async def fetch_account(method,path,**kwargs):
            assert (method,path)==('GET','/v1/accounts/smoke-account')
            return {'accountId':'smoke-account','status':'active'}

        app.api.call=fetch_account
        await app.command(99,'/assign 99 smoke-account')
        assert app.db.account_ids(99)==[]
        nonce=app.db.draft(99)['data']['nonce']
        await app.callback(99,{'data':'adm:apply:'+nonce})
        assert app.db.account_ids(99)==['smoke-account']
        assert app.db.audit_entries()[0]['action']=='assign_account'
        await app.command(99,'/unassign 99 smoke-account')
        assert app.db.account_ids(99)==['smoke-account']
        nonce=app.db.draft(99)['data']['nonce']
        await app.callback(99,{'data':'adm:apply:'+nonce})
        assert app.db.account_ids(99)==[]
        assert app.db.audit_entries()[0]['action']=='unassign_account'
        app.db.close()
    run(f())


def test_bulk_assignment_keeps_other_members_accounts_and_skips_inactive():
    async def f():
        app=fresh()
        occupied=[f'owned-{index}' for index in range(200)]
        app.db.assign_available_accounts(8,occupied)
        requested=[]

        async def provider(method,path,**kwargs):
            assert (method,path)==('GET','/v1/accounts')
            assert kwargs['params']['status']=='active'
            offset=kwargs['params']['offset']
            requested.append(offset)
            if offset==0:return {'accounts':[{'accountId':aid,'status':'active'} for aid in occupied]}
            return {'accounts':[{'accountId':'available','status':'active'},
                                {'accountId':'expired','status':'expired'}]}

        app.api.call=provider
        await app.callback(99,{'data':'adm:edit:assign_batch:7:300'})
        assert not requested
        confirm='adm:apply:'+app.db.draft(99)['data']['nonce']
        await app.callback(7,{'data':confirm})
        assert not requested
        await app.callback(99,{'data':confirm})
        await app.callback(99,{'data':confirm})
        assert requested==[0,200]
        assert app.db.account_ids(7)==['acc7','available']
        assert set(occupied).issubset(app.db.account_ids(8))
        assert '1 / 300' in app.tg.sent[-1][1]
        assert app.db.audit_entries()[0]['action']=='assign_batch'
        app.db.close()
    run(f())


def test_owner_callback_routes_survive_reopening_database(tmp_path):
    async def f():
        location=str(tmp_path/'bot.sqlite3')
        config=Config('bot','upstream',99)
        oid='a'*110
        db=ExtendedStore(location)
        app=ProximaApp(config,db,TG(),API())
        markup=app.owner_keyboard([('◇ Аккаунт','account:'+oid,None)])
        opaque=markup['inline_keyboard'][0][0]['callback_data']
        assert opaque.startswith('route:') and len(opaque.encode())<=64
        db.close()

        db=ExtendedStore(location)
        app=ProximaApp(config,db,TG(),API())
        await app.callback(7,{'data':opaque})
        assert app.api.calls==[]
        await app.callback(99,{'data':opaque})
        assert app.api.calls==[('GET','/v1/accounts/'+oid)]
        db.close()
    run(f())


def test_owner_controls_update_state_immediately_and_member_cannot_pause():
    async def f():
        app=fresh()
        tid=app.db.queue_task(7,'Заказ','publish',{'assets':{'accountIds':['acc7']}})
        app.db.transition(tid,'running',upstream_id='remote')
        calls=[]

        async def provider(method,path,**kwargs):
            calls.append((method,path))
            state='paused' if path.endswith('/pause') else 'running'
            return {'id':'remote','state':state}

        app.api.call=provider
        await app.task(7,tid)
        member_markup=app.tg.sent[-1][2]
        assert all('pause:' not in button['callback_data'] for row in member_markup['inline_keyboard'] for button in row)
        await app.callback(7,{'data':'pause:'+tid})
        assert calls==[]
        await app.callback(99,{'data':'adm:control:pause:'+tid})
        assert app.db.task(tid)['state']=='paused'
        assert 'На паузе' in app.tg.sent[-1][1]
        await app.callback(99,{'data':'adm:control:resume:'+tid})
        assert app.db.task(tid)['state']=='running'
        assert 'В работе' in app.tg.sent[-1][1]
        job=app.db.task(tid)
        assert app.save_verified_snapshot(job,{'id':'remote','state':'running','done':5})
        assert not app.save_verified_snapshot(job,{'id':'remote','state':'running','done':2})
        job=app.db.task(tid)
        assert app.save_verified_snapshot(job,{'id':'remote','state':'done','finishedAt':'2026-11-16T16:00:00Z'})
        assert app.db.task(tid)['state']=='completed'
        assert not app.save_verified_snapshot(job,{'id':'remote','state':'error'})
        assert app.db.task(tid)['state']=='completed'
        app.db.close()
    run(f())

def test_owner_sees_all_orders_and_can_cancel_with_confirm():
    async def f():
        a=fresh();d=a.db
        tid=d.queue_task(8,'Another client','fetch',{'assets':{'accountIds':['acc8']}})
        await a.callback(99,{'data':'adm:job:'+tid})
        assert 'Another client' in a.tg.sent[-1][1]
        before=len(a.tg.sent)
        await a.callback(7,{'data':'adm:job:'+tid})
        assert len(a.tg.sent)==before
        await a.callback(99,{'data':'adm:jobedit:cancel:'+tid})
        assert d.task(tid)['state']=='queued'
        nonce=d.draft(99)['data']['nonce']
        await a.callback(99,{'data':'adm:jobapply:'+nonce})
        assert d.task(tid)['state']=='canceled'
        assert d.audit_entries()[0]['action']=='admin_task_cancel'
        d.close()
    run(f())

def test_long_account_id_and_read_only_exports():
    async def f():
        a=fresh();a.db.assign(7,'a'*100)
        await a.admin_user_accounts(99,7)
        k=a.tg.sent[-1][2]
        assert all(len(b['callback_data'].encode())<=64 for row in k['inline_keyboard'] for b in row)
        await a.callback(7,{'data':'adm:export:members'})
        assert not a.tg.files
        await a.callback(99,{'data':'adm:export:members'})
        assert a.tg.files[-1][1].startswith(b'\xef\xbb\xbf')
        assert b'TOKORA' not in a.tg.files[-1][1]
        a.db.close()
    run(f())


def test_owner_can_open_member_results_and_csv_from_the_admin_card():
    async def f():
        app=fresh()
        tid=app.db.queue_task(8,'Сбор команды','fetch',{
            'assets':{'accountIds':['acc8']},'awemeIds':['7000000000000000000']})
        app.db.transition(tid,'paused',upstream_id='remote-collection')
        requests=[]

        async def provider(method,path,**kwargs):
            requests.append((method,path))
            assert method=='GET' and path=='/v1/tasks/remote-collection/entries'
            return {'total':1,'offset':0,'more':False,'items':[{'kind':'comment','text':'Тестовая строка'}]}

        app.api.call=provider
        try:
            await app.callback(99,{'data':'adm:job:'+tid})
            assert {'entries:'+tid,'exports:'+tid,'videos:'+tid,'collected:'+tid,
                    'task_accounts:remote-collection:0'} <= callbacks(app)
            assert 'adm:report:'+tid not in callbacks(app)
            await app.callback(7,{'data':'entries:'+tid})
            assert not requests
            await app.callback(99,{'data':'entries:'+tid})
            assert 'Тестовая строка' in app.tg.sent[-1][1]
            assert 'adm:job:'+tid in callbacks(app)
            await app.callback(99,{'data':'videos:'+tid})
            assert 'tiktok.com' in app.tg.sent[-1][1]
            assert 'adm:job:'+tid in callbacks(app)
            await app.callback(99,{'data':'exports:'+tid})
            assert 'export_csv:'+tid in callbacks(app)
            await app.callback(99,{'data':'export_csv:'+tid})
            assert 'Тестовая строка' in app.tg.files[-1][1].decode('utf-8-sig')
        finally:
            app.db.close()
    run(f())


def test_owner_queue_includes_paused_and_stopping_orders():
    async def f():
        app=fresh()
        try:
            cases=(('На паузе','paused',7,'acc7'),('Остановка','stopping',8,'acc8'))
            ids=[]
            for name,state,uid,account in cases:
                tid=app.db.queue_task(uid,name,'publish',{'assets':{'accountIds':[account]}})
                app.db.transition(tid,state,upstream_id='up-'+tid)
                ids.append(tid)
            await app.callback(99,{'data':'adm:liveq:0'})
            assert {'adm:job:'+tid for tid in ids} <= callbacks(app)
            assert '2 / 0' in app.tg.sent[-1][1]
            await app.callback(99,{'data':'adm:jobs:stopping:0'})
            assert 'adm:job:'+ids[1] in callbacks(app)
            assert 'adm:job:'+ids[0] not in callbacks(app)
        finally:
            app.db.close()
    run(f())


def test_owner_cabinet_links_and_folder_pagination():
    async def f():
        app=fresh()

        async def provider(method,path,**kwargs):
            assert (method,path)==('GET','/v1/account-folders')
            return {'available':True,'items':[{'id':f'{i:08x}','name':f'Папка {i}'} for i in range(1,27)]}

        app.api.call=provider
        try:
            await app.callback(99,{'data':'adm:api'})
            assert {'settings','materials'} <= callbacks(app)
            await app.callback(99,{'data':'settings'})
            assert {'owner_show:settings','owner_edit:settlement'} <= callbacks(app)
            await app.callback(99,{'data':'materials'})
            assert 'owner_show:asset_comments' in callbacks(app)
            await app.callback(99,{'data':'adm:foldersync:7'})
            for i in (1,2):
                route=f'adm:foldersync:7:{i}'
                assert route in callbacks(app)
                await app.callback(99,{'data':route})
            assert 'adm:fsp:7:0000001a' in callbacks(app)
            assert 'adm:foldersync:7:3' not in callbacks(app)
        finally:
            app.db.close()
    run(f())


def test_contact_profile_is_updated_on_message():
    async def f():
        a=fresh()
        await a.handle({'message':{'from':{'id':18,'first_name':'Example','username':'worker18'},
                                  'chat':{'type':'private'},'text':'/start'}})
        assert a.db.user(18)['username']=='worker18'
        a.db.close()
    run(f())


def test_photo_toggle_shared_templates_live_queue_folder_sync_and_pool_summary():
    async def f():
        a=fresh()
        try:
            # 1. Per-user photo access toggle
            assert not a.allow_photos(7)
            assert a.allow_photos(99)
            await a.callback(99,{'data':'adm:toggle_photo:7'})
            assert a.allow_photos(7)
            assert 'Включены' in a.tg.sent[-1][1]
            await a.callback(7,{'data':'adm:toggle_photo:7'})
            assert a.allow_photos(7)  # non-owner cannot toggle
            await a.callback(99,{'data':'adm:toggle_photo:7'})
            assert not a.allow_photos(7)

            # 2. Shared templates: member can read/use owner's shared template, cannot edit/delete
            tid=a.db.save_template(99,'Общий пресет','publish',{'comments':['Привет'], 'replies':['Да']})
            await a.callback(99,{'data':f'tpl_share:{tid}'})
            assert a.db.template(99,tid)['is_shared']
            assert any(t['template_id']==tid for t in a.db.templates(7))
            assert a.db.template(7,tid)['template_id']==tid
            assert a.db.template(7,tid,owned_only=True) is None
            await a.callback(7,{'data':f'tpl:{tid}'})
            assert 'Общий' in a.tg.sent[-1][1]
            assert 'tpl_delete:' not in str(a.tg.sent[-1][2])
            await a.callback(7,{'data':f'tpl_delete_confirm:{tid}'})
            assert a.db.template(99,tid) is not None

            # 3. Live queue & dispatch pause toggle
            job_id=a.db.queue_task(7,'Очередь тест','publish',{'assets':{'accountIds':['acc7']},'awemeIds':['1']})
            await a.callback(99,{'data':'adm:qpause:on'})
            assert a.db.dispatch_paused()
            assert 'На паузе' in a.tg.sent[-1][1]
            await a.callback(99,{'data':'adm:liveq:0'})
            assert 'Очередь тест' in a.tg.sent[-1][1]
            await a.callback(99,{'data':'adm:lqpause:off'})
            assert not a.db.dispatch_paused()
            a.db.cancel_queued(job_id,7)

            # 4. 1-click folder sync (add free & exact 1-in-1) + pool_summary + folder-aware assign_batch
            async def api_call(method,path,**kwargs):
                if path=='/v1/account-folders':
                    return {'available':True,'unassigned':1,'items':[{'id':'1234abcd','name':'Траф7','count':3,'active':3}]}
                if path=='/v1/accounts':
                    return {'accounts':[
                        {'accountId':'acc7','status':'active','folderId':'1234abcd'},
                        {'accountId':'f1','status':'active','folderId':'1234abcd'},
                        {'accountId':'f2','status':'active','folderId':'1234abcd'},
                        {'accountId':'other_folder','status':'active','folderId':'9999ffff'},
                    ],'total':4}
                return {}

            async def in_folder(fid):
                assert fid=='1234abcd'
                return {
                    'f1':{'accountId':'f1','status':'active','folderId':fid},
                    'acc8':{'accountId':'acc8','status':'active','folderId':fid},
                    'other_busy':{'accountId':'other_busy','status':'active','folderId':fid},
                }

            async def account_page(offset,limit=200,**kwargs):
                return {'accounts':[
                    {'accountId':'f1','status':'active','folderId':'1234abcd'},
                    {'accountId':'other_busy','status':'active','folderId':'1234abcd'},
                ],'total':2}

            a.api.call=api_call
            a.api.accounts_in_folder=in_folder
            a.api.account_page=account_page

            await a.callback(99,{'data':'adm:foldersync:7'})
            assert 'Траф7' in str(a.tg.sent[-1][2])
            await a.callback(99,{'data':'adm:fsp:7:1234abcd'})
            assert 'Свободных для добавления' in a.tg.sent[-1][1]
            await a.callback(99,{'data':'adm:fsg:7:1234abcd:e'})
            assert set(a.db.account_ids(7))=={'f1','other_busy'}
            assert a.db.account_ids(8)==['acc8']
            assert 'Синхронизация выполнена частично' in a.tg.sent[-1][1]

            # pool_summary exposes folder consistency to owner
            await a.pool_summary(99,7)
            assert 'Папки в кабинете' in a.tg.sent[-1][1] and '1234abcd' in a.tg.sent[-1][1]

            # assign_batch stays within user's existing folder
            a.db.unassign(7,'f1')
            a.db.unassign(7,'other_busy')
            a.db.assign(7,'acc7')
            added=await a.assign_batch(7,5)
            assert added==2
            assert set(a.db.account_ids(7))=={'acc7','f1','f2'}
            assert 'other_folder' not in a.db.account_ids(7)
        finally:
            a.db.close()
    run(f())
