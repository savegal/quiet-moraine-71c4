import asyncio
import json

import pytest

from proxima_bot.api import ApiError
from proxima_bot.db import task_target_links_count
from proxima_bot.worker import task_execution_limit_seconds
from test_extended import seed_approved_template, button_callback, new_app
from test_order_flows import VIDEO


def test_imported_owner_access_cleared_on_unassign_and_reassignment():
    app = new_app()
    try:
        app.db.grant(8, 30)
        app.db.assign(7, 'acc-imp')
        app.db.conn.execute(
            'INSERT INTO imported_owner_access(account_id, target_id, owner_id) VALUES (?, ?, ?)',
            ('acc-imp', 7, 99),
        )
        app.db.conn.commit()
        assert 'acc-imp' in app.db.account_ids(99)

        # Unassigning from the owner drops imported_owner_access without removing the trafficker's account.
        assert app.db.unassign(99, 'acc-imp') is True
        assert 'acc-imp' not in app.db.account_ids(99)
        assert 'acc-imp' in app.db.account_ids(7)

        # Unassigning from the trafficker also purges imported_owner_access so later assignment cannot resurrect it.
        app.db.conn.execute(
            'INSERT INTO imported_owner_access(account_id, target_id, owner_id) VALUES (?, ?, ?)',
            ('acc-imp', 7, 99),
        )
        app.db.conn.commit()
        assert app.db.unassign(7, 'acc-imp') is True
        assert app.db.account_ids(99) == []
        app.db.assign(8, 'acc-imp')
        assert app.db.account_ids(99) == []

        # Reassignment through assign and assign_available_accounts clears stale imported_owner_access too.
        app.db.conn.execute(
            'INSERT INTO imported_owner_access(account_id, target_id, owner_id) VALUES (?, ?, ?)',
            ('acc-direct', 7, 99),
        )
        app.db.conn.execute(
            'INSERT INTO imported_owner_access(account_id, target_id, owner_id) VALUES (?, ?, ?)',
            ('acc-batch', 7, 99),
        )
        app.db.conn.commit()
        app.db.assign(8, 'acc-direct')
        assert app.db.assign_available_accounts(8, ['acc-batch']) == 1
        assert app.db.account_ids(99) == []
    finally:
        app.db.close()


def test_folder_sync_unassigns_imported_owner_access_only_for_owner():
    async def run():
        app = new_app()
        try:
            app.db.assign(7, 'acc-keep')
            app.db.conn.execute(
                'INSERT INTO imported_owner_access(account_id, target_id, owner_id) VALUES (?, ?, ?)',
                ('acc-keep', 7, 99),
            )
            app.db.conn.commit()

            async def accounts_in_folder(fid):
                assert fid == 'aaaa1111'
                return {'acc-owner-free': {'accountId': 'acc-owner-free', 'status': 'active'}}

            app.api.accounts_in_folder = accounts_in_folder
            await app.admin_folder_sync_apply(99, 99, 'aaaa1111', 'e')
            assert set(app.db.account_ids(99)) == {'acc-owner-free'}
            assert set(app.db.account_ids(7)) == {'acc7', 'acc-keep'}
            assert 'Пул совпадает с папкой' in str(app.tg.out[-1][1])
        finally:
            app.db.close()

    asyncio.run(run())


