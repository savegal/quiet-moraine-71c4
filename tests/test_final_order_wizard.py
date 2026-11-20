import asyncio
import json

import pytest

from test_extended import approved_order_data, button_callback, new_app
from test_guided_order_form import options
from test_order_flows import OrderProvider, SCENARIOS
from proxima_bot.catalog import resolve_payload_targets


@pytest.mark.parametrize('change', ['add', 'replace'])
def test_member_confirms_only_the_account_pool_shown_in_review(change):
    async def run():
        app = new_app()
        try:
            app.save_wizard_step(7, 'publish', 'review', approved_order_data(app,7,'publish',{
                'name': 'Проверка состава', 'videos': ['7420000000000000000'],
                'comments': ['Комментарий'], 'replies': [],
            }))
            await app.show_wizard(7)
            confirmation = button_callback(app, 'wiz:submit:')
            if change == 'replace':
                app.db.unassign(7, 'acc7')
            app.db.assign(7, 'new-account')
            await app.callback(7, {'data': confirmation})
            assert app.db.recent_tasks(7) == []

            await app.callback(7, {'data': 'wiz_continue'})
            confirmation = button_callback(app, 'wiz:submit:')
            await app.callback(7, {'data': confirmation})
            task, = app.db.recent_tasks(7)
            expected = ['new-account'] if change == 'replace' else ['acc7', 'new-account']
            assert json.loads(task['payload_json'])['assets']['accountIds'] == expected
        finally:
            app.db.close()

    asyncio.run(run())


@pytest.mark.parametrize('mode', list(SCENARIOS))
def test_order_review_keeps_targets_and_quantity_when_editing_extra_options(mode):
    async def run():
        app = new_app()
        app.api = OrderProvider()
        try:
            await options(app,mode)
            _, expected = SCENARIOS[mode]
            await app.callback(7, {'data': button_callback(app, 'wiz:quick:')})
            draft_before = app.db.draft(7)['data']
            await app.callback(7, {'data': button_callback(app, 'wiz:edit_options:')})
            await app.input_draft(7, '{"delaySeconds":2}')
            assert app.db.draft(7)['step'] == 'options'
            assert 'Проверь ввод' in app.tg.out[-1][1]
            await app.callback(7, {'data': button_callback(app, 'wiz:quick:')})
            draft_after = app.db.draft(7)['data']
            for key in draft_before.keys()-{'wizard_token','options'}:
                assert draft_before[key] == draft_after[key]
            assert draft_after['options'] == {**draft_before.get('options',{}),'delaySeconds':2}
            await app.callback(7, {'data': button_callback(app, 'wiz:submit:')})
            task, = app.db.recent_tasks(7)
            payload = await resolve_payload_targets(app.api,json.loads(task['payload_json']))
            assert payload['settings']['delaySeconds'] == 2
            for key in ('awemeIds','replyLinks','likeLinks','likesPerComment','feedWarm'):
                if key in expected:
                    assert payload[key] == expected[key]
        finally:
            app.db.close()

    asyncio.run(run())
