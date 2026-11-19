import asyncio
import time
from proxima_bot.advanced_db import ExtendedStore
from proxima_bot.api import ApiError
from proxima_bot.config import Config
from proxima_bot.extended import ProximaApp
from proxima_bot.bot import BotApp, MAIN_MENU_TEXT


class MockTG:
    def __init__(self):
        self.out = []
    async def send(self, uid, text, markup=None, **kw):
        self.out.append((uid, text, markup))
    async def rich(self, uid, rich, plain, markup=None):
        self.out.append((uid, rich, markup))


class MockAPI:
    def __init__(self, stats_payload=None, accounts_payload=None, fail=False):
        self.stats_payload = stats_payload
        self.accounts_payload = accounts_payload
        self.fail = fail
        self.calls = []

    async def stats(self):
        self.calls.append('stats')
        if self.fail:
            raise ApiError('connection_unavailable', 503)
        return self.stats_payload or {}

    async def account_page(self, offset=0, limit=200, **kwargs):
        self.calls.append(('account_page', offset, limit, kwargs))
        if self.fail:
            raise ApiError('connection_unavailable', 503)
        return self.accounts_payload or {'accounts': [], 'total': 0}


def test_main_menu_formatting_with_exact_user_numbers():
    db = ExtendedStore(':memory:')
    app = BotApp(Config('token', 'key', 99), db, MockTG(), MockAPI())
    metrics = {
        'total_accounts': 359,
        'available_accounts': 147,
        'comments_today': 60,
        'replies_today': 0,
        'moderation_today': 1,
        'comments_day': 60,
        'replies_day': 2,
        'moderation_day': 26,
        'comments_week': 60,
        'replies_week': 2,
        'moderation_week': 26,
        'total_published': 62,
    }
    text = app.format_main_menu(metrics)
    assert '<b>🏠 Главное меню</b>' in text
    assert 'Управляйте аккаунтами и прокси, собирайте комментарии и запускайте задачи с ответами.' in text
    assert 'Здесь — актуальная статистика публикаций и доступ к основным разделам бота.' in text
    assert '└ Аккаунты: 359 · доступно 147' in text
    assert '<b>Комментарии / Ответы / Модерация</b>' in text
    assert '├ Сегодня: 60 / 0 / 1' in text
    assert '├ 24 часа: 60 / 2 / 26' in text
    assert '├ 7 дней: 60 / 2 / 26' in text
    assert '└ Всего опубликовано: 62' in text
    db.close()


def test_main_menu_data_extraction_from_upstream_payload():
    async def run():
        db = ExtendedStore(':memory:')
        stats_payload = {
            'activeTaskCount': 0,
            'today': {'comments': 60, 'replies': 0, 'moderation': 1, 'attempts': 61},
            'day': {'comments': 60, 'replies': 2, 'moderation': 26, 'attempts': 88},
            'week': {'comments': 60, 'replies': 2, 'moderation': 26, 'attempts': 88},
            'all': {'comments': 60, 'replies': 2, 'moderation': 26, 'attempts': 88},
        }
        accounts_payload = {
            'accounts': [],
            'total': 359,
            'counts': {
                'total': 359,
                'active': 147,
                'expired': 12,
                'error': 5,
            }
        }
        api = MockAPI(stats_payload=stats_payload, accounts_payload=accounts_payload)
        app = ProximaApp(Config('token', 'key', 99), db, MockTG(), api)

        metrics = await app.get_main_menu_data()
        assert metrics['total_accounts'] == 359
        assert metrics['available_accounts'] == 147
        assert metrics['comments_today'] == 60
        assert metrics['replies_today'] == 0
        assert metrics['moderation_today'] == 1
        assert metrics['comments_day'] == 60
        assert metrics['replies_day'] == 2
        assert metrics['moderation_day'] == 26
        assert metrics['comments_week'] == 60
        assert metrics['replies_week'] == 2
        assert metrics['moderation_week'] == 26
        assert metrics['total_published'] == 62

        await app.show_home(99)
        assert len(app.tg.out) == 1
        sent_text = app.tg.out[0][1]
        assert '<h2>🏠 Главное меню</h2>' in sent_text
        assert 'Аккаунты · <b>359</b> · доступно <b>147</b>' in sent_text
        assert '<b>60</b> комментариев · <b>0</b> ответов' in sent_text
        assert 'На модерации · 1' in sent_text
        assert '<td>24 часа</td><td>60</td><td>2</td><td>26</td>' in sent_text
        assert '<td>7 дней</td><td>60</td><td>2</td><td>26</td>' in sent_text
        assert 'Всего опубликовано · <b>62</b>' in sent_text

        # Verify caching: second call does not invoke API methods again
        call_count_before = len(api.calls)
        metrics2 = await app.get_main_menu_data()
        assert len(api.calls) == call_count_before
        assert metrics2 == metrics

        db.close()
    asyncio.run(run())


