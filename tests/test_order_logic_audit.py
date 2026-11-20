import asyncio
import json

import pytest

from proxima_bot.catalog import photo_choices
from proxima_bot.order_form import order_fields, parse_volume_message
from test_extended import seed_approved_template, approved_order_data, approve_template, new_app, collected_cid, collected_entry, collected_link, select_order_template
from test_guided_order_form import click, options
from test_order_flows import VIDEO, COMMENT
from test_release_flows import TelegramTransport


@pytest.mark.parametrize('disabled', ['comments', 'replies'])
def test_disabled_template_material_stays_disabled_in_editor_preview_and_payload(disabled):
    async def run():
        app = new_app()
        try:
            values = {'comments': ['Главный'], 'replies': ['Ответ'],
                      'disabled_fields': [disabled], 'options': {
                          'commentsPerVideo': 50, 'repliesPerComment': 3,
                          'likesPerComment': 15, 'likesPerReply': 10}}
            values[disabled] = []
            tid = seed_approved_template(app, 7, 'Один блок', 'publish', {'draft': values})
            await app.edit_local_template(7, 'publish', tid)
            await click(app, 'lt:edit_options:')
            form = str(app.tg.out[-1])
            removed = ('commentsPerVideo', 'likesPerComment') if disabled == 'comments' else ('repliesPerComment', 'likesPerReply')
            assert all(key not in form for key in removed)
            await click(app, 'lt:keep:')
            await click(app, 'lt:save:')
            await app.from_template(7, tid)
            await app.input_draft(7, VIDEO)
            await click(app, 'wiz:quick:')
            await click(app, 'wiz:submit:')
            payload = json.loads(app.db.recent_tasks(7)[0]['payload_json'])
            if disabled == 'comments':
                assert payload['settings']['commentsPerVideo'] == payload['settings']['likesPerComment'] == 0
                assert payload['settings']['likeComments'] is False
                assert payload['settings']['repliesPerVideo'] > 0
            else:
                assert payload['settings']['repliesPerComment'] == payload['settings']['repliesPerVideo'] == payload['settings']['likesPerReply'] == 0
                assert payload['settings']['likeReplies'] is False
        finally:
            app.db.close()
    asyncio.run(run())


@pytest.mark.parametrize('rich', [True, False])
def test_zero_replies_removes_reply_like_controls_and_reply_photo_choices(rich):
    async def run():
        app = new_app()
        try:
            await options(app, 'publish')
            token = app.db.draft(7)['data']['wizard_token']
            await app.callback(7, {'data': 'wiz:pick_repliesPerComment_0:' + token})
            app.tg = TelegramTransport(rich)
            await app.show_wizard(7)
            _, payload = app.tg.calls[-1]
            assert 'likesPerReply' not in str(payload)
            assert 'Лайков на ответный' not in str(payload)
            assert 'Лайки ответам' not in str(payload)
            assert photo_choices('publish', app.db.draft(7)['data']) == ('comments',)
        finally:
            app.db.close()
    asyncio.run(run())


def test_publication_with_replies_only_has_a_real_reply_budget_and_honest_forecast():
    async def run():
        app = new_app()
        try:
            await app.callback(7, {'data': 'new:publish'})
            await select_order_template(app,7,'publish',replies=['Ответ'])
            await app.input_draft(7, VIDEO)
            await click(app, 'wiz:quick:')
            assert 'По 3 ответов на каждый найденный основной комментарий' in app.tg.out[-1][1]
            assert 'Ответов по плану: 0' not in app.tg.out[-1][1]
            await click(app, 'wiz:submit:')
            payload = json.loads(app.db.recent_tasks(7)[0]['payload_json'])
            assert payload['settings']['commentsPerVideo'] == 0
            assert payload['settings']['repliesPerVideo'] == 30000
            assert payload['settings']['repliesPerComment'] == 3
        finally:
            app.db.close()
    asyncio.run(run())


