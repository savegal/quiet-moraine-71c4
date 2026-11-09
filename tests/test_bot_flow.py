import asyncio
import json
from proxima_bot.config import Config
from proxima_bot.db import Store
from proxima_bot.bot import BotApp

class TelegramStub:
    def __init__(self):self.sent=[];self.invoices=[];self.prechecks=[]
    async def send(self,uid,text,keyboard=None,**kw):
        self.sent.append((uid,text,keyboard))
    async def rich(self,uid,rich,plain,keyboard=None):
        self.sent.append((uid,rich,keyboard))
    async def answer_callback(self,callback_id,text=''):pass
    async def invoice(self,uid,payload,stars,days):self.invoices.append((uid,payload,stars,days))
    async def precheckout(self,qid,ok,reason=''):self.prechecks.append((qid,ok,reason))

class ApiStub:
    async def resolve(self,url):return '7420000000000000000'


def test_telegram_wizard_creates_isolated_queue():
    async def run():
        cfg=Config('fake','fake',99,dispatch_enabled=False)
        db=Store(':memory:');db.grant(7,30);db.assign(7,'acc-7');db.assign(8,'acc-8')
        tg=TelegramStub();app=BotApp(cfg,db,tg,ApiStub())
        await app.begin(7,'publish')
        await app.input_draft(7,'Проверка')
        await app.input_draft(7,'https://vt.tiktok.com/ABcd01/')
        await app.input_draft(7,'Пример положительного комментария')
        await app.callback(7,{'data':'skipreplies'})
        await app.callback(7,{'data':'submit'})
        tasks=db.recent_tasks(7)
        assert len(tasks)==1
        payload=json.loads(tasks[0]['payload_json'])
        assert payload['assets']['accountIds']==['acc-7']
        assert 'acc-8' not in tasks[0]['payload_json']
        assert payload['disableReplyPhotos'] is True
        assert not db.recent_tasks(8)
        assert db.draft(7) is None
        db.close()
    asyncio.run(run())


def test_precheckout_rejects_fake_amount_and_payment_idempotent():
    async def run():
        cfg=Config('fake','fake',99,stars_price=123)
        db=Store(':memory:');tg=TelegramStub();app=BotApp(cfg,db,tg,ApiStub())
        db.register(7)
        await app.callback(7,{'data':'buy'})
        payload=tg.invoices[-1][1]
        await app.handle({'pre_checkout_query':{'id':'q1','from':{'id':7},'invoice_payload':payload,'currency':'XTR','total_amount':122}})
        assert tg.prechecks[-1][1] is False
        await app.handle({'pre_checkout_query':{'id':'q2','from':{'id':7},'invoice_payload':payload,'currency':'XTR','total_amount':123}})
        assert tg.prechecks[-1][1] is True
        payment={'chat':{'type':'private'},'from':{'id':7},'successful_payment':{
          'invoice_payload':payload,'currency':'XTR','total_amount':123,'telegram_payment_charge_id':'paid-1'}}
        await app.handle({'message':payment})
        exp=db.user(7)['expires_at']
        await app.handle({'message':payment})
        assert db.user(7)['expires_at']==exp
        db.close()
    asyncio.run(run())


def test_expired_user_can_open_existing_task():
    async def run():
        cfg=Config('fake','fake',99)
        db=Store(':memory:');db.assign(7,'a')
        tid=db.queue_task(7,'old','publish',{'assets':{'accountIds':['a']}})
        tg=TelegramStub();app=BotApp(cfg,db,tg,ApiStub())
        await app.handle({'callback_query':{'id':'callback','from':{'id':7},'message':{'chat':{'type':'private'}},'data':'task:'+tid}})
        assert 'old' in tg.sent[-1][1]
        db.close()
    asyncio.run(run())
