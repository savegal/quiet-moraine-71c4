import asyncio
import json

import pytest

from proxima_bot.api import ApiError
from proxima_bot.operations import load_spec
from test_api_coverage import sample
from test_extended import seed_approved_template, button_callback, new_app


@pytest.mark.parametrize(('changes', 'expected'), [
    ({'description': 'Подпись'}, {'description': 'Подпись', 'ownText': True}),
    ({'hashtags': '#test'}, {'hashtags': '#test', 'ownText': True}),
    ({'comments': False}, {'comments': False, 'ownSettings': True}),
    ({'visibility': 2}, {'visibility': 2, 'ownSettings': True}),
    ({'description': '', 'ownText': False}, {'description': '', 'ownText': False}),
    ({'comments': False, 'ownSettings': False}, {'comments': False, 'ownSettings': False}),
])
def test_direct_draft_edit_activates_overrides_and_preserves_explicit_inheritance(changes, expected):
    async def run():
        app = new_app()
        writes = []

        from proxima_bot.operations import load_spec
        from test_api_coverage import sample
        settings=sample(load_spec()['components']['schemas']['PostSettings'])
        async def provider(method, path, **kwargs):
            if method=='GET' and path=='/v1/posts/drafts/media':return {'id':'media','kind':'video','name':'Видео'}
            if method=='GET' and path=='/v1/posts/settings':return settings
            writes.append((method,path,kwargs))
            return {'id':'media'}

        app.api.call = provider
        try:
            await app.owner_edit(99, 'draft_media')
            await app.input_draft(99, json.dumps(changes))
            assert writes == []
            assert app.db.draft(99)['mode']=='media_order'
            assert app.db.draft(99)['data']['settings']==settings
        finally:
            app.db.close()

    asyncio.run(run())


def test_album_upload_error_preserves_destination_and_success_accepts_next_photo():
    async def run():
        app = new_app()
        uploaded = []

        async def fetch_upload(file_id, limit):
            return b'\x89PNG\r\n\x1a\n' + b'photo', 'photo.png'

        async def upload(path, data, content_type, **kwargs):
            assert path == '/v1/posts/drafts/album/photos'
            uploaded.append(path)
            if len(uploaded) == 1:
                raise ApiError('draft_in_queue', 400)
            return {'id': 'album', 'photos': [{'id': 'photo'}]}

        app.tg.fetch_upload = fetch_upload
        app.api.upload = upload
        try:
            await app.upload_intro(99, 'album_album')
            await app.handle_upload(99, {'photo': [{'file_id': 'first'}]})
            assert 'Дождись завершения' in app.tg.out[-1][1]
            assert app.db.draft(99)['step'] == 'album_album'
            await app.handle_upload(99, {'photo': [{'file_id': 'second'}]})
            assert len(uploaded) == 2
            assert 'Фото добавлено' in app.tg.out[-1][1]
            assert app.db.draft(99)['step'] == 'album_album'
        finally:
            app.db.close()

    asyncio.run(run())


@pytest.mark.parametrize('route', ['plain','template_apply','template_edit'])
def test_plan_preserves_effective_caption_and_nullable_settings(route):
    async def run():
        app=new_app()
        settings={**sample(load_spec()['components']['schemas']['PostSettings']),
                  'description':'Подпись кабинета','hashtags':'#global','visibility':2,'comments':True}
        draft={'id':'media','kind':'video','name':'Видео','ownText':True,'ownSettings':True,
               'description':'Подпись черновика','hashtags':'#draft','visibility':None,'comments':False}
        plans=[]
        patches=[]

        async def provider(method,path,**kwargs):
            if (method,path)==('GET','/v1/posts/settings'):return settings
            if (method,path)==('GET','/v1/posts/drafts/media'):return draft
            if (method,path)==('PATCH','/v1/posts/drafts/media'):
                patches.append(kwargs['payload'])
                return draft
            assert (method,path)==('POST','/v1/posts/plan')
            plans.append(kwargs['payload']['settings'])
            return {'id':'plan','state':'checking','postsTotal':1}

        app.api.call=provider
        try:
            description='Подпись черновика'
            if route=='plain':
                await app.begin_media_order(99,'media')
            else:
                description='Подпись шаблона'
                tid=seed_approved_template(app, 99,'Видео','video_post',{'media':{'description':description}})
                await app.media_template_preview(99,tid,'media')
                prefix='mtpl:apply:' if route=='template_apply' else 'mtpl:edit:'
                await app.callback(99,{'data':button_callback(app,prefix)})
            if route!='template_apply':
                await app.callback(99,{'data':button_callback(app,'mo:prepare:')})
            plan,=plans
            assert {key:plan[key] for key in ('description','hashtags','visibility','comments')}=={
                'description':description,'hashtags':'#draft','visibility':2,'comments':False,
            }
            assert patches==([] if route=='plain' else [
                {'description':description,'hashtags':'#draft','ownText':True},
            ])
        finally:
            app.db.close()
    asyncio.run(run())


def test_media_template_rechecks_album_before_writing_and_keeps_form_on_failure():
    async def run():
        app=new_app()
        writes=[]
        settings=sample(load_spec()['components']['schemas']['PostSettings'])
        draft={'id':'media','kind':'photo','name':'Альбом','photos':[{'id':'photo'}]}

        async def provider(method,path,**kwargs):
            if (method,path)==('GET','/v1/posts/settings'):return settings
            if (method,path)==('GET','/v1/posts/drafts/media'):return draft
            writes.append((method,path))
            return {}

        app.api.call=provider
        try:
            tid=seed_approved_template(app, 99,'Фото','photo_post',{'media':{'description':'Новая подпись'}})
            await app.media_template_preview(99,tid,'media')
            confirm=button_callback(app,'mtpl:apply:')
            draft['photos']=[]
            await app.callback(99,{'data':confirm})
            assert writes==[]
            assert app.db.draft(99)['mode']=='media_order'
            assert app.db.draft(99)['data']['settings']['description']=='Новая подпись'
            assert 'Добавь фотографии' in app.tg.out[-1][1]
        finally:
            app.db.close()
    asyncio.run(run())


@pytest.mark.parametrize('wrong_at_initial_confirmation',[False,True])
def test_ready_response_for_another_media_plan_cannot_authorize_launch(wrong_at_initial_confirmation):
    async def run():
        app=new_app()
        writes=[]
        plan={'id':'other' if wrong_at_initial_confirmation else 'expected','state':'ready','postsTotal':1}

        async def provider(method,path,**kwargs):
            if method=='GET':return plan
            writes.append((method,path))
            return {'planId':'expected','queued':1}

        app.api.call=provider
        try:
            await app.callback(99,{'data':'plan_confirm:expected'})
            if wrong_at_initial_confirmation:
                assert app.db.draft(99) is None
            else:
                confirm=button_callback(app,'owner_do:plan_launch:')
                plan['id']='other'
                await app.callback(99,{'data':confirm})
            assert writes==[]
            assert 'Не удалось подтвердить ID плана' in app.tg.out[-1][1]
        finally:
            app.db.close()
    asyncio.run(run())