def test_reply_links_reuses_publication_texts_without_main_comment_actions():
    async def run():
        app = new_app()
        try:
            tid = seed_approved_template(app, 7, 'Оба текста', 'publish', {'draft': {
                'comments': ['Главный'], 'replies': ['Ответ'], 'options': {
                    'commentsPerVideo': 50, 'likesPerComment': 15, 'likesPerReply': 10}}})
            await app.callback(7, {'data': 'new:reply_links'})
            await click(app, 'wiz:tpl_' + tid + ':')
            await app.input_draft(7, COMMENT)
            assert all(field.key not in ('commentsPerVideo', 'likesPerComment')
                       for field in order_fields('reply_links', app.db.draft(7)['data']))
            await click(app, 'wiz:quick:')
            await click(app, 'wiz:submit:')
            payload = json.loads(app.db.recent_tasks(7)[0]['payload_json'])
            assert payload['assets']['replyTexts'] == ['Ответ']
            assert 'commentTexts' not in payload['assets']
            assert payload['settings']['commentsPerVideo'] == payload['settings']['likesPerComment'] == 0
            assert payload['settings']['likeComments'] is False
        finally:
            app.db.close()
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['publish', 'reply_links', 'keyword_reply', 'source_reply'])
def test_reply_only_order_rejects_zero_work_even_in_an_old_review(mode):
    async def run():
        app = new_app()
        try:
            data = {'name': 'Без действий', 'comments': [], 'replies': ['Ответ'],
                    'videos': [VIDEO], 'reply_links': [COMMENT], 'keywords': ['Фраза'],
                    'options': {'repliesPerComment': 0}, 'review_account_ids': ['acc7']}
            if mode == 'source_reply':
                source = app.db.queue_task(7, 'Сбор', 'fetch', {'assets': {'accountIds': ['acc7']}})
                app.db.transition(source, 'completed', upstream_id='source')
                data.update(source_task=source, target_count=1, target_links=[collected_link()])
            app.save_wizard_step(7, mode, 'review', approved_order_data(app,7,mode,data))
            await app.submit(7)
            assert not any(task['kind'] in ('publish', 'keyword_reply') for task in app.db.recent_tasks(7))
            assert 'хотя бы' in app.tg.out[-1][1]
            assert app.db.draft(7)['step'] == 'review'
        finally:
            app.db.close()
    asyncio.run(run())


@pytest.mark.parametrize('rich', [True, False])
@pytest.mark.parametrize(('legacy', 'expected'), [('all', 100), (80, 100), (1000, 1000)])
def test_legacy_collection_limit_is_identical_in_template_form_review_and_payload(rich, legacy, expected):
    async def run():
        app = new_app()
        try:
            tid = seed_approved_template(app, 7, 'Старый сбор', 'fetch', {'draft': {
                'fetch_count': legacy, 'fetch_replies': False, 'fetch_keywords': [],
                'options': {'collectAll': True, 'fetchCount': 80}}})
            await app.from_template(7, tid)
            await app.input_draft(7, VIDEO)
            assert app.db.draft(7)['step'] == 'options'
            app.tg = TelegramTransport(rich)
            await app.show_wizard(7)
            _, payload = app.tg.calls[-1]
            body = payload['rich_message']['html'] if rich else payload['text']
            assert 'До ' + f'{expected:,}'.replace(',', ' ') in body
            assert 'Ввод одним сообщением' not in body
            token = app.db.draft(7)['data']['wizard_token']
            await app.callback(7, {'data': 'wiz:quick:' + token})
            _, payload = app.tg.calls[-1]
            body = payload['rich_message']['html'] if rich else payload['text']
            assert 'До ' + f'{expected:,}'.replace(',', ' ') in body
            token = app.db.draft(7)['data']['wizard_token']
            await app.callback(7, {'data': 'wiz:submit:' + token})
            payload = json.loads(app.db.recent_tasks(7)[0]['payload_json'])
            assert payload['settings']['fetchCount'] == expected
            assert payload['settings']['collectAll'] is False
        finally:
            app.db.close()
    asyncio.run(run())


