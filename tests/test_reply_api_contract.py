import asyncio
import json

import pytest

from proxima_bot.api import ApiError
from proxima_bot.catalog import resolve_payload_targets, validate_reply_links
from proxima_bot.config import Config
from proxima_bot.worker import Worker
from test_extended import button_callback, collected_cid, collected_entry, collected_link, new_app
from test_worker import FakeTelegram, FakeTokora


def test_collection_can_preview_and_queue_one_native_order_with_100000_roots():
    async def run():
        app=new_app()
        targets=[]

        class Provider(FakeTokora):
            async def call(self,method,path,**kwargs):
                if method=='GET' and path=='/v1/tasks/collection/entries':
                    params=kwargs['params']
                    assert params['kind']=='comment'
                    offset,size=params['offset'],params['limit']
                    items=[collected_entry(index) for index in range(offset,min(offset+size,100_001))]
                    if offset==0:items[0]['kind']='reply'
                    return {'items':items,'more':offset+size<100_001,'offset':offset}
                assert method=='POST' and path=='/v1/tasks/collection/reply-plan'
                targets.extend(kwargs['payload']['targetIds'])
                return {'selected':len(targets),'videos':1}

        app.api=Provider()
        try:
            source=app.db.queue_task(7,'Сбор','fetch',{'assets':{'accountIds':['acc7']}})
            app.db.transition(source,'completed',upstream_id='collection')
            await app.callback(7,{'data':'source_reply:'+source})
            await app.input_draft(7,'100000')
            await app.input_draft(7,'Ответ')
            await app.callback(7,{'data':button_callback(app,'source:skip:')})
            assert len(targets)==100_000 and targets[0]==collected_cid(1) and targets[-1]==collected_cid(100_000)
            await app.callback(7,{'data':button_callback(app,'source:submit:')})
            order=app.db.recent_tasks(7)[0]
            assert order['state']=='queued'
            await Worker(app.db,app.api,FakeTelegram(),Config('fake','fake',99,dispatch_enabled=True)).tick()
            sent,=app.api.started
            assert sent['kind']=='publish' and sent['sourceJob']=='collection' and sent['targetIds']==targets
            assert not {'replyLinks','replySourceMode'} & sent.keys()
        finally:
            app.db.close()
    asyncio.run(run())


def test_plan_rejects_removed_target_in_old_draft_before_queueing():
    async def run():
        app=new_app()
        async def rejected_plan(method,path,**kwargs):
            if method=='POST':
                assert kwargs['payload']['targetIds']==[collected_cid(2)]
                raise ApiError('invalid_target',400)
            return {'items':[collected_entry(1),collected_entry(2)],'more':False}

        app.api.call=rejected_plan
        try:
            source=app.db.queue_task(7,'Сбор','fetch',{'assets':{'accountIds':['acc7']}})
            app.db.transition(source,'completed',upstream_id='collection')
            app.save_wizard_step(7,'source_reply','review',{'source_task':source,'replies':['Ответ'],
                                  'target_links':[collected_link(2)],'target_count':1})
            old_token=app.db.draft(7)['data']['wizard_token']
            await app.source_reply_preview(7)
            await app.callback(7,{'data':'source:submit:'+old_token})
            assert len(app.db.recent_tasks(7))==1
            assert app.db.draft(7)['data']['target_ids']==[collected_cid(2)]
        finally:
            app.db.close()
    asyncio.run(run())


def test_direct_links_count_nonempty_input_before_deduplication():
    pair=collected_link()
    assert len(validate_reply_links(['']*2700+[pair]*300))==300
    long_link='https://www.tiktok.com/@author/video/7420000000000000000?comment_id=7430000000000000001&extra='
    long_link=long_link.ljust(2048,'a')
    assert validate_reply_links([long_link])==[long_link]
    with pytest.raises(ValueError,match='2048'):
        validate_reply_links([long_link+'a'])
    with pytest.raises(ValueError,match='3000'):
        validate_reply_links(['']*3000+[pair])
    with pytest.raises(ValueError,match='300 непустых'):
        asyncio.run(resolve_payload_targets(None,{'replyLinks':[pair]*301}))
    assert asyncio.run(resolve_payload_targets(None,{'replyLinks':[pair,pair]}))['replyLinks']==[pair]


def test_resolver_error_keeps_line_numbers_on_failed_order_and_stops_launch():
    async def run():
        app=new_app()
        class Provider(FakeTokora):
            async def call(self,method,path,**kwargs):
                assert method=='POST' and path=='/v1/tasks/reply-links/resolve'
                first, second = kwargs['payload']['links']
                return {'items': [
                    {'line': 2, 'input': second, 'error': 'invalid_url'},
                    {'line': 1, 'input': first, 'value': collected_link()},
                ], 'ok': 1, 'failed': 1}

        app.api=Provider()
        try:
            tid=app.db.queue_task(7,'Ответы','publish',{'assets':{'accountIds':['acc7']},
                 'replyLinks':[collected_link(),'https://vt.tiktok.com/example/']})
            await Worker(app.db,app.api,FakeTelegram(),Config('fake','fake',99,dispatch_enabled=True)).tick()
            order=app.db.task(tid)
            assert order['state']=='failed' and not app.api.started
            assert json.loads(order['preflight_json'])['reply_link_error_lines']==[2]
            await app.task(7,tid)
            assert 'Ошибки в непустых строках' in app.tg.out[-1][1]
            assert 'Заменить ссылки' in str(app.tg.out[-1][2])
        finally:
            app.db.close()
    asyncio.run(run())


def test_resolver_rejects_a_result_for_the_wrong_input():
    class Provider:
        async def call(self, method, path, **kwargs):
            return {'items': [{'line': 1, 'input': 'https://vt.tiktok.com/other/',
                               'value': collected_link()}], 'ok': 1, 'failed': 0}

    with pytest.raises(ApiError, match='invalid_response'):
        asyncio.run(resolve_payload_targets(Provider(), {'replyLinks': ['https://vt.tiktok.com/example/']}))
