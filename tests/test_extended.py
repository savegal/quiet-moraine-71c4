import asyncio
import json
from datetime import datetime,timedelta
from proxima_bot.advanced_db import ExtendedStore
from proxima_bot.catalog import MODES,parse_options,parse_targets,parse_feedwarm,build_payload
from proxima_bot.config import Config
from proxima_bot.extended import ProximaApp
from proxima_bot.operations import operation_plan

class FakeTG:
    def __init__(self):self.out=[];self.documents=[]
    async def send(self,uid,text,markup=None,**kw):self.out.append((uid,text,markup))
    async def rich(self,uid,rich,plain,markup=None):self.out.append((uid,rich,markup))
    async def attachment(self,uid,content,filename,caption=''):self.documents.append((uid,content,filename))

class FakeAPI:
    def __init__(self):self.calls=[]
    async def resolve(self,s):return '7420000000000000000'
    async def call(self,method,path,**kw):
        self.calls.append((method,path,kw))
        if path.endswith('/reply-plan'):return {'planned':3}
        if path.endswith('/entries'):return {'items':[{'kind':'comment','text':'Hello','url':'https://tiktok.com/@author/video/1234','status':'live'}], 'total':1,'more':False}
        if path.endswith('/reply-links/resolve'):
            return {'items':[{'value':'1234567890123456789:1234567890123456789'}],'ok':1,'failed':0}
        if path.startswith('/v1/accounts'):return {'accounts':[],'total':0}
        if path.startswith('/v1/proxies'):return {'items':[]}
        if path.startswith('/v1/posts/drafts'):return {'items':[],'total':0}
        if path=='/v1/tasks':return {'items':[]}
        if path.endswith('/publication-report'):return {'comments':{'published':2,'attempts':3},'replies':{'published':0,'attempts':0}}
        return {'ok':True,'id':'provider1'}


def new_app():
    db=ExtendedStore(':memory:');db.grant(7,30);db.assign(7,'acc7');db.assign(8,'acc8')
    return ProximaApp(Config('token','key',99),db,FakeTG(),FakeAPI())

def button_callback(app,prefix):
    rows=app.tg.out[-1][2]['inline_keyboard']
    return next(button['callback_data'] for row in rows for button in row
                if button['callback_data'].startswith(prefix))


def test_all_kinds_available_and_no_auto_filter():
    assert set(MODES)=={'publish','reply_links','fetch','commentlike','keyword_reply','feedwarm','usernames','check'}
    assert parse_options('{}')=={}
    assert parse_options('{"uniqueText":false}')=={'uniqueText':False}
    async def run():
        api=FakeAPI()
        p=await build_payload(api,'publish',{'name':'x','videos':['7420000000000000000'],
             'comments':['в тг напиши','скину всем'], 'replies':[],'options':{}},['a'])
        assert p['assets']['commentTexts']==['в тг напиши','скину всем']
        assert 'uniqueText' not in p.get('settings',{})
        l=await build_payload(api,'reply_links',{'name':'x','reply_links':['https://vt.tiktok.com/ab'],
              'replies':['ok']},['a'])
        assert l['replyLinks']==['1234567890123456789:1234567890123456789']
        k=await build_payload(api,'keyword_reply',{'name':'x','keywords':['key'],'replies':['text']},['a'])
        assert k['kind']=='keyword_reply' and k['settings']['fetchKeywords']==['key']
        f=await build_payload(api,'feedwarm',{'name':'x','warm_json':{'keywords':['tutorial'],'dryRun':True}},['a'])
        assert f['feedWarm']['dryRun']
        link='https://vt.tiktok.com/valid/'
        c=await build_payload(api,'commentlike',{'name':'x','like_links':[link],'likes':4},['a'])
        assert c['likesPerComment']==4 and c['likeLinks']==[link]
    asyncio.run(run())


def test_wizard_complete_and_task_isolation():
    async def run():
        app=new_app()
        await app.begin(7,'publish')
        await app.input_draft(7,'Новый тест')
        await app.input_draft(7,'7420000000000000000')
        await app.input_draft(7,'Скину всем в тг')
        for _ in range(3): # replies, options, when
            await app.callback(7,{'data':button_callback(app,'wiz:skip:')})
        await app.callback(7,{'data':button_callback(app,'wiz:submit:')})
        task=app.db.recent_tasks(7)[0]
        p=json.loads(task['payload_json'])
        assert p['assets']['accountIds']==['acc7']
        assert p['assets']['commentTexts']==['Скину всем в тг']
        assert 'uniqueText' not in p.get('settings',{})
        assert not app.db.recent_tasks(8)
        await app.callback(8,{'data':'task:'+task['task_id']})
        assert 'Нет доступа' in app.tg.out[-1][1]
        app.db.close()
    asyncio.run(run())


def test_schedule_not_dispatched_early():
    db=ExtendedStore(':memory:');db.grant(7,30);db.assign(7,'a')
    tid=db.queue_task(7,'test','publish',{'assets':{'accountIds':['a']}})
    db.set_schedule(tid,7,datetime.now().timestamp().__int__()+86400)
    assert db.next_eligible(99) is None
    assert db.cancel_queued(tid,7)
    db.close()


