import asyncio
import json
import sqlite3

from proxima_bot.advanced_db import ExtendedStore
from proxima_bot.config import Config
from proxima_bot.extended import ProximaApp
from test_extended import FakeAPI, FakeTG, approve_template, button_callback, new_app, seed_approved_template


VIDEO = '7420000000000000000'


async def click(app, uid, prefix):
    await app.callback(uid, {'data': button_callback(app, prefix)})


async def propose_text(app, tid, field, value):
    await app.callback(7, {'data': 'tpl_edit:' + tid})
    await click(app, 7, 'lt:edit_' + field + ':')
    await app.input_draft(7, value)
    await click(app, 7, 'lt:save:')
    return app.db.template_review(7, tid)


def test_template_needs_owner_approval_and_revisions_keep_the_active_texts_until_approved():
    async def run():
        app = new_app()
        try:
            await app.callback(7, {'data': 'tpl_new'})
            for preset in ('main', 'reply', 'full'):
                assert button_callback(app, 'tpl_new:' + preset)
            await click(app, 7, 'tpl_new:main')
            await app.input_draft(7, 'Тексты')
            await click(app, 7, 'lt:edit_comments:')
            await app.input_draft(7, 'Главный <старый>')
            await click(app, 7, 'lt:edit_options:')
            await click(app, 7, 'lt:keep:')
            await click(app, 7, 'lt:save:')
            tid = app.db.templates(7)[0]['template_id']
            original = app.db.template_review(7, tid)
            assert not app.matching_order_templates(7, 'publish')
            assert app.tg.out[-2][0] == 99
            await app.callback(7, {'data': 'tr:approve:' + original['revision']})
            await app.callback(7, {'data': 'tpl_use:' + tid})
            assert 'нужен одобренный шаблон' in app.tg.out[-1][1]
            assert not app.db.recent_tasks(7)
            await approve_template(app, 7, tid)

            first_edit = await propose_text(app, tid, 'replies', 'Первый вариант')
            assert not app.matching_order_templates(7, 'source_reply')
            new_edit = await propose_text(app, tid, 'replies', 'Второй вариант')
            await app.callback(99, {'data': 'tr:approve:' + first_edit['revision']})
            assert app.db.template(7, tid)['approved_revision'] == original['revision']
            await app.callback(99, {'data': 'tr:list:0'})
            await click(app, 99, 'tr:view:')
            content = app.tg.out[-1][1]
            assert all(word in content for word in ('Было', 'Станет', '&lt;старый&gt;', 'Второй вариант'))
            await click(app, 99, 'tr:text:')
            exported = app.tg.documents[-1][1].decode()
            assert exported.index('БЫЛО') < exported.index('СТАНЕТ') < exported.index('Второй вариант')
            await app.callback(99, {'data': 'tr:reject:' + new_edit['revision']})
            assert app.db.template_review(7, tid)['state'] == 'rejected'
            assert not app.matching_order_templates(7, 'reply_links')

            await propose_text(app, tid, 'replies', 'Одобренный ответ')
            await app.from_template(7, tid)
            await app.input_draft(7, VIDEO)
            await click(app, 7, 'wiz:quick:')
            stale = button_callback(app, 'wiz:submit:')
            await approve_template(app, 7, tid)
            await app.callback(7, {'data': stale})
            assert not app.db.recent_tasks(7)
            assert app.db.draft(7)['step'] == 'choose_template'
            await click(app, 7, 'wiz:tpl_' + tid + ':')
            await click(app, 7, 'wiz:quick:')
            await click(app, 7, 'wiz:submit:')
            task, = app.db.recent_tasks(7)
            payload = json.loads(task['payload_json'])
            assert payload['assets']['replyTexts'] == ['Одобренный ответ']
            assert payload['settings']['repliesPerComment'] == 3
            await app.callback(7, {'data': 'tpl:' + tid})
            await click(app, 7, 'tpl_text:')
            assert 'Главный <старый>' in app.tg.documents[-1][1].decode()
        finally:
            app.db.close()

    asyncio.run(run())


def test_keyword_reply_access_alone_offers_a_reply_template():
    async def run():
        app = new_app()
        try:
            app.set_mode_restricted('publish', True)
            app.set_mode_restricted('reply_links', True)
            await app.callback(7, {'data': 'tpl_new'})
            assert 'tpl_new:main' not in str(app.tg.out[-1][2])
            await click(app, 7, 'tpl_new:reply')
            await app.input_draft(7, 'Ответы по ключевым словам')
            await click(app, 7, 'lt:edit_replies:')
            await app.input_draft(7, 'Сохранённый ответ')
            await click(app, 7, 'lt:save:')
            tid = app.db.templates(7)[0]['template_id']
            await approve_template(app, 7, tid)
            await app.callback(7, {'data': 'new:keyword_reply'})
            await click(app, 7, 'wiz:tpl_' + tid + ':')
            await app.input_draft(7, VIDEO)
            await app.input_draft(7, 'Искомая фраза')
            await click(app, 7, 'wiz:quick:')
            await click(app, 7, 'wiz:submit:')
            task, = app.db.recent_tasks(7)
            assert json.loads(task['payload_json'])['assets']['replyTexts'] == ['Сохранённый ответ']
        finally:
            app.db.close()

    asyncio.run(run())


