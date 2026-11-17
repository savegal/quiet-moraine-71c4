import asyncio
import json

import pytest

from proxima_bot.order_form import complete_order_form, edit_order_form, order_fields
from test_extended import new_app, collected_entry, collected_cid
from test_guided_order_form import click
from test_release_flows import TelegramTransport


@pytest.mark.parametrize('mode', ['source_reply', 'reply_links', 'keyword_reply', 'publish'])
@pytest.mark.parametrize('likes', [0, 10])
def test_reply_only_forms_disable_original_comment_likes_from_legacy_templates(mode, likes):
    data = {'comments': [], 'replies': ['Ответ'], 'options': {
        'likesPerComment': 20, 'likeComments': True, 'likeEnabled': True,
        'likesPerReply': likes, 'targetCount': 500,
    }, 'target_count': 2}
    keys = {field.key for field in order_fields(mode, data)}
    assert 'likesPerComment' not in keys and 'targetCount' not in keys
    assert edit_order_form(mode, data, 'pick_likesPerComment_20') is None
    assert edit_order_form(mode, data, 'pick_targetCount_1000') is None
    result = complete_order_form(mode, data)
    assert result['options']['likesPerComment'] == 0
    assert result['options']['likeComments'] is False
    assert result['options']['likeEnabled'] is bool(likes)
    assert 'targetCount' not in result['options']
    assert result['target_count'] == 2


def test_legacy_source_count_is_migrated_only_when_no_selected_count_exists():
    result = complete_order_form('source_reply', {'replies': ['Ответ'], 'options': {'targetCount': 8}})
    assert result['target_count'] == 8
    assert 'targetCount' not in result['options']


@pytest.mark.parametrize('rich', [True, False])
def test_source_form_has_only_reply_controls_in_rich_and_basic_telegram(rich):
    async def run():
        app = new_app()
        try:
            app.tg = TelegramTransport(rich)
            await app.order_form_field(7, 'source_reply', {'replies': ['Ответ'], 'target_count': 2}, 'token', prefix='source')
            method, payload = app.tg.calls[-1]
            assert method == ('sendRichMessage' if rich else 'sendMessage')
            body = payload['rich_message']['html'] if rich else payload['text']
            assert 'Выбрано комментариев для ответов' in body
            assert 'Ответов на каждый комментарий' in body
            assert 'Лайков на ответный комментарий' in body
            assert 'Лайков на главный' not in body
            assert 'На сколько собранных' not in body
            assert 'targetCount' not in str(payload)
            assert 'likesPerComment' not in str(payload)
            assert 'form_more' not in str(payload)
        finally:
            app.db.close()
    asyncio.run(run())


def test_collected_replies_keep_count_once_and_sanitize_legacy_draft_at_submission():
    async def run():
        app = new_app()
        async def provider(method, path, **kwargs):
            if path.endswith('/entries'):
                return {'items': [collected_entry(i) for i in range(5)], 'more': False}
            return {}
        app.api.call = provider
        try:
            source = app.db.queue_task(7, 'Сбор', 'fetch', {'assets': {'accountIds': ['acc7']}})
            app.db.transition(source, 'completed', upstream_id='source')
            await app.callback(7, {'data': 'source_reply:' + source})
            await app.input_draft(7, '2')
            await app.input_draft(7, 'Ответ')
            draft = app.db.draft(7)
            assert 'targetCount' not in draft['data']['options']
            # Old inline buttons cannot add the removed settings back.
            for verb in ('pick_targetCount_1000', 'pick_likesPerComment_20'):
                await app.callback(7, {'data': 'source:' + verb + ':' + draft['data']['wizard_token']})
            # JSON is rejected, including a hidden target-count override.
            await app.input_draft(7, '{"targetCount":500,"likesPerComment":20}')
            assert app.db.draft(7)['step'] == 'options'
            assert app.db.draft(7)['data']['options'] == {}
            draft = app.db.draft(7)
            draft['data']['options'] = {'targetCount': 500, 'likesPerComment': 20, 'likeComments': True}
            app.db.save_draft(7, 'source_reply', 'options', draft['data'])
            await click(app, 'source:skip:')
            draft = app.db.draft(7)
            assert draft['data']['target_cids'] == [collected_cid(0),collected_cid(1)]
            # Also cover a review saved before the fix, submitted without re-opening it.
            draft['data']['options'].update(likesPerComment=20, likeComments=True)
            app.db.save_draft(7, 'source_reply', 'review', draft['data'])
            await click(app, 'source:submit:')
            task = next(task for task in app.db.recent_tasks(7) if task['kind'] == 'publish')
            payload = json.loads(task['payload_json'])
            assert payload['targetIds'] == [collected_cid(0),collected_cid(1)]
            assert payload['settings']['likesPerComment'] == 0
            assert payload['settings']['likeComments'] is False
            assert payload['settings']['likesPerReply'] == 10
            assert 'targetCount' not in payload['settings']
        finally:
            app.db.close()
    asyncio.run(run())
