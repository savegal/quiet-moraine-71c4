import asyncio
import json

import pytest

from proxima_bot.order_templates import MEDIA_TEMPLATE_MODES, TEMPLATE_LABELS, template_fields
from proxima_bot.operations import load_spec
from test_api_coverage import sample
from test_extended import seed_approved_template, approve_template, button_callback, new_app, collected_cid
from test_order_flows import OrderProvider, VIDEO, COMMENT


async def click(app,uid,prefix):
    await app.callback(uid,{'data':button_callback(app,prefix)})


async def create_template(app,uid,mode):
    await app.templates(uid)
    await click(app,uid,'tpl_new')
    await app.edit_local_template(uid,mode)
    inputs={
        'name':'Личный '+mode, 'comments':'Комментарий', 'replies':'Ответ',
        'keywords':'ключевая фраза','likes':'3','fetch_count':'100',
        'warm_json':'обзор камеры', 'options':'{}', 'description':'Подпись',
        'hashtags':'#test', 'per_account':'1', 'media_options':'{"comments":false}',
    }
    for field in template_fields(mode):
        assert app.db.draft(uid)['step']==field
        if field in ('videos','like_links','reply_links'):
            await click(app,uid,'lt:skip:')
        elif field=='options':
            await click(app,uid,'lt:keep:')
        elif field=='fetch_keywords':
            await click(app,uid,'lt:skip:')
        elif field=='fetch_replies':
            await click(app,uid,'lt:replies_no:')
        else:
            await app.input_draft(uid,inputs[field])
    assert app.db.draft(uid)['step']=='review'
    confirm=button_callback(app,'lt:save:')
    await app.callback(uid,{'data':confirm})
    await app.callback(uid,{'data':confirm})
    item,=app.db.templates(uid)
    return await approve_template(app,uid,item['template_id'])


@pytest.mark.parametrize('mode',list(TEMPLATE_LABELS))
def test_personal_template_can_be_created_offline_without_accounts_or_order(mode):
    async def run():
        app=new_app()
        uid=99 if mode in MEDIA_TEMPLATE_MODES else 7
        app.db.unassign(7,'acc7')

        async def forbidden(*args,**kwargs):
            raise AssertionError('Saving a local template must not use the provider')

        app.api.call=forbidden
        app.api.account_page=forbidden
        try:
            item=await create_template(app,uid,mode)
            assert item['mode']==mode
            assert app.db.draft(uid) is None and not app.db.recent_tasks(uid)
            payload=json.loads(item['payload'])
            assert all(key not in json.dumps(payload) for key in ('requestId','accountIds','review_account_ids','wizard_token'))
            await app.callback(uid,{'data':'tpl:'+item['template_id']})
            assert 'Перед запуском проверишь объём и тексты заказа.' in app.tg.out[-1][1]
            assert app.db.template(8,item['template_id']) is None
        finally:
            app.db.close()

    asyncio.run(run())


@pytest.mark.parametrize('mode',['publish','reply_links','fetch','commentlike','keyword_reply','feedwarm','usernames'])
def test_personal_template_asks_for_missing_targets_and_requires_order_confirmation(mode):
    async def run():
        app=new_app()
        try:
            item=await create_template(app,7,mode)
            app.api=OrderProvider()
            await app.callback(7,{'data':'tpl_use:'+item['template_id']})
            if mode!='feedwarm':
                assert app.db.draft(7)['step'] in ('videos','like_links','reply_links')
                await app.input_draft(7,COMMENT if mode in ('reply_links','commentlike') else VIDEO)
            if mode=='fetch':await click(app,7,'wiz:skip:')
            assert app.db.draft(7)['step']=='options'
            await click(app,7,'wiz:quick:')
            assert not app.db.recent_tasks(7)
            await click(app,7,'wiz:submit:')
            task,=app.db.recent_tasks(7)
            body=json.loads(task['payload_json'])
            assert body['assets']['accountIds']==['acc7']
            if mode=='fetch':
                assert body['settings']=={'fetchCount':100,'collectAll':False,'includeReplies':False,'delaySeconds':2,'uniqueText':False,'fetchKeywords':[]}
            if mode=='commentlike':assert body['likesPerComment']==3
            if mode in ('publish','reply_links','keyword_reply'):assert body['assets']['replyTexts']==['Ответ']
        finally:
            app.db.close()

    asyncio.run(run())


