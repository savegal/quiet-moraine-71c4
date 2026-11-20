import asyncio
import json
from dataclasses import replace
from contextlib import closing

import pytest

from proxima_bot.config import Config
from proxima_bot.advanced_db import ExtendedStore
from proxima_bot.db import Store
from proxima_bot.extended import ProximaApp
from proxima_bot.order_form import field_presets, order_fields
from proxima_bot.worker import Worker
from test_extended import seed_approved_template, approve_template, new_app, button_callback, collected_entry, collected_cid, FakeTG, FakeAPI
from test_guided_order_form import options, click
from test_order_flows import VIDEO, COMMENT
from test_worker import FakeTokora, FakeTelegram
from proxima_bot.api import ApiError
from proxima_bot.catalog import build_payload
from proxima_bot.presentation import task_rich


def test_mode_access_overrides_initial_config_and_survives_restart(tmp_path):
    path = str(tmp_path / 'orders.sqlite3')
    config = Config('fake', 'fake', 99, restricted_modes=('publish', 'feedwarm'))
    with closing(ExtendedStore(path)) as db:
        app = ProximaApp(config, db, FakeTG(), FakeAPI())
        app.set_mode_restricted('publish', False)
        app.set_mode_restricted('reply_links', True)
        app.set_mode_restricted('reply_links', True)
        with pytest.raises(ValueError):
            app.set_mode_restricted('check', False)
    with closing(ExtendedStore(path)) as db:
        app = ProximaApp(config, db, FakeTG(), FakeAPI())
        assert app.restricted_modes() == {'check', 'feedwarm', 'reply_links'}
        assert app.mode_restricted(7, 'source_reply')
        assert app.mode_restricted(7, 'video_post')
        assert not app.mode_restricted(99, 'source_reply')
        assert len(db.audit_entries()) == 2


def test_mode_access_gates_old_order_and_followup_confirmations():
    async def run():
        app = new_app()
        try:
            await options(app, 'publish')
            await click(app, 'wiz:quick:')
            submit = button_callback(app, 'wiz:submit:')
            app.set_mode_restricted('publish', True)
            await app.callback(7, {'data': submit})
            assert not app.db.recent_tasks(7)

            source = app.db.queue_task(7, 'Сбор', 'fetch', {'assets': {'accountIds': ['acc7']}})
            app.db.transition(source, 'completed', upstream_id='source')
            publication = app.db.queue_task(7, 'Пост', 'publish', {'assets': {
                'accountIds': ['acc7'], 'replyTexts': ['Ответ'],
            }})
            app.db.transition(publication, 'completed', upstream_id='publication')

            async def reply_topup(method, path, **kwargs):
                return {'available': True, 'all': 1, 'repliesPerComment': 1}

            app.api.call = reply_topup
            seed_approved_template(app, 7,'Ответы','reply_links',{'draft':{'replies':['Ответ']}})
            await app.callback(7, {'data': 'replyinfo:' + publication})
            submit = button_callback(app, 'topup:')
            app.set_mode_restricted('reply_links', True)
            await app.callback(7, {'data': submit})
            assert len(app.db.recent_tasks(7)) == 2
            assert 'доступен только владельцу' in app.tg.out[-1][1]
            await app.callback(7, {'data': 'source_reply:' + source})
            assert app.db.draft(7)['mode'] == 'task_topup'
        finally:
            app.db.close()
    asyncio.run(run())


def test_reply_link_order_can_be_repeated_and_saved_when_regular_publish_is_restricted():
    async def run():
        app=new_app()
        app.cfg=replace(app.cfg,restricted_modes=('publish',))
        try:
            task=app.db.queue_task(7,'Ответы','publish',{
                'assets':{'accountIds':['acc7'],'replyTexts':['Ответ']},'replyLinks':[COMMENT],
            })
            app.db.transition(task,'completed',upstream_id='remote-answers')
            await app.task(7,task)
            await click(app,'repeat:')
            assert app.db.draft(7)['mode']=='reply_links'
            assert app.db.draft(7)['step']=='choose_template'
            await app.task(7,task)
            await click(app,'template_task:')
            assert app.db.draft(7)['data']['mode']=='reply_links'
            await click(app,'lt:save:')
            template,=app.db.templates(7)
            assert template['mode']=='reply_links'
            await approve_template(app,7,template['template_id'])
            await app.from_template(7,template['template_id'])
            assert app.db.draft(7)['mode']=='reply_links'
            assert app.db.draft(7)['step']=='reply_links'
        finally:
            app.db.close()
    asyncio.run(run())


