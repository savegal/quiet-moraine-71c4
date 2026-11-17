import asyncio
import json

import pytest

from proxima_bot.presentation import REPLY_CHECK_NOTE, task_rich
from test_extended import new_app, button_callback
from test_release_flows import TelegramTransport


@pytest.mark.parametrize(('kind','snapshot','label'), [
    ('publish',{'awemes':[{'comments':8,'replies':2}],'attempts':20},'Комментарии'),
    ('keyword_reply',{'awemes':[{'comments':0,'replies':3}],'attempts':8},'Ответы'),
    ('fetch',{'collected':50,'scanned':100},'Собрано'),
    ('commentlike',{'liked':10,'likeTotal':20,'likeWaiting':10},'Лайков подтверждено'),
    ('usernames',{'usernameStats':{'names':8,'candidates':3,'mentions':20}},'Telegram-юзернеймы'),
    ('feedwarm',{'warm':{'searchMatched':15,'accountsDone':2,'accounts':3}},'Видео найдено'),
    ('import',{'imported':5,'importCreated':4,'importUpdated':1,'importWebOnly':2},'Импортировано'),
    ('check',{'checked':10,'live':8,'expired':2},'Проверено'),
])
def test_every_workspace_order_has_metrics_controls_and_private_results(kind,snapshot,label):
    async def run():
        app=new_app()
        remote={'id':'remote-job','name':'Заказ','kind':kind,'state':'running','done':2,'total':4,**snapshot}
        calls=[]
        async def task(tid):
            calls.append(('task',tid))
            return remote
        async def provider(method,path,**kwargs):
            calls.append((method,path))
            return {'items':[{'kind':'comment','text':'Результат','outcome':'collected'}],'total':1,'more':False}
        app.api.task=task
        app.api.call=provider
        try:
            await app.workspace_task(99,'remote-job')
            assert label in app.tg.out[-1][1]
            assert 'wt:stopask:' in str(app.tg.out[-1][2])
            assert 'exports:remote-job' in str(app.tg.out[-1][2])
            before=len(calls)
            await app.task_entries(7,'remote-job')
            assert len(calls)==before
            await app.task_entries(99,'remote-job')
            assert 'Результат' in app.tg.out[-1][1]
            assert 'tokora_task:remote-job' in str(app.tg.out[-1][2])
            await app.export_file(99,'remote-job','csv')
            assert len(app.tg.documents)==1
        finally:
            app.db.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind',['video','photo'])
@pytest.mark.parametrize('rich',[True,False])
def test_media_card_auto_updates_in_place_until_final_result(kind,rich):
    async def run():
        app=new_app()
        app.tg=TelegramTransport(rich)
        app.live_cards.app=app
        post={'id':'post','name':'Публикация','kind':kind,'outcome':'uploading','attempts':1,'maxAttempts':3}
        async def provider(method,path,**kwargs):
            assert (method,path)==('GET','/v1/posts/post')
            return post
        app.api.call=provider
        try:
            await app.post_info(99,'post')
            assert app.live_cards.cards[99].kind=='media_post'
            post.update(outcome='published',stats={'views':100,'likes':20,'comments':3,'shares':1},
                        postUrl='https://www.tiktok.com/@user/video/7420000000000000000')
            await app.live_cards.tick()
            assert 99 not in app.live_cards.cards
            method,data=app.tg.calls[-1]
            assert method=='editMessageText'
            rendered=json.dumps(data,ensure_ascii=False)
            assert 'Опубликовано' in rendered and 'Просмотры' in rendered and '100' in rendered
        finally:
            app.db.close()
    asyncio.run(run())


def test_media_plan_refresh_exposes_launch_only_after_checking_and_reply_notice_is_explicit():
    async def run():
        app=new_app()
        app.tg=TelegramTransport(True)
        plan={'id':'plan','state':'checking','postsTotal':1}
        async def provider(method,path,**kwargs):
            assert (method,path)==('GET','/v1/posts/plan/plan')
            return plan
        app.api.call=provider
        try:
            await app.show_plan(99,'plan')
            assert app.live_cards.cards[99].kind=='media_plan'
            plan['state']='ready'
            await app.live_cards.tick()
            assert 99 not in app.live_cards.cards
            assert 'plan_confirm:plan' in json.dumps(app.tg.calls[-1][1])
            card={'task_id':'id','display_name':'Ответы','kind':'keyword_reply','state':'running','snapshot_json':'{}'}
            assert '«0 ответов»' in REPLY_CHECK_NOTE and REPLY_CHECK_NOTE in task_rich(card)
        finally:
            app.db.close()
    asyncio.run(run())
