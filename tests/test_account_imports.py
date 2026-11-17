import asyncio
import io
import json
import sys
from zipfile import ZipFile

import pytest

from proxima_bot.api import ApiError
from proxima_bot.db import Store
from proxima_bot.imports import import_and_assign, parse_account_file
from proxima_bot.manage import main


def account(value):
    return {'cookies': [{'name': 'sessionid', 'value': value}]}


def test_zip_keeps_account_boundaries_in_json_and_txt():
    stream = io.BytesIO()
    with ZipFile(stream, 'w') as archive:
        archive.writestr('accounts.json', json.dumps({'accounts': [account('one'), account('two')]}))
        archive.writestr('headers.txt', 'Cookie: sessionid=three; test=value\nsessionid=four\n')
        archive.writestr('netscape.txt', '# Netscape HTTP Cookie File\n'
                         '#HttpOnly_.tiktok.com\tTRUE\t/\tTRUE\t0\tsessionid\tfive\n'
                         '.tiktok.com\tTRUE\t/\tFALSE\t0\tother\t\n')
    parsed = parse_account_file(stream.getvalue(), '.zip')
    assert [entry['cookies'][0]['value'] for entry in parsed] == ['one', 'two', 'three', 'four', 'five']
    assert parsed[-1]['cookies'][0]['httpOnly'] is True
    assert parsed[-1]['cookies'][1]['value'] == ''


def test_plain_json_cookie_array_is_one_account():
    cookies = [{'name': 'sessionid', 'value': 'one', 'expirationDate': 1800000000.5},
               {'name': 'other', 'value': 'two'}]
    parsed = parse_account_file(json.dumps(cookies).encode(), '.json')
    assert len(parsed) == 1 and len(parsed[0]['cookies']) == 2
    assert parsed[0]['cookies'][0]['expirationDate'] == 1800000000


def test_malformed_file_is_rejected_before_any_import():
    with pytest.raises(ValueError, match='№2'):
        parse_account_file(json.dumps({'accounts': [account('ok'), {'cookies': []}]}).encode(), '.json')
    with pytest.raises(ValueError, match='Netscape'):
        parse_account_file(b'.tiktok.com\tTRUE\t/\tTRUE\tbad\tsessionid\tvalue', '.txt')


def test_assignment_only_uses_verified_ids_and_never_transfers_other_users_accounts():
    async def run():
        imported=[]

        class API:
            async def call(self, method, path, *, payload, retries, background):
                assert method=='POST' and retries==0
                assert background
                value = payload['cookies'][0]['value']
                if path=='/v1/accounts/import/check':
                    if value=='unknown':
                        return {'exists':None,'accountId':None,'uid':None,'hidden':False}
                    exists=value=='other' or value in imported
                    return {'exists':exists,'accountId':value if exists else None,'uid':'123','hidden':False}
                assert path=='/v1/accounts/import'
                imported.append(value)
                if value == 'invalid':
                    raise ApiError('invalid_cookies', 400)
                if value == 'missing-id':
                    return {'account': {}}
                return {'account': {'accountId': value}, 'ready': value != 'waiting'}

        db = Store(':memory:')
        db.assign(8, 'other')
        try:
            summary = await import_and_assign(API(), db, 7, [
                account(value) for value in ('ok', 'ok', 'waiting', 'other', 'invalid', 'missing-id', 'unknown')
            ])
            assert (summary.total, summary.assigned, summary.pending, summary.duplicates,
                    summary.conflicts, summary.rejected, summary.unverified) == (7, 2, 1, 1, 1, 2, 1)
            assert imported==['ok','waiting','invalid','missing-id']
            assert db.account_ids(7) == ['ok', 'waiting']
            assert db.account_ids(8) == ['other']
        finally:
            db.close()

    asyncio.run(run())


def test_rate_limit_waits_and_retries_only_the_throttled_record(monkeypatch):
    async def run():
        calls = []
        sleeps = []

        class API:
            async def call(self, method, path, *, payload, retries, background):
                value = payload['cookies'][0]['value']
                calls.append((path,value))
                if len(calls)==1:
                    raise ApiError('rate_limited', 429, 3)
                if value == 'second':
                    raise ApiError('api_key_invalid', 401)
                if path=='/v1/accounts/import/check':
                    return {'exists':False,'accountId':None,'uid':'123','hidden':False}
                return {'account': {'accountId': value}, 'ready': True}

        async def sleep(delay):
            sleeps.append(delay)

        monkeypatch.setattr('proxima_bot.imports.asyncio.sleep', sleep)
        db = Store(':memory:')
        try:
            summary = await import_and_assign(API(), db, 7, [account(v) for v in ('first', 'second', 'third')])
            assert sleeps == [3] and calls == [('/v1/accounts/import/check','first'),('/v1/accounts/import/check','first'),
                                               ('/v1/accounts/import','first'),('/v1/accounts/import/check','second')]
            assert summary.stopped == 'HTTP 401' and summary.attempted == 2
            assert db.account_ids(7) == ['first']
        finally:
            db.close()

    asyncio.run(run())


def test_cli_assigns_all_valid_accounts_from_one_file(monkeypatch, tmp_path, capsys):
    source = tmp_path / 'accounts.txt'
    source.write_text('sessionid=first\nsessionid=second\n', encoding='utf-8')
    database = tmp_path / 'bot.sqlite3'

    class API:
        def __init__(self, *args):
            pass

        async def call(self, method, path, *, payload=None, retries, background=False, params=None):
            if method=='GET' and path=='/v1/tasks':
                return {'items':[]}
            if method=='GET' and path=='/v1/accounts/check':
                return {'state':'idle'}
            if path=='/v1/accounts/import/check':
                return {'exists':False,'accountId':None,'uid':'123','hidden':False}
            return {'account': {'accountId': payload['cookies'][0]['value']}, 'ready': True}

    monkeypatch.setattr('proxima_bot.manage.Tokora', API)
    monkeypatch.setattr('proxima_bot.manage.tokora_credentials', lambda: ('https://example.invalid', 'test'))
    monkeypatch.setenv('DATABASE_PATH', str(database))
    monkeypatch.setattr(sys, 'argv', ['manage', 'import-accounts-file', str(source), '--user-id', '7'])
    asyncio.run(main())
    db = Store(str(database))
    try:
        assert db.account_ids(7) == ['first', 'second']
        output = capsys.readouterr().out
        assert 'Закреплено за участником: 2.' in output and 'sessionid' not in output
    finally:
        db.close()
