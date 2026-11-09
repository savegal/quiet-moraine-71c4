"""Owner console permission, idempotency, subscription and usability regression tests."""
import asyncio
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

def test_payment_event_recorded_once():
    a=fresh();d=a.db
    invoice=d.create_invoice(7,50)
    assert d.paid_invoice(7,invoice,50,'xtr-charge',30)
    assert d.paid_invoice(7,invoice,50,'xtr-charge',30) is None
    assert len([e for e in d.subscriptions(7) if e['action']=='stars'])==1
    d.close()

def test_contact_profile_is_updated_on_message():
    async def f():
        a=fresh()
        await a.handle({'message':{'from':{'id':18,'first_name':'Example','username':'worker18'},
                                  'chat':{'type':'private'},'text':'/start'}})
        assert a.db.user(18)['username']=='worker18'
        a.db.close()
    run(f())