def test_template_edit_updates_existing_record_and_rename_preserves_materials():
    async def run():
        app=new_app()
        try:
            item=await create_template(app,7,'fetch')
            tid=item['template_id']
            await app.callback(7,{'data':'tpl_edit:'+tid})
            await click(app,7,'lt:edit_fetch_count:')
            await app.input_draft(7,'1000')
            await click(app,7,'lt:save:')
            assert len(app.db.templates(7))==1
            await approve_template(app,7,tid)
            assert json.loads(app.db.template(7,tid)['payload'])['draft']['fetch_count']==1000

            await app.callback(7,{'data':'tpl_rename:'+tid})
            await app.input_draft(7,'Мой сбор')
            updated=await approve_template(app,7,tid)
            assert updated['name']=='Мой сбор'
            assert json.loads(updated['payload'])['draft']['fetch_count']==1000

            await app.callback(7,{'data':'tpl_edit:'+tid})
            await click(app,7,'lt:edit_fetch_count:')
            await click(app,7,'lt:skip:')
            await click(app,7,'lt:save:')
            body=json.loads((await approve_template(app,7,tid))['payload'])['draft']
            assert 'fetch_count' not in body and 'fetchCount' not in body['options']
            await app.callback(8,{'data':'tpl_edit:'+tid})
            assert app.db.draft(8) is None
        finally:
            app.db.close()

    asyncio.run(run())


def test_approved_reply_only_template_skips_main_comment_input():
    async def run():
        app=new_app()
        try:
            item=await create_template(app,7,'publish')
            await app.callback(7,{'data':'tpl_edit:'+item['template_id']})
            await click(app,7,'lt:edit_comments:')
            await click(app,7,'lt:off:')
            await click(app,7,'lt:save:')
            await approve_template(app,7,item['template_id'])
            await app.from_template(7,item['template_id'])
            await app.input_draft(7,VIDEO)
            assert app.db.draft(7)['step']=='options'
            await click(app,7,'wiz:quick:')
            assert app.db.draft(7)['step']=='review'
            await click(app,7,'wiz:submit:')
            task,=app.db.recent_tasks(7)
            body=json.loads(task['payload_json'])
            assert body['assets']['commentTexts']==[]
            assert body['assets']['replyTexts']==['Ответ']
        finally:
            app.db.close()

    asyncio.run(run())


def test_edit_at_template_limit_and_source_reply_template_use_do_not_create_duplicates():
    async def run():
        app=new_app()
        try:
            item=await create_template(app,7,'source_reply')
            for index in range(49):
                seed_approved_template(app, 7,f'Шаблон {index}','fetch',{})
            await app.callback(7,{'data':'tpl_edit:'+item['template_id']})
            await click(app,7,'lt:edit_replies:')
            await app.input_draft(7,'Обновлённый ответ')
            await click(app,7,'lt:save:')
            assert len(app.db.templates(7))==50
            await approve_template(app,7,item['template_id'])
            source=app.db.queue_task(7,'Сбор','fetch',{'assets':{'accountIds':['acc7']}})
            app.db.transition(source,'completed',upstream_id='collected')
            await app.from_template(7,item['template_id'])
            await click(app,7,'source_tpl_use:')
            await app.input_draft(7,'100')
            await click(app,7,'source:skip:')
            await click(app,7,'source:submit:')
            tasks=app.db.recent_tasks(7)
            assert len(tasks)==2
            body=json.loads(tasks[0]['payload_json'])
            assert body['sourceJob']=='collected' and body['targetIds']==[collected_cid()]
            assert body['assets']['replyTexts']==['Обновлённый ответ']
        finally:
            app.db.close()

    asyncio.run(run())


