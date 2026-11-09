import asyncio
import json

import pytest

from proxima_bot.catalog import (
    build_payload,
    parse_comment_links,
    parse_feedwarm,
    parse_options,
    parse_targets,
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
        draft = {'name': 'Тест', 'keywords': ['выбранная фраза'], 'replies': ['Ответ']}
        with pytest.raises(ValueError, match='fetchKeywords'):
            await build_payload(api, 'keyword_reply', {**draft, 'options': {
                'fetchKeywords': ['другая фраза'],
            }}, ['acc'])
        payload = await build_payload(api, 'keyword_reply', draft, ['acc'])
        assert payload['settings']['fetchKeywords'] == ['выбранная фраза']

    asyncio.run(run())


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
