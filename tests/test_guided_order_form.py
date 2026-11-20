import asyncio
import json

import pytest

from proxima_bot.operations import load_spec, valid_body
from test_api_coverage import sample
from test_extended import approve_template, button_callback, new_app, collected_cid, select_order_template
from test_order_flows import SCENARIOS, VIDEO, OrderProvider


async def click(app,prefix,uid=7):
    await app.callback(uid,{'data':button_callback(app,prefix)})


async def options(app,mode):
    await app.begin(7,mode)
    inputs,expected=SCENARIOS[mode]
    if mode in ('publish','reply_links','keyword_reply'):
        assets=expected['assets']
        await select_order_template(app,7,mode,comments=assets.get('commentTexts',[]),replies=assets['replyTexts'])
        inputs=inputs[:2 if mode=='keyword_reply' else 1]
    else:
        await app.input_draft(7,'Проверка формы')
    for value in inputs:await app.input_draft(7,value)
    if mode=='fetch':
        await click(app,'wiz:fetch_without_replies:')
        await click(app,'wiz:skip:')
    assert app.db.draft(7)['step']=='options'


async def number(app,key,value,prefix='wiz',uid=7):
    await click(app,prefix+':form_'+key+':',uid)
    await app.input_draft(uid,str(value))


def test_publication_quantities_likes_ai_and_delay_are_sent_exactly_as_chosen():
    async def run():
        app=new_app()
        try:
            await options(app,'publish')
            assert 'Объём и действия' in app.tg.out[-1][1]
            assert 'Комментариев на видео' in str(app.tg.out[-1][2])
            for key,value in [('commentsPerVideo',5),('repliesPerComment',2),('likesPerComment',3),('likesPerReply',4)]:
                await number(app,key,value)
            assert 'uniqueText' not in str(app.tg.out[-1][2])
            await app.callback(7,{'data':'wiz_continue'})
            await click(app,'wiz:quick:')
            assert 'Ответов на каждый комментарий' in app.tg.out[-1][1]
            await click(app,'wiz:submit:')
            task,=app.db.recent_tasks(7)
            payload=json.loads(task['payload_json'])
            assert payload['settings']=={
                'commentsPerVideo':5,'repliesPerComment':2,'repliesPerVideo':10,
                'likesPerComment':3,'likesPerReply':4,'delaySeconds':2,'uniqueText':False,
                'likeEnabled':True,'likeComments':True,'likeReplies':True,
            }
            assert payload['assets']['accountIds']==['acc7']
            assert valid_body(load_spec()['components']['schemas']['TaskStart'],payload)
        finally:app.db.close()
    asyncio.run(run())


@pytest.mark.parametrize(('mode','key','value','section'),[
    ('reply_links','repliesPerComment',3,'settings'),
    ('keyword_reply','fetchCount',35,'settings'),
    ('usernames','fetchCount',25,'settings'),
    ('fetch',None,2,'settings'),
    ('commentlike',None,2,'settings'),
    ('feedwarm','searchLimit',40,'feedWarm'),
])
def test_each_task_type_has_editable_fields_and_a_schema_valid_payload(mode,key,value,section):
    async def run():
        app=new_app()
        app.api=OrderProvider()
        try:
            await options(app,mode)
            if key:await number(app,key,value)
            await click(app,'wiz:quick:')
            await click(app,'wiz:submit:')
            task,=app.db.recent_tasks(7)
            payload=json.loads(task['payload_json'])
            assert payload[section][key or 'delaySeconds']==value
            assert valid_body(load_spec()['components']['schemas']['TaskStart'],payload)
            if mode=='reply_links':assert payload['settings']['commentsPerVideo']==0
            if mode=='commentlike':assert payload['likesPerComment']==3
            if mode=='feedwarm':
                assert payload['feedWarm']['comment'] is False and payload['feedWarm']['like'] is False
            assert not any(method=='POST' and path=='/v1/tasks' for method,path,_ in app.api.calls)
        finally:app.db.close()
    asyncio.run(run())


@pytest.mark.parametrize('bad',['-1','0','101','1.5','abc'])
def test_invalid_publication_quantity_stays_on_the_field(bad):
    async def run():
        app=new_app()
        try:
            await options(app,'publish')
            await number(app,'commentsPerVideo',bad)
            draft=app.db.draft(7)
            assert draft['step']=='options' and draft['data']['form_field']=='commentsPerVideo'
            assert 'Проверь ввод' in app.tg.out[-1][1]
            assert not app.db.recent_tasks(7)
        finally:app.db.close()
    asyncio.run(run())


def test_review_edits_targets_and_quantities_without_changing_approved_texts():
    async def run():
        app=new_app()
        try:
            await options(app,'publish')
            await number(app,'commentsPerVideo',7)
            await click(app,'wiz:quick:')
            assert 'wiz:edit_comments:' not in str(app.tg.out[-1][2])
            for field,value in [('name','Новое название'),('targets','7420000000000000001')]:
                await click(app,'wiz:edit_'+field+':')
                await app.input_draft(7,value)
                assert app.db.draft(7)['step']=='review'
            await click(app,'wiz:edit_options:')
            await number(app,'likesPerComment',0)
            await click(app,'wiz:quick:')
            await click(app,'wiz:submit:')
            payload=json.loads(app.db.recent_tasks(7)[0]['payload_json'])
            assert payload['settings']['commentsPerVideo']==7 and payload['settings']['likeEnabled'] is True
            assert payload['awemeIds']==['7420000000000000001']
            assert payload['assets']['commentTexts']==['Текст комментария']
        finally:app.db.close()
    asyncio.run(run())