@pytest.mark.parametrize(('mode', 'data', 'text', 'section', 'expected'), [
    ('publish', {'comments': ['Главный']}, 'Комментарии: 25\nЛайки: 10', 'options', {'commentsPerVideo': 25, 'likesPerComment': 10}),
    ('reply_links', {'replies': ['Ответ']}, 'Ответы: 2\nЛайки ответам: 5', 'options', {'repliesPerComment': 2, 'likesPerReply': 5}),
    ('usernames', {}, 'Лимит сбора: 1000', 'options', {'fetchCount': 1000}),
    ('feedwarm', {}, 'Максимум результатов: 1000\nПериод поиска: 30\nМинимум лайков: 5\nМинимум комментариев: 10', 'warm_json',
     {'searchLimit': 1000, 'periodDays': 30, 'minLikes': 5, 'minComments': 10}),
])
def test_bulk_input_instructions_only_offer_fields_of_the_current_order(mode, data, text, section, expected):
    result = parse_volume_message(mode, data, text)
    assert result[section] == expected
    with pytest.raises(ValueError):
        parse_volume_message(mode, data, 'Несуществующий параметр: 10')


def test_bulk_input_can_enable_replies_and_set_likes_in_one_message():
    draft={'comments':['Комментарий'],'replies':['Ответ'],'options':{'repliesPerComment':0}}
    result=parse_volume_message('publish',draft,'Лайки ответам: 5\nОтветы: 2')
    assert result['options']=={'repliesPerComment':2,'likesPerReply':5}
    assert draft['options']=={'repliesPerComment':0}


def test_approved_edit_adding_replies_enables_reply_actions():
    async def run():
        app=new_app()
        try:
            await app.callback(7,{'data':'new:publish'})
            tid=await select_order_template(app,7,'publish',comments=['Главный комментарий'])
            await app.input_draft(7,VIDEO)
            await click(app,'wiz:quick:')
            assert 'wiz:edit_replies:' not in str(app.tg.out[-1][2])
            await app.edit_local_template(7,'publish',tid)
            await click(app,'lt:edit_replies:')
            await app.input_draft(7,'Новый ответ')
            await click(app,'lt:save:')
            await approve_template(app,7,tid)
            await app.from_template(7,tid)
            await app.input_draft(7,VIDEO)
            await click(app,'wiz:quick:')
            assert 'Новый ответ' in app.tg.out[-1][1]
            await click(app,'wiz:submit:')
            task,=app.db.recent_tasks(7)
            settings=json.loads(task['payload_json'])['settings']
            assert settings['repliesPerComment']==3 and settings['likesPerReply']==10
        finally:
            app.db.close()

    asyncio.run(run())


def test_collected_reply_can_return_to_review_or_change_quantity_in_place():
    async def run():
        app=new_app()

        async def provider(method,path,**kwargs):
            if path.endswith('/entries'):
                return {'items':[collected_entry(i) for i in range(4)],'more':False}
            return {}

        app.api.call=provider
        try:
            source=app.db.queue_task(7,'Сбор','fetch',{'assets':{'accountIds':['acc7']}})
            app.db.transition(source,'completed',upstream_id='source')
            await app.callback(7,{'data':'source_reply:'+source})
            await app.input_draft(7,'2')
            await click(app,'source:back:')
            assert app.db.draft(7)['step']=='target_count'
            await app.input_draft(7,'2')
            await select_order_template(app,7,'source_reply',replies=['Ответ'])
            await click(app,'source:skip:')
            await click(app,'source:edit_targets:')
            await click(app,'source:back:')
            assert app.db.draft(7)['data']['target_cids']==[collected_cid(0),collected_cid(1)]
            await click(app,'source:edit_targets:')
            await app.input_draft(7,'1')
            draft=app.db.draft(7)
            assert draft['step']=='review' and draft['data']['target_cids']==[collected_cid(0)]
            assert draft['data']['replies']==['Ответ']
        finally:
            app.db.close()

    asyncio.run(run())
