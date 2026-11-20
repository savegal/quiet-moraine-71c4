import asyncio

import pytest

from proxima_bot.order_templates import template_input_steps
from test_edit_in_place import Recorder
from test_extended import approve_template, button_callback, new_app, seed_approved_template, select_order_template
from test_guided_order_form import click, number, options
from test_order_flows import SCENARIOS


class CallbackRecorder(Recorder):
    async def call(self, method, data, *, timeout=25):
        if method == 'answerCallbackQuery':
            self.calls.append((method, data))
            return True
        return await super().call(method, data, timeout=timeout)


@pytest.mark.parametrize('mode', list(SCENARIOS))
def test_real_callback_renders_first_material_once_without_name_prompt(mode):
    async def run():
        app = new_app()
        app.tg = CallbackRecorder()
        try:
            await app.handle({'callback_query': {
                'id': 'start', 'from': {'id': 7}, 'data': 'new:' + mode,
                'message': {'message_id': 901, 'chat': {'id': 7, 'type': 'private'}},
            }})
            renders = [(method, body) for method, body in app.tg.calls
                       if method in ('editMessageText', 'sendMessage', 'sendRichMessage')]
            assert len(renders) == 1
            assert renders[0][0] == 'editMessageText'
            assert renders[0][1]['message_id'] == 901
            body=renders[0][1]
            text=body.get('text') or body['rich_message']['html']
            assert 'Введи короткое название' not in text
            assert 'Название · Название' not in text
            assert app.db.draft(7)['step'] not in ('name', 'review')
            back = next(button['callback_data']
                        for row in renders[0][1]['reply_markup']['inline_keyboard']
                        for button in row if button['callback_data'].startswith('wiz:back:') or button['callback_data']=='create')
            await app.handle({'callback_query': {
                'id': 'back', 'from': {'id': 7}, 'data': back,
                'message': {'message_id': 901, 'chat': {'id': 7, 'type': 'private'}},
            }})
            body=app.tg.calls[-1][1]
            text=body.get('text') or body['rich_message']['html']
            assert 'Новый заказ' in text
            assert 'Введи короткое название' not in text
        finally:
            app.db.close()
    asyncio.run(run())


def test_manual_name_prompt_does_not_duplicate_caption():
    async def run():
        app = new_app()
        try:
            await app.begin(7, 'publish')
            assert 'Название · Название' not in app.tg.out[-1][1]
        finally:
            app.db.close()
    asyncio.run(run())


def test_fetch_volume_summary_preserves_collection_limit_after_delay_edit():
    async def run():
        app = new_app()
        try:
            await options(app, 'fetch')
            assert 'delaySeconds' not in str(app.tg.out[-1][2])
            assert 'До 100' in app.tg.out[-1][1]
        finally:
            app.db.close()
    asyncio.run(run())


def test_template_accepts_advertised_named_volume_message():
    async def run():
        app = new_app()
        try:
            await app.edit_local_template(7, 'publish', compact=True)
            await app.input_draft(7, 'Объём')
            await click(app, 'lt:edit_options:')
            await app.input_draft(7, 'Комментарии: 60\nОтветы: 3\nЛайки: 20')
            draft = app.db.draft(7)
            assert draft['step'] == 'options'
            assert draft['data']['values']['options']['commentsPerVideo'] == 60
            assert draft['data']['values']['options']['repliesPerComment'] == 3
        finally:
            app.db.close()
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['reply_links', 'keyword_reply', 'source_reply'])
def test_required_reply_material_cannot_be_disabled(mode):
    async def run():
        app = new_app()
        try:
            await app.edit_local_template(7, mode, compact=True)
            await app.input_draft(7, 'Ответы')
            await click(app, 'lt:edit_replies:')
            markup = app.tg.out[-1][2]
            assert not any(button['callback_data'].startswith('lt:off:')
                           for row in markup['inline_keyboard'] for button in row)
        finally:
            app.db.close()
    asyncio.run(run())


def test_template_rejects_disabling_both_publication_materials():
    async def run():
        app = new_app()
        try:
            await app.edit_local_template(7, 'publish', compact=True)
            await app.input_draft(7, 'Пустой')
            for field in ('comments', 'replies'):
                await click(app, 'lt:edit_' + field + ':')
                await click(app, 'lt:off:')
            await click(app, 'lt:save:')
            assert app.db.templates(7) == []
            assert app.db.draft(7)['step'] == 'review'
            assert 'Добавь хотя бы один текст' in app.tg.out[-1][1]
        finally:
            app.db.close()
    asyncio.run(run())


def test_explicit_fetch_buttons_clear_deferred_template_rules():
    async def run():
        app = new_app()
        try:
            await app.edit_local_template(7, 'fetch', compact=True)
            await app.input_draft(7, 'Сбор')
            for field, choice in (('fetch_count', 'fetch1000'), ('fetch_replies', 'replies_yes')):
                await click(app, 'lt:edit_' + field + ':')
                await click(app, 'lt:skip:')
                await click(app, 'lt:edit_' + field + ':')
                await click(app, 'lt:' + choice + ':')
            values = app.db.draft(7)['data']['values']
            assert values['fetch_count'] == 1000 and values['fetch_replies'] is True
            assert 'fetch_count' not in template_input_steps('fetch', values)
            assert 'fetch_replies' not in template_input_steps('fetch', values)
        finally:
            app.db.close()
    asyncio.run(run())


def test_source_named_volume_input_does_not_skip_confirmation():
    async def run():
        app = new_app()
        try:
            tid = app.db.queue_task(7, 'Сбор', 'fetch', {'assets': {'accountIds': ['acc7']}})
            app.db.transition(tid, 'completed', upstream_id='source')
            await app.callback(7, {'data': 'source_reply:' + tid})
            await app.input_draft(7,'100')
            await select_order_template(app,7,'source_reply',replies=['Текст ответа'])
            await app.input_draft(7, 'Ответы: 3\nЛайки ответам: 20')
            assert app.db.draft(7)['step'] == 'options'
            assert app.db.draft(7)['data']['options']['repliesPerComment'] == 3
        finally:
            app.db.close()
    asyncio.run(run())


def test_reenabling_material_in_template_requires_approval():
    async def run():
        app = new_app()
        try:
            tid = seed_approved_template(app, 7, 'Только ответы', 'publish', {'draft': {
                'name': 'Только ответы', 'videos': ['7420000000000000000'],
                'comments': [], 'replies': ['Ответ'], 'disabled_fields': ['comments'],
            }})
            await app.from_template(7, tid)
            await app.input_draft(7,'7420000000000000000')
            await click(app, 'wiz:quick:')
            assert 'wiz:edit_comments:' not in str(app.tg.out[-1][2])
            await app.edit_local_template(7,'publish',tid)
            await click(app,'lt:edit_comments:')
            await app.input_draft(7, 'Теперь нужен основной комментарий')
            values = app.db.draft(7)['data']['values']
            assert values['comments'] == ['Теперь нужен основной комментарий']
            assert 'comments' not in values.get('disabled_fields', [])
            await click(app,'lt:save:')
            await approve_template(app,7,tid)
        finally:
            app.db.close()
    asyncio.run(run())