def test_assign_batch_resolves_inactive_account_folder_and_prevents_folder_mixing():
    async def run():
        app = new_app()
        try:
            app.db.unassign(7, 'acc7')
            app.db.assign(7, 'acc-expired')

            async def api_call(method, path, **kwargs):
                assert method == 'GET'
                if path == '/v1/accounts':
                    params = kwargs.get('params') or {}
                    if params.get('status') == 'active':
                        return {
                            'accounts': [
                                {'accountId': 'cand-b1', 'status': 'active', 'folderId': 'bbbb2222'},
                                {'accountId': 'cand-a1', 'status': 'active', 'folderId': 'aaaa1111'},
                                {'accountId': 'cand-a2', 'status': 'active', 'folderId': 'aaaa1111'},
                            ]
                        }
                    return {'accounts': [{'accountId': 'acc-expired', 'status': 'expired', 'folderId': 'aaaa1111'}]}
                if path == '/v1/accounts/acc-expired':
                    return {'accountId': 'acc-expired', 'status': 'expired', 'folderId': 'aaaa1111'}
                raise AssertionError((method, path, kwargs))

            app.api.call = api_call
            added = await app.assign_batch(7, 2)
            assert added == 2
            assert set(app.db.account_ids(7)) == {'acc-expired', 'cand-a1', 'cand-a2'}

            # If the accounts page returns a non-active existing item directly, its folderId is still honored.
            app.db.unassign(7, 'cand-a1')
            app.db.unassign(7, 'cand-a2')

            async def page_with_expired_existing(method, path, **kwargs):
                assert (method, path) == ('GET', '/v1/accounts')
                return {
                    'accounts': [
                        {'accountId': 'acc-expired', 'status': 'expired', 'folderId': 'aaaa1111'},
                        {'accountId': 'cand-b1', 'status': 'active', 'folderId': 'bbbb2222'},
                        {'accountId': 'cand-a1', 'status': 'active', 'folderId': 'aaaa1111'},
                    ]
                }

            app.api.call = page_with_expired_existing
            assert await app.assign_batch(7, 2) == 1
            assert set(app.db.account_ids(7)) == {'acc-expired', 'cand-a1'}

            # User with no existing accounts still selects a single folder instead of mixing page order.
            app.db.grant(9, 30)

            async def multi_folder_api(method, path, **kwargs):
                assert (method, path) == ('GET', '/v1/accounts')
                return {
                    'accounts': [
                        {'accountId': 'cand-b1', 'status': 'active', 'folderId': 'bbbb2222'},
                        {'accountId': 'cand-c1', 'status': 'active', 'folderId': 'cccc3333'},
                        {'accountId': 'cand-c2', 'status': 'active', 'folderId': 'cccc3333'},
                    ]
                }

            app.api.call = multi_folder_api
            added_new = await app.assign_batch(9, 2)
            assert added_new == 2
            assert set(app.db.account_ids(9)) == {'cand-c1', 'cand-c2'}

            # Reset user 9 to two folders and confirm assign_batch refuses to mix further.
            app.db.assign(9, 'cand-x1')

            async def mixed_api(method, path, **kwargs):
                if path == '/v1/accounts' and (kwargs.get('params') or {}).get('status') == 'active':
                    return {
                        'accounts': [
                            {'accountId': 'cand-c1', 'status': 'active', 'folderId': 'bbbb2222'},
                            {'accountId': 'cand-x1', 'status': 'active', 'folderId': 'aaaa1111'},
                            {'accountId': 'cand-free', 'status': 'active', 'folderId': 'aaaa1111'},
                        ]
                    }
                raise AssertionError((method, path, kwargs))

            app.api.call = mixed_api
            with pytest.raises(ValueError):
                await app.assign_batch(9, 1)
        finally:
            app.db.close()

    asyncio.run(run())


def test_assign_batch_unfoldered_existing_never_mixes_with_foldered_candidates():
    async def run():
        app = new_app()
        try:
            app.db.unassign(7, 'acc7')
            app.db.assign(7, 'acc-plain')

            async def mixed_candidates(method, path, **kwargs):
                if path == '/v1/accounts':
                    return {
                        'accounts': [
                            {'accountId': 'acc-plain', 'status': 'active'},
                            {'accountId': 'cand-foldered', 'status': 'active', 'folderId': 'aaaa1111'},
                            {'accountId': 'cand-plain', 'status': 'active'},
                        ]
                    }
                raise AssertionError((method, path, kwargs))

            app.api.call = mixed_candidates
            assert await app.assign_batch(7, 2) == 1
            assert set(app.db.account_ids(7)) == {'acc-plain', 'cand-plain'}

            app.db.unassign(7, 'cand-plain')

            async def foldered_only(method, path, **kwargs):
                if path == '/v1/accounts':
                    return {
                        'accounts': [
                            {'accountId': 'acc-plain', 'status': 'active'},
                            {'accountId': 'cand-foldered', 'status': 'active', 'folderId': 'aaaa1111'},
                        ]
                    }
                raise AssertionError((method, path, kwargs))

            app.api.call = foldered_only
            with pytest.raises(ValueError):
                await app.assign_batch(7, 1)
        finally:
            app.db.close()

    asyncio.run(run())