def test_restricted_reply_link_order_cannot_reopen_from_a_legacy_publish_template():
    async def run():
        app=new_app()
        app.cfg=replace(app.cfg,restricted_modes=('reply_links',))
        try:
            payload={'kind':'publish','assets':{'replyTexts':['Ответ']},'replyLinks':[COMMENT]}
            template=seed_approved_template(app, 7,'Ответы','publish',{'job':payload})
            await app.from_template(7,template)
            assert app.db.draft(7) is None
            assert 'доступен только владельцу' in app.tg.out[-1][1]
            await app.template_card(7,template)
            assert 'tpl_use:' not in str(app.tg.out[-1][2])
            await app.prepare_copy(7,payload,'Повтор')
            assert app.db.draft(7) is None
        finally:
            app.db.close()
    asyncio.run(run())


def test_new_publication_defaults_presets_and_fixed_hidden_settings():
    async def run():
        app = new_app()
        try:
            await options(app, 'publish')
            fields = order_fields('publish', app.db.draft(7)['data'])
            assert {field.key: field.default for field in fields} == {
                'commentsPerVideo': 50, 'repliesPerComment': 3,
                'likesPerComment': 15, 'likesPerReply': 10,
            }
            assert field_presets(fields[0]) == (25, 50, 75, 100)
            assert field_presets(fields[1]) == (0, 2, 3, 5)
            assert field_presets(fields[2]) == (5, 10, 15, 20)
            await click(app, 'wiz:quick:')
            await click(app, 'wiz:submit:')
            settings = json.loads(app.db.recent_tasks(7)[0]['payload_json'])['settings']
            assert settings['delaySeconds'] == 2
            assert settings['uniqueText'] is False
            assert 'accountsPerVideo' not in settings
            assert settings['repliesPerVideo'] == 150
        finally:
            app.db.close()
    asyncio.run(run())


def test_zero_replies_disables_reply_likes_and_json_cannot_change_delay():
    async def run():
        app = new_app()
        try:
            await options(app, 'publish')
            await app.input_draft(7, '{"delaySeconds":0}')
            assert app.db.draft(7)['step'] == 'options'
            assert not app.db.draft(7)['data'].get('options')
            token = app.db.draft(7)['data']['wizard_token']
            await app.callback(7, {'data': 'wiz:pick_repliesPerComment_0:' + token})
            await click(app, 'wiz:quick:')
            await click(app, 'wiz:submit:')
            settings = json.loads(app.db.recent_tasks(7)[0]['payload_json'])['settings']
            assert settings['delaySeconds'] == 2
            assert settings['repliesPerComment'] == settings['repliesPerVideo'] == settings['likesPerReply'] == 0
        finally:
            app.db.close()
    asyncio.run(run())


def test_fresh_targets_are_required_and_never_saved_as_preferences():
    async def run():
        app = new_app()
        try:
            app.db.set_preferences(7, {'last_videos': [VIDEO]})
            tid = seed_approved_template(app, 7, 'Старый', 'publish', {'draft': {
                'videos': [VIDEO], 'comments': ['Текст'], 'replies': [],
            }})
            await app.from_template(7, tid)
            assert app.db.draft(7)['step'] == 'videos'
            assert 'Сохранённые видео' not in str(app.tg.out[-1])
            assert 'videos' not in app.db.draft(7)['data']
        finally:
            app.db.close()
    asyncio.run(run())


def test_collection_keywords_are_sent_and_active_workspace_does_not_reject_queue():
    async def run():
        app = new_app()
        try:
            await app.begin(7, 'fetch')
            for value in ('Сбор', VIDEO):
                await app.input_draft(7, value)
            await click(app, 'wiz:fetch_1000:')
            await click(app, 'wiz:fetch_without_replies:')
            await app.input_draft(7, 'камера\nобзор')
            await click(app, 'wiz:quick:')
            await click(app, 'wiz:submit:')
            task = app.db.recent_tasks(7)[0]
            assert task['state'] == 'queued'
            settings = json.loads(task['payload_json'])['settings']
            assert settings['fetchCount'] == 1000
            assert settings['fetchKeywords'] == ['камера', 'обзор']
            assert settings['includeReplies'] is False
        finally:
            app.db.close()
    asyncio.run(run())


def test_selected_collected_comment_ids_and_reply_template_preview():
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
            seed_approved_template(app, 7, 'Ответы', 'publish', {'draft': {'replies': ['Ответ из шаблона']}})
            await app.callback(7, {'data': 'source_reply:' + source})
            await app.input_draft(7, '2')
            await click(app, 'source:tpl_')
            await click(app, 'source:skip:')
            assert 'Ответ из шаблона' in app.tg.out[-1][1]
            await click(app, 'source:submit:')
            task = next(task for task in app.db.recent_tasks(7) if task['kind'] == 'publish')
            payload = json.loads(task['payload_json'])
            assert payload['sourceJob']=='source' and payload['targetIds']==[collected_cid(0),collected_cid(1)]
            assert not {'replyLinks','replySourceMode'} & payload.keys()
            assert payload['settings']['delaySeconds'] == 2
            assert 'targetCount' not in payload['settings']
        finally:
            app.db.close()
    asyncio.run(run())


