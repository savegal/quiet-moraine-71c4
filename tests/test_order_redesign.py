import asyncio
import json

import pytest

from proxima_bot.order_form import parse_volume_message
from test_extended import seed_approved_template, approve_template, button_callback, new_app
from test_guided_order_form import options, click
from test_release_flows import TelegramTransport


def test_typical_volume_is_explicit_and_payload_uses_60_100_20():
    async def run():
        app = new_app()
        try:
            await options(app, 'publish')
            assert app.db.draft(7)['data'].get('options', {}) == {}
            # Defaults are shown explicitly and confirmed below.
            await click(app, 'wiz:quick:')
            await click(app, 'wiz:submit:')
            payload = json.loads(app.db.recent_tasks(7)[0]['payload_json'])
            assert payload['settings']['commentsPerVideo'] == 50
            assert payload['settings']['repliesPerVideo'] == 150
            assert payload['settings']['repliesPerComment'] == 3
            assert payload['settings']['likesPerComment'] == 15
            assert payload['settings']['likesPerReply'] == 10
            assert payload['settings']['uniqueText'] is False
        finally:
            app.db.close()
    asyncio.run(run())


def test_bulk_volume_validates_atomically_and_preserves_other_settings():
    source = {'comments': ['text'], 'replies': ['reply'], 'options': {'delaySeconds': 5}}
    result = parse_volume_message('publish', source, 'Комментарии: 60\nОтветы: 3\nЛайки: 20')
    assert result['options'] == {'delaySeconds': 5, 'commentsPerVideo': 60, 'repliesPerComment': 3, 'likesPerComment': 20}
    for text in ('Комментарии: 60\nОтветы: 101', 'Ответы: 3\nОтветы: 2', 'Лайки: -1'):
        with pytest.raises(ValueError):
            parse_volume_message('publish', source, text)
    assert source['options'] == {'delaySeconds': 5}


@pytest.mark.parametrize('save', [False, True])
def test_template_edit_returns_to_order_without_duplicates_or_losing_input(save):
    async def run():
        app = new_app()
        try:
            await options(app, 'publish')
            await click(app, 'wiz:quick:')
            original = app.db.draft(7)
            callback = button_callback(app, 'wiz:template:')
            await app.callback(7, {'data': callback})
            assert app.db.draft(7)['mode'] == 'local_template'
            assert len(app.db.templates(7)) == 1
            if save:
                confirmation = button_callback(app, 'lt:save:')
                await app.callback(7, {'data': confirmation})
                await app.callback(7, {'data': confirmation})
                assert len(app.db.templates(7)) == 2
                assert app.db.draft(7)['data'] == original['data']
                item = app.db.templates(7)[0]
                await approve_template(app,7,item['template_id'])
                body = json.loads(app.db.template(7, item['template_id'])['payload'])['draft']
                assert 'videos' not in body
                assert '_return_draft' not in body
                assert body['comments'] == original['data']['comments']
                await app.from_template(7, item['template_id'])
                await app.input_draft(7, '7420000000000000000')
                assert app.db.draft(7)['step'] == 'options'
            else:
                await click(app, 'lt:cancel:')
                restored = app.db.draft(7)
                assert restored['mode'] == original['mode']
                assert restored['step'] == original['step']
                assert restored['data'] == original['data']
        finally:
            app.db.close()
    asyncio.run(run())


def test_media_template_quantity_can_change_without_writing_to_provider():
    from proxima_bot.operations import load_spec
    from test_api_coverage import sample

    async def run():
        app = new_app()
        calls = []
        settings = sample(load_spec()['components']['schemas']['PostSettings'])
        settings['perAccount'] = 1

        async def provider(method, path, **kwargs):
            calls.append((method, path))
            if path == '/v1/posts/drafts/media':
                return {'id': 'media', 'kind': 'video', 'name': 'Видео'}
            if path == '/v1/posts/settings':
                return settings
            raise AssertionError((method, path))

        app.api.call = provider
        try:
            tid = seed_approved_template(app, 99, 'Видео', 'video_post', {'media': {'description': 'Подпись', 'per_account': 3}})
            await app.media_template_preview(99, tid, 'media')
            await app.callback(99, {'data': button_callback(app, 'mtpl:edit:')})
            assert app.db.draft(99)['mode'] == 'media_order'
            assert app.db.draft(99)['data']['settings']['perAccount'] == 3
            await app.callback(99, {'data': button_callback(app, 'mo:edit_perAccount:')})
            await app.input_draft(99, '5')
            assert app.db.draft(99)['data']['settings']['perAccount'] == 5
            assert app.db.draft(99)['data']['patch']['description'] == 'Подпись'
            assert all(method == 'GET' for method, _ in calls)
        finally:
            app.db.close()
    asyncio.run(run())


def test_menu_uses_auto_name_and_new_template_opens_compact_editor():
    async def run():
        app = new_app()
        try:
            await app.callback(7, {'data': 'new:publish'})
            assert app.db.draft(7)['step'] == 'choose_template'
            assert app.db.draft(7)['data']['name'].startswith('Комментарии и ответы')
            await app.callback(7, {'data': 'tpl_new:publish'})
            await app.input_draft(7, 'Мой шаблон')
            assert app.db.draft(7)['step'] == 'review'
            await click(app, 'lt:edit_replies:')
            await click(app, 'lt:off:')
            assert 'Нет текстов' in app.tg.out[-1][1]
            await click(app, 'lt:cancel:')
            assert app.db.draft(7)['step'] == 'choose_template'
        finally:
            app.db.close()
    asyncio.run(run())


@pytest.mark.parametrize('rich', [False, True])
def test_rich_volume_has_inline_choices_and_fallback_keeps_editors(rich):
    async def run():
        app = new_app()
        try:
            await options(app, 'publish')
            app.tg = TelegramTransport(rich)
            await app.show_wizard(7)
            method, payload = app.tg.calls[-1]
            rows = payload['reply_markup']['inline_keyboard']
            if rich:
                assert method == 'sendRichMessage'
                assert 'pick_commentsPerVideo_50' in payload['rich_message']['html']
                assert len(rows) <= 5
            else:
                assert method == 'sendMessage'
                assert any('form_commentsPerVideo' in button['callback_data'] for row in rows for button in row)
                assert '50' in str(rows)
        finally:
            app.db.close()
    asyncio.run(run())