def test_main_menu_empty_assigned_accounts_dict_reports_zero_available():
    async def run():
        app = new_app()
        try:
            app.db.unassign(7, 'acc7')
            app.db.assign(7, 'acc-1')
            app.db.assign(7, 'acc-2')

            async def empty_page(offset, limit=200):
                return {'accounts': []}

            async def empty_call(method, path, **kwargs):
                return {'accounts': []}

            app.api.account_page = empty_page
            app.api.call = empty_call
            data = await app.get_main_menu_data(7)
            assert data['total_accounts'] == 2
            assert data['available_accounts'] == 0

            app.db.grant(9, 30)
            app.db.assign(9, 'acc-3')
            app.db.assign(9, 'acc-4')

            async def failing_page(offset, limit=200):
                raise ApiError('network_error')

            async def failing_call(method, path, **kwargs):
                raise ApiError('network_error')

            app.api.account_page = failing_page
            app.api.call = failing_call
            fallback = await app.get_main_menu_data(9)
            assert fallback['total_accounts'] == 2
            assert fallback['available_accounts'] is None
        finally:
            app.db.close()

    asyncio.run(run())


def test_reply_topup_supports_all_mode_rejects_empty_and_preserves_policy_and_target_count():
    async def run():
        app = new_app()
        try:
            app.db.assign(7, 'acc7')
            source_id = app.db.queue_task(
                7,
                'Публикация',
                'publish',
                {
                    'assets': {
                        'accountIds': ['acc7'],
                        'videos': [VIDEO, 'https://www.tiktok.com/@u/video/7321000000000000002'],
                        'comments': ['c'],
                        'replyTexts': ['Ответ'],
                    },
                    'settings': {'commentsPerVideo': 5, 'repliesPerComment': 2, 'repliesPerVideo': 10},
                },
            )
            app.db.transition(source_id, 'completed', upstream_id='job-1')
            source = app.db.task(source_id)
            seed_approved_template(app, 7,'Для доотправки','reply_links',{'draft':{'replies':['Ответ']}})

            async def empty_topup(method, path, **kwargs):
                return {'available': True, 'failed': 0, 'all': 0, 'repliesPerComment': 2}

            app.api.call = empty_topup
            await app.topup_info(7, source, 'reply')
            assert app.db.draft(7) is None

            async def all_topup(method, path, **kwargs):
                return {
                    'available': True,
                    'failed': 0,
                    'all': 12,
                    'videosFailed': 0,
                    'videosAll': 3,
                    'repliesPerComment': 2,
                }

            app.api.call = all_topup
            await app.topup_info(7, source, 'reply')
            draft = app.db.draft(7)
            assert draft is not None
            assert '12' in app.tg.out[-1][1]
            await app.submit_topup(7, source, 'reply', draft['data']['wizard_token'])

            followup = next(t for t in app.db.recent_tasks(7) if t['task_id'] != source_id)
            payload = json.loads(followup['payload_json'])
            assert payload['sourceJob'] == 'job-1'
            assert payload['replySourceMode'] == 'all'
            assert payload['settings'] == {
                'repliesPerComment': 2,
                'repliesPerVideo': 24,
                'delaySeconds': 2,
                'uniqueText': False,
            }
            assert followup['target_count'] == 3
            assert task_target_links_count(followup) == 3
            assert task_execution_limit_seconds(followup) > task_execution_limit_seconds(
                {'kind': 'publish', 'payload_json': '{}'}
            )

            # Like top-up chained onto a sourceJob task inherits stored target_count without shadowing.
            async def like_topup(method, path, **kwargs):
                return {'available': True, 'likesPerItem': 3, 'likesPerReply': 0}

            app.api.call = like_topup
            app.db.transition(followup['task_id'], 'completed', upstream_id='job-followup')
            await app.topup_info(7, app.db.task(followup['task_id']), 'like')
            like_draft = app.db.draft(7)
            assert like_draft is not None
            await app.submit_topup(7, app.db.task(followup['task_id']), 'like', like_draft['data']['wizard_token'])
            like_task = next(
                t for t in app.db.recent_tasks(7)
                if t['kind'] == 'commentlike'
            )
            assert like_task['target_count'] == 3
            assert json.loads(like_task['payload_json'])['settings']['delaySeconds'] == 2
        finally:
            app.db.close()

    asyncio.run(run())