def test_editing_legacy_reply_link_template_preserves_order_type():
    async def run():
        app=new_app()
        try:
            tid=seed_approved_template(app, 7,'Ответы','publish',{'job':{
                'kind':'publish','replyLinks':[COMMENT],'assets':{'replyTexts':['Ответ']},
            }})
            await app.callback(7,{'data':'tpl_edit:'+tid})
            assert app.db.draft(7)['data']['mode']=='reply_links'
            await click(app,7,'lt:save:')
            await approve_template(app,7,tid)
            assert app.db.template(7,tid)['mode']=='reply_links'
            await app.from_template(7,tid)
            await app.input_draft(7,COMMENT)
            await click(app,7,'wiz:quick:')
            await click(app,7,'wiz:submit:')
            task,=app.db.recent_tasks(7)
            assert json.loads(task['payload_json'])['replyLinks']
        finally:
            app.db.close()
    asyncio.run(run())


def test_template_quantity_conflicts_are_rejected_and_back_cancels_rename():
    async def run():
        app=new_app()
        try:
            item=await create_template(app,7,'fetch')
            tid=item['template_id']
            await app.callback(7,{'data':'tpl_edit:'+tid})
            await click(app,7,'lt:edit_options:')
            await app.input_draft(7,'{"fetchCount":80}')
            assert app.db.draft(7)['step']=='options'
            assert 'отключён' in app.tg.out[-1][1]
            await click(app,7,'lt:cancel:')
            await app.callback(7,{'data':'tpl_rename:'+tid})
            await click(app,7,'tpl:'+tid)
            assert app.db.draft(7) is None
        finally:
            app.db.close()
    asyncio.run(run())


def test_media_template_checks_hashtag_limit_while_editing():
    async def run():
        app=new_app()
        try:
            await app.edit_local_template(99,'video_post',compact=True)
            await app.input_draft(99,'Подпись видео')
            await click(app,99,'lt:edit_hashtags:')
            await app.input_draft(99,'#'+'x'*500)
            assert app.db.draft(99)['step']=='hashtags'
            assert '500 символов' in app.tg.out[-1][1]
            await app.input_draft(99,'#'+'x'*499)
            await click(app,99,'lt:save:')
            item,=app.db.templates(99)
            stored=app.db.template(99,item['template_id'])
            assert len(json.loads(stored['payload'])['media']['hashtags'])==500
        finally:
            app.db.close()

    asyncio.run(run())


@pytest.mark.parametrize('mode',['video_post','photo_post'])
def test_media_template_previews_before_patching_selected_draft_and_preparing_plan(mode):
    async def run():
        app=new_app()
        try:
            item=await create_template(app,99,mode)
            kind='video' if mode=='video_post' else 'photo'
            draft={'id':'media','name':'Медиа','kind':kind,'photos':[{'id':'photo'}]}
            settings=sample(load_spec()['components']['schemas']['PostSettings'])
            calls=[]

            async def provider(method,path,**kwargs):
                calls.append((method,path,kwargs.get('payload')))
                if path=='/v1/posts/drafts':return {'items':[draft]}
                if path=='/v1/posts/settings':return settings
                if path=='/v1/posts/drafts/media':
                    if method=='PATCH':
                        assert kwargs['payload']=={
                            'description':'Подпись','hashtags':'#test','ownText':True,
                        }
                    return draft
                if path=='/v1/posts/plan':
                    assert kwargs['payload']['draftIds']==['media']
                    assert kwargs['payload']['settings']['perAccount']==1
                    assert kwargs['payload']['settings']['comments']==settings['comments']
                    return {'id':'plan','state':'checking','postsTotal':1}
                raise AssertionError(path)

            app.api.call=provider
            await app.callback(99,{'data':'tpl_use:'+item['template_id']})
            await click(app,99,'mtpl:pick:')
            assert all(method=='GET' for method,_,_ in calls)
            confirmation=button_callback(app,'mtpl:apply:')
            await app.callback(99,{'data':confirmation})
            await app.callback(99,{'data':confirmation})
            assert [path for method,path,_ in calls if method!='GET']==['/v1/posts/drafts/media','/v1/posts/plan']
            assert app.db.templates(99)[0]['mode']==mode
        finally:
            app.db.close()

    asyncio.run(run())
