import asyncio
import json
from proxima_bot.config import Config
from proxima_bot.db import Store
from proxima_bot.bot import BotApp

class TelegramStub:
    def __init__(self):self.sent=[]
    async def send(self,uid,text,keyboard=None,**kw):
        self.sent.append((uid,text,keyboard))
    async def rich(self,uid,rich,plain,keyboard=None):
        self.sent.append((uid,rich,keyboard))
    async def answer_callback(self,callback_id,text=''):pass

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


def test_handle_overlaps_answer_callback_with_callback_work():
    from proxima_bot.api import TelegramError

    async def run():
        cfg = Config('fake', 'fake', 99)
        db = Store(':memory:')
        db.assign(7, 'a')
        tid = db.queue_task(7, 'overlap-task', 'publish', {'assets': {'accountIds': ['a']}})

        started = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()

        class BlockingAnswerTG(TelegramStub):
            async def answer_callback(self, callback_id, text=''):
                started.set()
                await release.wait()
                finished.set()
                raise TelegramError(400, 'query is too old')

        tg = BlockingAnswerTG()
        app = BotApp(cfg, db, tg, ApiStub())
        orig_callback = app.callback

        async def wrapped_callback(uid, cb):
            assert started.is_set()
            assert not finished.is_set()
            await orig_callback(uid, cb)
            release.set()

        app.callback = wrapped_callback
        try:
            await asyncio.wait_for(
                app.handle({'callback_query': {'id': 'cb1', 'from': {'id': 7}, 'message': {'chat': {'type': 'private'}}, 'data': 'task:' + tid}}),
                timeout=1.0,
            )
            assert finished.is_set()
            assert 'overlap-task' in tg.sent[-1][1]
        finally:
            db.close()

    asyncio.run(run())