def test_account_console_reply_like_sends_parent_id_and_returns_to_replies():
    async def run():
        app = new_app()
        vid = '7321000000000000001'
        parent_cid = '7321000000000000100'
        reply_cid = '7321000000000000200'
        calls = []

        async def api_call(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if path.endswith('/replies'):
                return {
                    'items': [{'id': reply_cid, 'videoId': vid, 'text': 'Reply body', 'liked': False}],
                    'total': 1,
                }
            if path.endswith('/like'):
                return {'videoId': vid, 'commentId': reply_cid, 'requestedLiked': True}
            raise AssertionError((method, path, kwargs))

        app.api.call = api_call
        try:
            await app.account_video_comments(99, 'acc7', vid, 0, cid=parent_cid)
            like_btn = next(
                b['callback_data']
                for row in app.tg.out[-1][2]['inline_keyboard']
                for b in row
                if 'Лайк 1' in b['text']
            )
            await app.callback(99, {'data': like_btn})
            await app.callback(99, {'data': button_callback(app, 'ac:cancel_like:')})
            assert calls[-1][1] == f'/v1/accounts/acc7/videos/{vid}/comments/{parent_cid}/replies'

            like_btn = next(
                b['callback_data']
                for row in app.tg.out[-1][2]['inline_keyboard']
                for b in row
                if 'Лайк 1' in b['text']
            )
            await app.callback(99, {'data': like_btn})
            await app.callback(99, {'data': button_callback(app, 'ac:apply_like:')})
            like_call = next(c for c in calls if c[0] == 'PUT')
            assert like_call[1] == f'/v1/accounts/acc7/videos/{vid}/comments/{reply_cid}/like'
            assert like_call[2]['payload'] == {'liked': True, 'parentId': parent_cid}
            assert any('Обновить ответы' in b['text']
                       for row in app.tg.out[-1][2]['inline_keyboard'] for b in row)
            # Clicking the refresh button navigates back into replies rather than top-level comments.
            refresh_cb = app.tg.out[-1][2]['inline_keyboard'][0][0]['callback_data']
            await app.callback(99, {'data': refresh_cb})
            assert calls[-1][1] == f'/v1/accounts/acc7/videos/{vid}/comments/{parent_cid}/replies'
            assert await app.account_console_callback(99, f'ac:like:acc7:{vid}:{reply_cid}:1:not-an-id') is True
        finally:
            app.db.close()

    asyncio.run(run())


def test_export_file_handles_long_workspace_task_id_on_empty_and_api_error():
    async def run():
        app = new_app()
        long_tid = 'ws_' + 'a' * 55  # len('exports:' + long_tid) == 66 > 64 bytes

        async def fetch_task(tid):
            assert tid == long_tid
            return {'id': long_tid, 'kind': 'fetch', 'state': 'completed', 'name': 'Сбор'}

        async def empty_entries(method, path, **kwargs):
            return {'items': [], 'more': False}

        app.api.task = fetch_task
        app.api.call = empty_entries
        try:
            await app.export_file(99, long_tid, 'csv')
            assert 'Пока нет данных для этой выгрузки.' in app.tg.out[-1][1]
            cb = app.tg.out[-1][2]['inline_keyboard'][0][0]['callback_data']
            assert len(cb.encode('utf-8')) <= 64
            assert cb.startswith('route:')

            async def failed_entries(method, path, **kwargs):
                raise ApiError('upstream_failed')

            app.api.call = failed_entries
            await app.export_file(99, long_tid, 'csv')
            assert 'Выгрузка не завершилась.' in app.tg.out[-1][1]
            cb2 = app.tg.out[-1][2]['inline_keyboard'][0][0]['callback_data']
            assert len(cb2.encode('utf-8')) <= 64
            assert cb2.startswith('route:')
        finally:
            app.db.close()

    asyncio.run(run())