def test_template_quantities_can_be_configured_without_json_and_reused():
    async def run():
        app=new_app()
        try:
            await app.edit_local_template(7,'publish')
            await app.input_draft(7,'Шаблон объёма')
            await app.input_draft(7,'Комментарий')
            await app.input_draft(7,'Ответ')
            await number(app,'commentsPerVideo',6,prefix='lt')
            await number(app,'repliesPerComment',2,prefix='lt')
            await click(app,'lt:keep:')
            await click(app,'lt:save:')
            template,=app.db.templates(7)
            await approve_template(app,7,template['template_id'])
            assert not app.api.calls
            await app.from_template(7,template['template_id'])
            await app.input_draft(7,VIDEO)
            assert app.db.draft(7)['step']=='options'
            await click(app,'wiz:quick:')
            await click(app,'wiz:submit:')
            payload=json.loads(app.db.recent_tasks(7)[0]['payload_json'])
            assert payload['settings']['commentsPerVideo']==6
            assert payload['settings']['repliesPerComment']==2
        finally:app.db.close()
    asyncio.run(run())


def test_source_replies_offer_quantities_and_likes_without_json():
    async def run():
        app=new_app()
        try:
            source=app.db.queue_task(7,'Сбор','fetch',{'assets':{'accountIds':['acc7']}})
            app.db.transition(source,'completed',upstream_id='remote-source')
            await app.callback(7,{'data':'source_reply:'+source})
            await app.input_draft(7,'100')
            await select_order_template(app,7,'source_reply',replies=['Ответ'])
            await number(app,'repliesPerComment',3,prefix='source')
            await number(app,'likesPerReply',2,prefix='source')
            await click(app,'source:skip:')
            await click(app,'source:submit:')
            payload=json.loads(app.db.recent_tasks(7)[0]['payload_json'])
            assert payload['sourceJob']=='remote-source' and payload['targetIds']==[collected_cid()]
            assert payload['settings']['repliesPerComment']==3
            assert payload['settings']['likesPerReply']==2 and payload['settings']['likeReplies'] is True
            assert payload['settings']['commentsPerVideo']==0
        finally:app.db.close()
    asyncio.run(run())


def test_selecting_main_comment_template_disables_reply_actions_and_their_likes():
    async def run():
        app=new_app()
        try:
            await options(app,'publish')
            await number(app,'likesPerReply',4)
            await click(app,'wiz:quick:')
            await click(app,'wiz:templates:')
            await select_order_template(app,7,'publish',comments=['Текст комментария'])
            await click(app,'wiz:quick:')
            await click(app,'wiz:submit:')
            payload=json.loads(app.db.recent_tasks(7)[0]['payload_json'])
            assert payload['settings']['repliesPerVideo']==0
            assert payload['settings']['likesPerReply']==0
            assert payload['settings']['likeEnabled'] is True and payload['settings']['likeReplies'] is False
        finally:app.db.close()
    asyncio.run(run())


def test_template_can_ask_for_quantities_again_at_every_launch():
    async def run():
        app=new_app()
        try:
            await app.edit_local_template(7,'publish')
            for value in ('Шаблон','Комментарий','Ответ'):await app.input_draft(7,value)
            await click(app,'lt:skip:')
            await click(app,'lt:save:')
            template,=app.db.templates(7)
            await approve_template(app,7,template['template_id'])
            await app.from_template(7,template['template_id'])
            await app.input_draft(7,VIDEO)
            assert app.db.draft(7)['step']=='options'
            await number(app,'commentsPerVideo',8)
            await click(app,'wiz:quick:')
            await click(app,'wiz:submit:')
            assert json.loads(app.db.recent_tasks(7)[0]['payload_json'])['settings']['commentsPerVideo']==8
        finally:app.db.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind',['video','photo'])
def test_media_form_prepares_plan_with_chosen_quantity_accounts_and_caption(kind):
    async def run():
        app=new_app()
        calls=[]
        settings={**sample(load_spec()['components']['schemas']['PostSettings']),
                  'description':'Старая подпись','hashtags':'#old','comments':False}
        async def provider(method,path,**kwargs):
            calls.append((method,path,kwargs))
            if path=='/v1/posts/drafts/media':return {'id':'media','kind':kind,'photos':[{'id':'photo'}]}
            if path=='/v1/posts/settings':return settings
            if path=='/v1/posts/plan':return {'id':'plan','state':'checking','postsTotal':2}
            raise AssertionError((method,path))
        app.api.call=provider
        try:
            await app.callback(7,{'data':'draft_plan:media'})
            assert not calls
            await app.callback(99,{'data':'draft_plan:media'})
            assert not any(method!='GET' for method,_,_ in calls)
            await click(app,'mo:edit_perAccount:',99)
            await app.input_draft(99,'2')
            await click(app,'mo:edit_accountIds:',99)
            await app.input_draft(99,'my-account')
            await click(app,'mo:edit_description:',99)
            await app.input_draft(99,'Новая подпись')
            await click(app,'mo:toggle_comments:',99)
            await click(app,'mo:prepare:',99)
            submitted=next(kwargs['payload'] for method,path,kwargs in calls if path=='/v1/posts/plan')
            assert submitted['settings']['perAccount']==2
            assert submitted['settings']['accountIds']==['my-account'] and submitted['settings']['allAccounts'] is False
            assert submitted['settings']['comments'] is True
            assert submitted['settings']['description']=='Новая подпись'
            patch=next(kwargs['payload'] for method,path,kwargs in calls if method=='PATCH')
            assert patch['description']=='Новая подпись' and patch['hashtags']=='#old'
            assert patch['comments'] is True and patch['ownSettings'] is True and patch['ownText'] is True
            assert not any(path.endswith('/launch') for _,path,_ in calls)
            assert app.db.draft(99) is None
        finally:app.db.close()
    asyncio.run(run())