def test_timeout_survives_worker_restart_and_waits_for_stop_confirmation(monkeypatch):
    clock = [1_800_000_000]
    monkeypatch.setattr('proxima_bot.db.now', lambda: clock[0])
    monkeypatch.setattr('proxima_bot.worker.now', lambda: clock[0])
    class Provider(FakeTokora):
        stopped = False
        stops = 0
        async def start(self, payload):
            self.started.append(payload)
            return {'id': 'job' + str(len(self.started)), 'state': 'running', 'attempts': 1}
        async def stop(self, tid):
            self.stops += 1
            return {'id':tid,'state':'running','stopRequested':True}
        async def task(self, tid):
            if self.stopped:return {'id':tid,'state':'stopped','finishedAt':'2026-11-16T16:00:00Z'}
            return await super().task(tid)
    async def run():
        db = Store(':memory:')
        try:
            db.grant(7, 30); db.assign(7, 'a')
            first = db.queue_task(7, 'Первый', 'publish', {'assets': {'accountIds': ['a']}})
            db.queue_task(7, 'Второй', 'publish', {'assets': {'accountIds': ['a']}})
            api = Provider(); cfg = Config('fake', 'fake', 99, dispatch_enabled=True, max_active_external=1)
            worker = Worker(db, api, FakeTelegram(), cfg)
            await worker.tick()
            assert db.task(first)['execution_started_at'] == clock[0]
            clock[0] += 1200
            worker = Worker(db, api, FakeTelegram(), cfg)
            await worker.tick()
            assert api.stops == 1 and len(api.started) == 1
            assert db.task(first)['state'] == 'stopping'
            api.stopped = True
            await worker.tick()
            assert db.task(first)['state'] == 'stopped'
            assert len(api.started) == 2 and api.stops == 1
        finally:
            db.close()
    asyncio.run(run())


def test_import_owner_access_does_not_leak_to_other_members_or_follow_reassignment():
    db = Store(':memory:')
    try:
        db.assign(7, 'imported')
        db.conn.execute('INSERT INTO imported_owner_access VALUES (?,?,?)', ('imported', 7, 99))
        assert db.account_ids(7) == db.account_ids(99) == ['imported']
        assert db.account_ids(8) == []
        assert db.eligible_task_accounts(99, ['imported'], 99) == ['imported']
        db.assign(8, 'imported')
        assert db.account_ids(99) == []
    finally:
        db.close()


def test_busy_executor_keeps_order_queued_with_retry_reason():
    async def run():
        db=Store(':memory:')
        try:
            db.grant(7,30); db.assign(7,'a')
            tid=db.queue_task(7,'Ожидание','fetch',{'assets':{'accountIds':['a']}})
            api=FakeTokora()
            async def busy(payload):
                raise ApiError('job_busy',503)
            api.start=busy
            await Worker(db,api,FakeTelegram(),Config('fake','fake',99,dispatch_enabled=True)).tick()
            assert db.task(tid)['state']=='queued'
            assert db.task(tid)['error_code']=='job_busy'
            card=task_rich(db.task(tid))
            assert 'Заказ поставлен в очередь' in card and 'Сервис занят' in card
        finally:db.close()
    asyncio.run(run())


def test_video_collection_has_no_actions_and_empty_result_requests_new_topic():
    async def run():
        app=new_app()
        try:
            await options(app,'feedwarm')
            await click(app,'wiz:quick:')
            await click(app,'wiz:submit:')
            task=app.db.recent_tasks(7)[0]
            warm=json.loads(task['payload_json'])['feedWarm']
            assert warm['comment'] is warm['like'] is False
            assert warm['noFeed'] is warm['search'] is True
            app.db.transition(task['task_id'],'completed',snapshot={'collected':0})
            assert 'Измени тему поиска' in task_rich(app.db.task(task['task_id']))
        finally:app.db.close()
    asyncio.run(run())


def test_video_link_cannot_be_used_as_comment_like_target():
    async def run():
        app=new_app()
        try:
            with pytest.raises(ValueError,match='комментарий'):
                await build_payload(app.api,'commentlike',{'name':'Лайки','like_links':[VIDEO],'likes':15},['acc7'])
            payload=await build_payload(app.api,'commentlike',{'name':'Лайки','like_links':[COMMENT],'likes':15},['acc7'])
            assert payload['likeLinks']==[COMMENT]
        finally:app.db.close()
    asyncio.run(run())


def test_main_menu_and_tools_have_no_removed_sections():
    async def run():
        app=new_app()
        try:
            await app.show_home(7)
            assert not any(button['callback_data']=='settings' for row in app.tg.out[-1][2]['inline_keyboard'] for button in row)
            await app.tools_menu(7)
            assert not any(button['callback_data']=='materials' for row in app.tg.out[-1][2]['inline_keyboard'] for button in row)
        finally:app.db.close()
    asyncio.run(run())