def test_failed_publication_asks_for_replacement_links_after_template_selection():
    async def run():
        app = new_app()
        try:
            for targets,mode,step,materials in (
                ({'awemeIds':['https://vt.tiktok.com/old/']},'publish','videos',{'comments':['Главный']}),
                ({'replyLinks':['7420000000000000000:7420000000000000001']},'reply_links','reply_links',{'replies':['Ответ']}),
            ):
                tid = seed_approved_template(app,7,'Тексты',mode,{'draft':materials})
                old = app.db.queue_task(7,'Заказ с плохой ссылкой','publish', {
                    **targets,'assets':{'accountIds':['acc7']},
                })
                app.db.transition(old,'failed',error_code='invalid_order_targets')
                await app.task(7,old)
                await click(app,7,'repeat:')
                await click(app,7,'wiz:tpl_' + tid + ':')
                draft = app.db.draft(7)
                assert (draft['mode'],draft['step']) == (mode,step)
                assert step not in draft['data']
        finally:
            app.db.close()

    asyncio.run(run())


def test_reopened_collection_and_results_offer_reply_and_never_queue_manual_text():
    async def run():
        app = new_app()
        try:
            tid = app.db.queue_task(7, 'Сбор', 'fetch', {'assets': {'accountIds': ['acc7']}})
            app.db.transition(tid, 'completed', upstream_id='past-collection')
            await app.tasks(7)
            await click(app, 7, 'task:' + tid)
            assert button_callback(app, 'source_reply:') == 'source_reply:' + tid
            await click(app, 7, 'entries:')
            await click(app, 7, 'source_reply:')
            await app.input_draft(7, '2')
            await app.input_draft(7, 'Текст без одобрения')
            assert app.db.draft(7)['step'] == 'replies'
            assert len(app.db.recent_tasks(7)) == 1
            app.save_wizard_step(7,'source_reply','review',{'source_task':tid,'replies':['Старый текст']})
            await app.submit(7)
            assert app.db.draft(7)['step'] == 'replies'
            assert len(app.db.recent_tasks(7)) == 1
            assert not any('/reply-plan' in path for _,path,_ in app.api.calls)
        finally:
            app.db.close()

    asyncio.run(run())


def test_media_visibility_toggles_are_independent_and_old_upload_buttons_are_blocked(tmp_path):
    async def change_video():
        db = ExtendedStore(str(tmp_path / 'media.sqlite3'))
        app = ProximaApp(Config('test', 'test', 99), db, FakeTG(), FakeAPI())
        try:
            await app.admin_order_modes(99)
            hide_video = button_callback(app,'adm:mode:video_post:1')
            await app.callback(7, {'data': hide_video})
            assert not app.mode_restricted(99,'video_post')
            db.register_object('post_plan_video','existing-video-plan',99)
            await app.callback(99, {'data': hide_video})
            await click(app,99,'adm:modeapply:')
        finally:
            db.close()

    asyncio.run(change_video())

    async def reopen():
        db = ExtendedStore(str(tmp_path / 'media.sqlite3'))
        app = ProximaApp(Config('test', 'test', 99), db, FakeTG(), FakeAPI())
        try:
            await app.create(99)
            assert 'upload:video' not in str(app.tg.out[-1][2])
            assert button_callback(app,'owner_edit:album')
            await app.callback(99, {'data':'upload:video'})
            assert db.draft(99) is None
            await app.callback(99, {'data':'plan_confirm:existing-video-plan'})
            assert not app.api.calls
            await app.template_menu(99)
            assert 'tpl_new:video_post' not in str(app.tg.out[-1][2])
            assert button_callback(app,'tpl_new:photo_post')
        finally:
            db.close()

    asyncio.run(reopen())


def test_media_plan_uses_provider_draft_kinds_for_old_and_mixed_plans():
    async def run():
        app = new_app()
        kinds = {'old-video':['video'],'old-photo':['photo'],'mixed':['video','photo']}

        async def provider(method,path,**kwargs):
            assert method == 'GET'
            pid = path.rsplit('/',1)[-1]
            return {'id':pid,'state':'ready','postsTotal':1,'queued':0,
                    'drafts':[{'kind':kind} for kind in kinds[pid]]}

        app.api.call = provider
        try:
            await app.callback(99,{'data':'plan_confirm:old-video'})
            stale_submit = button_callback(app,'owner_do:plan_launch:')
            app.set_mode_restricted('video_post',True)
            await app.callback(99,{'data':stale_submit})
            assert 'plan_confirm:' not in str(app.tg.out[-1][2])
            await app.callback(99,{'data':'plan_confirm:mixed'})
            assert 'owner_do:' not in str(app.tg.out[-1][2])
            await app.callback(99,{'data':'plan_confirm:old-photo'})
            assert button_callback(app,'owner_do:plan_launch:')
        finally:
            app.db.close()

    asyncio.run(run())


def test_old_templates_are_migrated_to_review_and_preserved_across_restarts(tmp_path):
    path=str(tmp_path / 'legacy.sqlite3')
    with sqlite3.connect(path) as conn:
        conn.execute('''CREATE TABLE user_templates (
            user_id INTEGER NOT NULL, template_id TEXT NOT NULL, name TEXT NOT NULL,
            mode TEXT NOT NULL, payload TEXT NOT NULL, created_at INTEGER NOT NULL,
            is_shared INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,template_id))''')
        conn.execute('INSERT INTO user_templates VALUES (?,?,?,?,?,?,?)',
                     (7,'abcdef12','Старый','publish','{"draft":{"comments":["Text"]}}',1,0))
    with_db = ExtendedStore(path)
    revision=with_db.template_review(7,'abcdef12')['revision']
    with_db.close()

    async def reopen():
        db=ExtendedStore(path)
        app=ProximaApp(Config('test','test',99),db,FakeTG(),FakeAPI())
        try:
            assert db.template_review(7,'abcdef12')['revision']==revision
            assert not app.matching_order_templates(7,'publish')
            await approve_template(app,7,'abcdef12')
            assert app.matching_order_templates(7,'publish')[0]['approved_revision']==revision
        finally:
            db.close()

    asyncio.run(reopen())