def test_main_menu_fallback_on_api_error():
    async def run():
        db = ExtendedStore(':memory:')
        db.assign(7, 'acc-user-1')
        db.assign(7, 'acc-user-2')
        api = MockAPI(fail=True)
        app = ProximaApp(Config('token', 'key', 99), db, MockTG(), api)

        # Even with failed API, get_main_menu_data should not raise
        metrics = await app.get_main_menu_data(uid=7)
        assert metrics['total_accounts'] == 2
        assert metrics['available_accounts'] is None
        assert 'доступно —' in app.format_main_menu(metrics)
        assert metrics['comments_today'] == 0
        assert metrics['total_published'] == 0

        await app.show_home(7)
        assert len(app.tg.out) == 1
        text = app.tg.out[0][1]
        assert '<h2>🏠 Главное меню</h2>' in text
        assert 'Аккаунты · <b>2</b> · доступно <b>—</b>' in text
        assert '<b>0</b> комментариев · <b>0</b> ответов' in text
        assert 'Всего опубликовано · <b>0</b>' in text

        db.close()
    asyncio.run(run())


def test_main_menu_triggers_on_texts():
    async def run():
        db = ExtendedStore(':memory:')
        app = ProximaApp(Config('token', 'key', 99), db, MockTG(), MockAPI())
        for trigger in (MAIN_MENU_TEXT, 'Главное меню', '⌂ Главное меню', '🏠 Главное меню'):
            update = {
                'message': {
                    'chat': {'type': 'private'},
                    'from': {'id': 99},
                    'text': trigger
                }
            }
            await app.handle(update)
            assert '<h2>🏠 Главное меню</h2>' in app.tg.out[-1][1]
        db.close()
    asyncio.run(run())


def test_member_home_stats_are_isolated_from_workspace_and_owner():
    async def run():
        db = ExtendedStore(':memory:')
        try:
            db.grant(7, 30)
            db.grant(8, 30)
            db.assign(7, 'a7')
            db.assign(8, 'a8-1')
            db.assign(8, 'a8-2')
            t7 = db.queue_task(7, 'Свой заказ', 'publish', {'assets': {'accountIds': ['a7']}})
            db.transition(t7, 'completed', snapshot={'awemes': [{'comments': 5, 'replies': 2}], 'published': 7})
            t8 = db.queue_task(8, 'Чужой заказ', 'publish', {'assets': {'accountIds': ['a8-1']}})
            db.transition(t8, 'completed', snapshot={'awemes': [{'comments': 999, 'replies': 0}], 'published': 999})

            cfg = Config('bot', 'tok', 99, dispatch_enabled=True)
            api = MockAPI(
                stats_payload={
                    'accounts': {'total': 50, 'active': 40},
                    'tasks': {'active': 12},
                    'today': {'comments': 777, 'replies': 333, 'likes': 111},
                },
                accounts_payload={'accounts': [{'accountId': 'a7', 'status': 'active'}], 'total': 50},
            )
            app = ProximaApp(cfg, db, MockTG(), api)
            member_data = await app.get_main_menu_data(7)
            assert member_data['total_accounts'] == 1
            assert member_data['available_accounts'] == 1
            assert member_data['comments_today'] == 5
            assert member_data['replies_today'] == 2
            assert member_data['total_published'] == 7

            owner_data = await app.get_main_menu_data(99)
            assert owner_data['total_accounts'] == 50
            assert owner_data['comments_today'] == 777
        finally:
            db.close()

    asyncio.run(run())


def test_daily_quota_resets_at_configured_timezone_midnight(monkeypatch):
    from datetime import datetime, timezone

    # 2026-06-01 20:30 UTC is 23:30 in Europe/Moscow (+3) vs 01:30 next day in Asia/Almaty (+5)
    t_before = int(datetime(2026, 6, 1, 20, 30, tzinfo=timezone.utc).timestamp())
    t_after = int(datetime(2026, 6, 1, 21, 5, tzinfo=timezone.utc).timestamp())  # 00:05 MSK!
    clock = [t_before]
    monkeypatch.setattr('proxima_bot.db.now', lambda: clock[0])

    db = ExtendedStore(':memory:', timezone_name='Europe/Moscow')
    try:
        db.grant(7, 30)
        db.change_limit(7, 99, 'daily', 1)
        db.assign(7, 'a7')
        tid = db.queue_task(7, 'Дневной 1', 'publish', {'assets': {'accountIds': ['a7']}})
        db.transition(tid, 'completed')
        try:
            db.queue_task(7, 'Дневной 2', 'publish', {'assets': {'accountIds': ['a7']}})
            assert False, 'Expected daily quota limit before MSK midnight'
        except ValueError:
            pass

        # 21:05 UTC is 00:05 MSK next day -> quota resets even though UTC date didn't change yet!
        clock[0] = t_after
        tid2 = db.queue_task(7, 'После полуночи МСК', 'publish', {'assets': {'accountIds': ['a7']}})
        assert tid2
    finally:
        db.close()
