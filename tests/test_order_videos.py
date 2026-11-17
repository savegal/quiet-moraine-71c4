import asyncio
import json

import pytest

from proxima_bot.extended import order_video_links, public_video_link
from test_extended import new_app


VIDEO='7420000000000000000'
LINK='https://www.tiktok.com/share/video/'+VIDEO


def queue(app,kind,payload):
    return app.db.queue_task(7,'Заказ',kind,{'assets':{'accountIds':['acc7']},**payload})


def test_video_targets_accept_ids_comment_pairs_and_sanitized_links():
    assert public_video_link(VIDEO)==LINK
    assert public_video_link(VIDEO+':7430000000000000000')==LINK
    assert public_video_link('https://www.tiktok.com/@author/video/'+VIDEO+'?token=secret')==LINK
    assert public_video_link('https://vt.tiktok.com/abc/?token=secret')=='https://vt.tiktok.com/abc/'
    assert public_video_link('https://evil.example/video/'+VIDEO)==''
    assert public_video_link('javascript:alert(1)')==''
    assert order_video_links({'awemes':[{'awemeId':VIDEO,'url':'https://evil.example'}]},
                             {'awemeIds':[VIDEO],'replyLinks':[VIDEO+':123'],'likeLinks':[LINK]})==[LINK]


@pytest.mark.parametrize('kind',['publish','keyword_reply','fetch','commentlike','usernames','feedwarm'])
def test_local_cards_offer_original_videos_before_execution(kind):
    app=new_app()
    try:
        tid=queue(app,kind,{'awemeIds':[VIDEO]})
        assert 'videos:'+tid in str(app.render_task(app.db.task(tid))[2])
    finally:
        app.db.close()


def test_queued_order_opens_original_without_api_and_hides_it_from_other_users():
    async def run():
        app=new_app()
        try:
            tid=queue(app,'publish',{'awemeIds':[VIDEO]})
            await app.callback(7,{'data':'videos:'+tid})
            assert LINK in app.tg.out[-1][1] and 'task:'+tid in str(app.tg.out[-1][2])
            assert not app.api.calls
            await app.callback(8,{'data':'videos:'+tid})
            assert LINK not in app.tg.out[-1][1] and 'не найден' in app.tg.out[-1][1]
            assert not app.api.calls
        finally:
            app.db.close()
    asyncio.run(run())


def test_workspace_originals_fall_back_to_ids_and_old_video_button_still_works():
    async def run():
        app=new_app()
        async def task(tid):
            assert tid=='remote'
            return {'id':tid,'name':'С сайта','kind':'commentlike','state':'completed',
                    'awemes':[{'awemeId':VIDEO}]}
        app.api.task=task
        try:
            card=await app.read_workspace_task('remote')
            assert 'videos:remote' in str(app.render_workspace_task(card)[2])
            await app.callback(99,{'data':'videos:remote'})
            assert LINK in app.tg.out[-1][1]
            assert 'tokora_task:remote' in str(app.tg.out[-1][2])
            await app.callback(99,{'data':'wt:videos:remote'})
            assert LINK in app.tg.out[-1][1]
        finally:
            app.db.close()
    asyncio.run(run())


def test_originals_paginate_all_targets_and_deduplicate():
    async def run():
        app=new_app()
        ids=[str(int(VIDEO)+i) for i in range(25)]
        try:
            tid=queue(app,'publish',{'awemeIds':ids+ids})
            await app.callback(7,{'data':'videos:'+tid})
            text=app.tg.out[-1][1]
            assert text.count('<a href=')==10 and 'Всего · 25' in text
            assert 'videos:'+tid+':1' in str(app.tg.out[-1][2])
            await app.callback(7,{'data':'videos:'+tid+':1'})
            text=app.tg.out[-1][1]
            assert text.count('<a href=')==10 and 'Открыть видео 11' in text
            assert ids[0] not in text and ids[10] in text
            await app.callback(7,{'data':'videos:'+tid+':9999999'})
            assert app.tg.out[-1][1].count('<a href=')==5 and 'стр. 3' in app.tg.out[-1][1]
        finally:
            app.db.close()
    asyncio.run(run())


@pytest.mark.parametrize('source_key',['sourceJob','likeSourceJob'])
def test_source_orders_open_videos_of_the_original_job(source_key):
    async def run():
        app=new_app()
        async def task(tid):
            assert tid=='source'
            return {'id':tid,'awemes':[{'awemeId':VIDEO}]}
        app.api.task=task
        try:
            tid=queue(app,'publish',{source_key:'source'})
            await app.order_videos(7,tid)
            assert LINK in app.tg.out[-1][1]
        finally:
            app.db.close()
    asyncio.run(run())


def test_running_local_order_recovers_original_from_remote_snapshot():
    async def run():
        app=new_app()
        async def task(tid):
            return {'id':tid,'awemes':[{'awemeId':VIDEO}]}
        app.api.task=task
        try:
            tid=queue(app,'publish',{})
            app.db.transition(tid,'running',upstream_id='remote')
            await app.order_videos(7,tid)
            assert LINK in app.tg.out[-1][1]
        finally:
            app.db.close()
    asyncio.run(run())


def test_collection_summary_contains_openable_videos_in_both_message_formats():
    async def run():
        app=new_app()
        async def provider(method,path,**kwargs):
            assert (method,path)==('GET','/v1/tasks/remote/collected/videos')
            return {'comments':4,'replies':2,'items':[{'awemeId':VIDEO,'comments':4,'replies':2}]}
        app.api.call=provider
        rendered=[]
        async def rich(uid,html,plain,markup):
            rendered.extend([html,plain,json.dumps(markup)])
        app.tg.rich=rich
        try:
            tid=queue(app,'fetch',{'awemeIds':[VIDEO]})
            app.db.transition(tid,'completed',upstream_id='remote')
            await app.collected_videos(7,tid)
            assert all(LINK in text for text in rendered[:2])
            assert 'videos:'+tid in rendered[2]
        finally:
            app.db.close()
    asyncio.run(run())
