import asyncio
import json
from types import SimpleNamespace

import pytest

from proxima_bot import api as api_module
from proxima_bot.api import ApiError, Tokora
from proxima_bot.catalog import (
    build_payload,
    parse_comment_links,
    parse_feedwarm,
    parse_options,
    parse_targets,
    resolve_payload_targets,
    validate_feedwarm,
    validate_task_options,
)


class Provider:
    def __init__(self):
        self.calls = []

    async def resolve(self, link):
        self.calls.append(link)
        return '7420000000000000000'


@pytest.mark.parametrize('link', [
    'http://127.0.0.1/private',
    'file:///etc/passwd',
    'https://www.tiktok.com.example.invalid/video/7420000000000000000',
    'https://www.tiktok.com@example.invalid/video/7420000000000000000',
    'https://attacker@www.tiktok.com/video/7420000000000000000',
    'https://www.tiktok.com:81/video/7420000000000000000',
    'https://www.tiktok.com/\\example.invalid',
])
def test_all_member_link_inputs_reject_untrusted_urls(link):
    with pytest.raises(ValueError):
        parse_targets(link)
    with pytest.raises(ValueError):
        parse_comment_links(link)
    with pytest.raises(ValueError, match='sampleLinks'):
        parse_feedwarm(json.dumps({'sampleLinks': [link]}))


def test_documented_link_and_comment_id_formats_are_preserved():
    short = 'https://vm.tiktok.com/ZMabc123/'
    full = 'https://www.tiktok.com/@author/video/7420000000000000000'
    pair = '7683098288987360530:7683468183685186311'
    video_and_comment = full + ' 7683468183685186311'
    assert parse_targets(f'{full}\n{short}\n{full}') == [full, short]
    assert parse_comment_links(f'{pair}\n{video_and_comment}\n{short}') == [pair, video_and_comment, short]
    assert parse_feedwarm(json.dumps({'sampleLinks': [short]})) == {'sampleLinks': [short]}


def test_local_task_limits_do_not_change_general_owner_settings():
    settings = parse_options('{"workers":100000}')
    assert settings == {'workers': 100000}
    with pytest.raises(ValueError, match='workers'):
        validate_task_options(settings)
    assert validate_task_options({'workers': 20, 'uniqueText': False}) == {
        'workers': 20, 'uniqueText': False,
    }
    with pytest.raises(ValueError, match='searchLimit'):
        parse_feedwarm('{"searchLimit":1000000000}')
    with pytest.raises(ValueError, match='назначенных'):
        validate_feedwarm({'accounts': 2}, account_count=1)


def test_keyword_reply_targets_cannot_be_changed_in_extra_options():
    async def run():
        api = Provider()
        draft = {'name': 'Тест', 'videos': ['7420000000000000000'],
                 'keywords': ['выбранная фраза'], 'replies': ['Ответ']}
        with pytest.raises(ValueError, match='fetchKeywords'):
            await build_payload(api, 'keyword_reply', {**draft, 'options': {
                'fetchKeywords': ['другая фраза'],
            }}, ['acc'])
        payload = await build_payload(api, 'keyword_reply', draft, ['acc'])
        assert payload['settings']['fetchKeywords'] == ['выбранная фраза']

    asyncio.run(run())


@pytest.mark.parametrize(('disabled', 'comments', 'replies'), [
    ('comments', [], ['Ответ']),
    ('replies', ['Комментарий'], []),
])
def test_publish_payload_drops_stale_texts_from_disabled_blocks(disabled, comments, replies):
    draft = {'name': 'Старый черновик', 'videos': ['7420000000000000000'],
             'comments': ['Комментарий'], 'replies': ['Ответ'], 'disabled_fields': [disabled]}
    payload = asyncio.run(build_payload(Provider(), 'publish', draft, ['acc'], defer_resolution=True))
    assert payload['assets'] == {'accountIds': ['acc'], 'commentTexts': comments, 'replyTexts': replies}


@pytest.mark.parametrize('mode', ['reply_links', 'keyword_reply'])
@pytest.mark.parametrize('material', [{}, {'replies': ['Старый текст'], 'disabled_fields': ['replies']}])
def test_reply_order_requires_enabled_texts_before_target_resolution(mode, material):
    provider = Provider()
    draft = {'name': 'Старый черновик', 'videos': ['https://vt.tiktok.com/example/'],
             'reply_links': ['https://vt.tiktok.com/example/'], 'keywords': ['Фраза'], **material}
    with pytest.raises(ValueError, match='Добавь тексты ответов'):
        asyncio.run(build_payload(provider, mode, draft, ['acc']))
    assert not provider.calls


def test_video_id_in_query_is_not_used_as_the_actual_target():
    async def run():
        api = Provider()
        link = 'https://vm.tiktok.com/ZMabc123/?redirect=/video/7400000000000000000'
        result = await build_payload(api, 'fetch', {'name': 'Тест', 'videos': [link]}, ['acc'])
        assert api.calls == [link]
        assert result['awemeIds'] == ['7420000000000000000']

    asyncio.run(run())


def test_old_feedwarm_draft_is_revalidated_before_dispatch():
    async def run():
        api = Provider()
        with pytest.raises(ValueError, match='sampleLinks'):
            await build_payload(api, 'feedwarm', {
                'name': 'Тест', 'warm_json': {'sampleLinks': ['http://127.0.0.1/private']},
            }, ['acc'])
        assert not api.calls

    asyncio.run(run())


def test_rate_limited_video_batch_reuses_previous_valid_resolutions(monkeypatch):
    links = [f'https://vt.tiktok.com/link-{index}/' for index in range(3)]
    ids = [str(7420000000000000000 + index) for index in range(3)]
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(api_module, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(api_module, 'TOKORA_REQUESTS_PER_MINUTE', 2)

    async def run():
        api = Tokora(None, 'https://example.invalid', 'unused')
        calls = []

        async def call(method, path, *, payload, retries):
            assert (method, path) == ('POST', '/v1/links/resolve')
            await api.reserve_request()
            target = payload['link']
            calls.append(target)
            return {'videoId': ids[links.index(target)]}

        api.call = call
        order = {'awemeIds': links}
        with pytest.raises(ApiError, match='rate_limited'):
            await resolve_payload_targets(api, order)
        assert calls == links[:2]
        clock.now = 61
        assert await resolve_payload_targets(api, order) == {'awemeIds': ids}
        assert calls == links

        clock.now += api_module.VIDEO_RESOLUTION_CACHE_TTL
        assert await api.resolve(links[0]) == ids[0]
        assert calls == [*links, links[0]]

    asyncio.run(run())
