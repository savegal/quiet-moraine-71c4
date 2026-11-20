import asyncio
from datetime import datetime, timedelta

from test_extended import seed_approved_template, button_callback, new_app
from test_guided_order_form import click
from test_order_flows import VIDEO


def save(app, uid=7, mode='publish', **fields):
    return seed_approved_template(app, uid, 'Готовые тексты', mode, {'draft': {
        'name': 'Готовые тексты', 'comments': ['Комментарий из шаблона'],
        'replies': ['Ответ из шаблона'], 'ask_fields': ['videos'], **fields,
    }})


def test_template_can_be_chosen_inside_new_order_and_volume_is_still_confirmed():
    async def run():
        app = new_app()
        try:
            tid = save(app, videos=['7420000000000000000'])
            await app.callback(7, {'data': 'new:publish'})
            assert app.db.draft(7)['step']=='choose_template'
            await click(app, 'wiz:tpl_' + tid + ':')
            assert app.db.draft(7)['step'] == 'videos'
            await app.input_draft(7, VIDEO)
            draft = app.db.draft(7)
            assert draft['step'] == 'options'
            assert draft['data']['comments'] == ['Комментарий из шаблона']
            assert draft['data']['replies'] == ['Ответ из шаблона']
            assert app.db.recent_tasks(7) == []
            await click(app, 'wiz:quick:')
            assert app.db.draft(7)['step'] == 'review'
        finally:
            app.db.close()
    asyncio.run(run())


def test_template_selection_keeps_targets_and_applies_saved_volume():
    async def run():
        app = new_app()
        try:
            tid = save(app, videos=['7420000000000000009'], options={'commentsPerVideo': 60})
            await app.choose_order_template(7,'publish',{'name':'Повтор','videos':[VIDEO]})
            await click(app, 'wiz:tpl_' + tid + ':')
            current = app.db.draft(7)
            assert current['step'] == 'options'
            assert current['data']['videos'] == [VIDEO]
            assert current['data']['options']['commentsPerVideo'] == 60
        finally:
            app.db.close()
    asyncio.run(run())


def test_picker_filters_ownership_type_and_handles_cancel_and_pagination():
    async def run():
        app = new_app()
        try:
            foreign = save(app, uid=8)
            other = save(app, mode='reply_links')
            ids = [save(app) for _ in range(12)]
            await app.callback(7, {'data': 'new:publish'})
            original = app.db.draft(7)
            assert foreign not in str(app.tg.out[-1][2])
            assert other not in str(app.tg.out[-1][2])
            await click(app, 'wiz:tplpage_1:')
            assert ids[0] in str(app.tg.out[-1][2])
            await click(app, 'create')
            assert app.db.draft(7)['data'] == original['data']
            token = original['data']['wizard_token']
            for tid in (foreign, other):
                await app.callback(7, {'data': f'wiz:tpl_{tid}:{token}'})
                assert app.db.draft(7)['data'] == original['data']
                assert 'недоступен' in app.tg.out[-1][1]
        finally:
            app.db.close()
    asyncio.run(run())


def test_no_template_prevents_order_creation():
    async def run():
        app = new_app()
        try:
            await app.callback(7, {'data': 'new:publish'})
            assert app.db.draft(7)['step'] == 'choose_template'
            await app.input_draft(7,VIDEO)
            await app.submit(7)
            assert app.db.draft(7)['step']=='choose_template'
            assert not app.db.recent_tasks(7)
            assert 'одобренный владельцем' in app.tg.out[-1][1]
        finally:
            app.db.close()
    asyncio.run(run())


def test_template_order_can_be_scheduled_from_volume_screen():
    async def run():
        app = new_app()
        try:
            tid = save(app)
            await app.from_template(7,tid)
            await app.input_draft(7,VIDEO)
            assert app.db.draft(7)['step']=='options'
            await click(app,'wiz:skip:')
            assert app.db.draft(7)['step']=='when'
            when=(datetime.now(app.schedule_zone)+timedelta(days=1)).replace(second=0,microsecond=0)
            await app.input_draft(7,when.strftime('%d.%m.%Y %H:%M'))
            await click(app,'wiz:submit:')
            task,=app.db.recent_tasks(7)
            assert task['fire_at']==int(when.timestamp())
        finally:
            app.db.close()

    asyncio.run(run())