def test_non_admin_cannot_edit_shared_media():
    async def run():
        app=new_app()
        await app.callback(7,{'data':'owner_go:posts_clear_history'})
        await app.callback(7,{'data':'owner_edit:settings'})
        await app.callback(7,{'data':'upload:video'})
        await app.callback(7,{'data':'owner_show:proxies'})
        assert not app.api.calls
        await app.callback(99,{'data':'owner_go:posts_clear_history'})
        assert not app.api.calls
        await app.callback(99,{'data':'owner_confirm:posts_clear_history'})
        assert app.api.calls[-1][1]=='/v1/posts/queue/clear-history'
        app.db.close()
    asyncio.run(run())


def test_template_reuse_regenerates_request_and_pool():
    async def run():
        app=new_app()
        original={'kind':'publish','name':'template','requestId':'old',
                  'assets':{'accountIds':['acc7'],'commentTexts':['note']},'awemeIds':['7420000000000000000']}
        tid=app.db.save_template(7,'template','publish',{'job':original})
        await app.from_template(7,tid)
        assert not app.db.recent_tasks(7)
        submit=button_callback(app,'wiz:submit:')
        app.db.unassign(7,'acc7')
        app.db.assign(7,'replacement')
        await app.callback(7,{'data':submit})
        await app.callback(7,{'data':submit})
        row=app.db.recent_tasks(7)[0]
        assert len(app.db.recent_tasks(7))==1
        body=json.loads(row['payload_json'])
        assert body['requestId']!='old'
        assert body['assets']['accountIds']==['replacement']
        assert app.db.template(8,tid) is None
        app.db.close()
    asyncio.run(run())


def test_export_and_source_reply_local_ownership():
    async def run():
        app=new_app()
        tid=app.db.queue_task(7,'base','fetch',{'assets':{'accountIds':['acc7']}})
        app.db.transition(tid,'completed',upstream_id='upstream1')
        await app.callback(7,{'data':'source_reply:'+tid})
        await app.input_draft(7,'ответ')
        await app.callback(7,{'data':button_callback(app,'source:skip:')})
        await app.callback(7,{'data':button_callback(app,'source:submit:')})
        row=app.db.recent_tasks(7)[0]
        assert row['kind']=='publish'
        data=json.loads(row['payload_json'])
        assert data['sourceJob']=='upstream1' and data['assets']['accountIds']==['acc7']
        await app.export_file(7,tid,'csv')
        assert b'Hello' in app.tg.documents[-1][1]
        await app.export_file(7,tid,'links')
        assert b'https://tiktok.com/' in app.tg.documents[-1][1]
        await app.export_file(8,tid,'csv')
        assert len(app.tg.documents)==2
        app.db.close()
    asyncio.run(run())


def test_wizard_back_preserves_input_and_expires_old_confirmation():
    async def run():
        app=new_app()
        await app.begin(7,'fetch')
        await app.input_draft(7,'Проверка')
        await app.input_draft(7,'7420000000000000000')
        await app.callback(7,{'data':button_callback(app,'wiz:skip:')})
        await app.callback(7,{'data':button_callback(app,'wiz:skip:')})
        old_submit=button_callback(app,'wiz:submit:')
        await app.callback(7,{'data':button_callback(app,'wiz:back:')})
        assert app.db.draft(7)['data']['videos']==['7420000000000000000']
        await app.callback(7,{'data':button_callback(app,'wiz:skip:')})
        new_submit=button_callback(app,'wiz:submit:')
        assert old_submit!=new_submit
        await app.callback(7,{'data':old_submit})
        assert not app.db.recent_tasks(7)
        assert 'Кнопка устарела' in app.tg.out[-1][1]
        await app.callback(7,{'data':new_submit})
        assert len(app.db.recent_tasks(7))==1
        app.db.close()
    asyncio.run(run())


def test_long_owner_account_buttons_open_only_for_owner():
    async def run():
        app=new_app()
        account_id='a'*110
        async def page(offset,limit):
            return {'accounts':[{'accountId':account_id,'status':'active','profile':{'username':'testing'}}],
                    'total':11}
        app.api.account_page=page
        await app.accounts(99)
        callback=button_callback(app,'route:')
        assert len(callback.encode())<=64
        assert button_callback(app,'accounts_page:1')
        await app.callback(7,{'data':callback})
        assert not app.api.calls
        await app.callback(99,{'data':callback})
        check=button_callback(app,'route:')
        await app.callback(99,{'data':check})
        assert app.api.calls[-1][:2]==('POST',f'/v1/accounts/{account_id}/check')
        app.db.close()
    asyncio.run(run())


def test_operation_plan_safe_and_schema_aware():
    op=operation_plan('getPostsById',{'path':{'id':'a-123'}})
    assert op['method']=='GET' and op['path']=='/v1/posts/a-123'
    try:operation_plan('deletePostsDraftsById',{'path':{'id':'../escape'}})
    except ValueError:pass
    else:raise AssertionError('unsafe id accepted')
    try:operation_plan('postTasks',{'body':{'kind':'publish','requestId':'i'}})
    except ValueError:pass
    else:raise AssertionError('bypass of queue accepted')
