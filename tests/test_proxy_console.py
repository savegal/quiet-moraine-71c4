import asyncio
import json
from types import SimpleNamespace

import pytest

from proxima_bot.db import Store
from proxima_bot.manage import open_store, print_proxy_import, run_cli_operation
from proxima_bot.proxy_console import PROXY_CHECK, PROXY_LIST, ensure_proxies_unbound

from test_extended import button_callback, new_app
from test_release_flows import TelegramTransport


class ProxyApi:
    def __init__(self):
        self.items = [
            {'id':f'px{i}', 'address':f'socks5://user:secret@private{i}.invalid:8080',
             'status':'valid' if i%2==0 else 'failed', 'latencyMs':30+i, 'countryCode':'il',
             'error':'socks5://user:secret@hidden.invalid:8080', 'egressIp':'192.0.2.1',
             'hasCredentials':True, 'checkedAt':1_700_000_000_000}
            for i in range(10)
        ]
        self.items.append({'id':'managed_px','address':'','status':'unknown','hasCredentials':False})
        self.check = {'state':'idle','total':0,'checked':0}
        self.account_check = {'state':'idle'}
        self.active = []
        self.posts = 0
        self.accounts = {
            'acc7': {'accountId':'acc7','proxy':'private0.invalid:8080','status':'active'},
            'acc8': {'accountId':'acc8','proxy':'private1.invalid:8080','status':'active'},
        }
        self.calls = []

    @property
    def writes(self):
        return [(method,path,body) for method,path,body in self.calls if method!='GET']

    async def active_tasks(self):
        self.calls.append(('GET','/v1/tasks',None))
        return self.active

    async def account_page(self, offset, limit=200):
        self.calls.append(('GET','/v1/accounts',{'offset':offset,'limit':limit}))
        accounts = list(self.accounts.values())
        return {'accounts':accounts[offset:offset+limit],'total':len(accounts)}

    async def call(self, method, path, *, payload=None, **kwargs):
        self.calls.append((method,path,payload))
        if method=='GET':
            if path==PROXY_LIST:
                return {'items':self.items,'ownOnly':False}
            if path==PROXY_CHECK:
                return self.check
            if path=='/v1/proxies/balance':
                return {'hasPackage':True,'status':'low','remainingBytes':25_000_000,
                        'usedBytes':975_000_000,'limitBytes':1_000_000_000}
            if path=='/v1/accounts/check':
                return self.account_check
            if path=='/v1/posts/queue':
                return {'active':self.posts}
            if path.startswith('/v1/accounts/'):
                return self.accounts[path.rsplit('/',1)[-1]]
        if method=='POST' and path==PROXY_CHECK:
            self.check = {'state':'running','total':len(payload.get('ids',self.items)),
                          'checked':0,'startedAt':1_700_000_000_000}
            return self.check
        if method=='DELETE' and path==PROXY_CHECK:
            self.check = {'state':'stopped','total':2,'checked':1}
            return {}
        if method=='DELETE' and path==PROXY_LIST:
            return {}
        if method=='PUT' and path.endswith('/proxy'):
            aid = path.split('/')[3]
            return {**self.accounts[aid],'proxy':payload.get('proxyId')}
        if method=='PATCH' and path.startswith('/v1/proxies/'):
            return {'id':path.rsplit('/',1)[-1]}
        raise AssertionError((method,path))


async def click(app, prefix, uid=99):
    data = button_callback(app,prefix)
    await app.callback(uid,{'data':data})
    return data


def test_proxy_inventory_never_shares_credentials_and_restricts_member_reads():
    async def run():
        app = new_app()
        app.api = ProxyApi()
        try:
            await app.proxies(7)
            member_text = app.tg.out[-1][1]
            assert 'С привязкой прокси</td><td>1' in member_text
            assert 'private' not in member_text and 'px0' not in member_text
            assert all(path!=PROXY_LIST for _,path,_ in app.api.calls)
            before = app.api.calls.copy()
            await app.callback(7,{'data':'px:item:px0'})
            await app.callback(7,{'data':'px:check_one:px0'})
            assert app.api.calls==before

            await app.proxies(99)
            await click(app,'px:item:px0')
            card = app.tg.out[-1][1]
            assert all(value in card for value in ('px0','Свой','IL','30 мс'))
            assert all(value not in card for value in ('secret','private','192.0.2.1','hidden.invalid'))
            await app.callback(99,{'data':'px:list:all:1'})
            await click(app,'px:item:managed_px')
            assert 'Адрес скрыт' in app.tg.out[-1][1]
            assert 'px:delete:managed_px' not in str(app.tg.out[-1][2])
            await app.callback(99,{'data':'px:list:failed:0'})
            assert 'px:item:px1' in str(app.tg.out[-1][2]) and 'px:item:px0' not in str(app.tg.out[-1][2])
        finally:
            app.db.close()
    asyncio.run(run())


def test_proxy_balance_and_saved_binding_id():
    async def run():
        app=new_app()
        app.api=ProxyApi()
        try:
            await app.callback(7,{'data':'px:balance'})
            assert not app.api.calls
            await app.callback(99,{'data':'px:balance'})
            assert '25,00 МБ' in app.tg.out[-1][1] and 'Заканчивается' in app.tg.out[-1][1]
            app.api.accounts={
                'acc7':{'accountId':'acc7','proxy':None,'proxyId':None,'proxySource':'tokora'},
                'acc8':{'accountId':'acc8','proxy':None,'proxyId':'px1'},
            }
            inventory={item['id']:item for item in app.api.items}
            await ensure_proxies_unbound(app.api,inventory,['px0'])
            with pytest.raises(ValueError,match='acc8'):
                await ensure_proxies_unbound(app.api,inventory,['px1'])
        finally:
            app.db.close()

    asyncio.run(run())


def test_selected_proxy_check_uses_ids_and_stop_does_not_apply_to_replacement_check():
    async def run():
        app = new_app()
        app.api = ProxyApi()
        try:
            await app.callback(99,{'data':'px:select'})
            token = app.db.draft(99)['data']['token']
            await click(app,f'px:toggle:{token}:all:0:px0')
            await click(app,f'px:selection:{token}:all:1')
            await click(app,f'px:toggle:{token}:all:1:px8')
            await click(app,f'px:check_selected:{token}')
            assert app.api.writes==[('POST',PROXY_CHECK,{'ids':['px0','px8']})]
            assert 'В работе' in app.tg.out[-1][1]
            await click(app,'px:stop')
            stale = button_callback(app,'px:confirm:')
            app.api.check['startedAt'] += 1
            await app.callback(99,{'data':stale})
            assert len(app.api.writes)==1 and 'другая проверка' in app.tg.out[-1][1]
            stamp = app.api.check.pop('startedAt')
            await app.callback(99,{'data':'px:stop'})
            assert app.db.draft(99) is None and 'открой кабинет' in app.tg.out[-1][1]
            app.api.check['startedAt'] = stamp
            task_id = app.db.queue_task(7,'Сбор','fetch',{'assets':{'accountIds':['acc7']}})
            app.db.transition(task_id,'running',upstream_id='job1')
            await app.callback(99,{'data':'px:stop'})
            confirm = await click(app,'px:confirm:')
            await app.callback(99,{'data':confirm})
            assert app.api.writes[-1]==('DELETE',PROXY_CHECK,None) and len(app.api.writes)==2
        finally:
            app.db.close()
    asyncio.run(run())


def test_proxy_progress_edits_existing_card_and_finishes():
    async def run():
        app = new_app()
        app.api = ProxyApi()
        app.tg = TelegramTransport()
        try:
            await app.callback(99,{'data':'px:check'})
            assert app.api.writes==[('POST',PROXY_CHECK,{})]
            card = app.live_cards.cards[99]
            app.api.check.update({'checked':2,'valid':1,'errors':{'socks5://secret@private.invalid':1}})
            await app.live_cards.tick()
            method, body = app.tg.calls[-1]
            assert method=='editMessageText' and body['message_id']==card.message_id
            assert '2 / 11' in body['rich_message']['html'] and 'secret' not in body['rich_message']['html']
            app.api.check.update({'state':'completed','checked':11})
            await app.live_cards.tick()
            assert 'Завершена' in app.tg.calls[-1][1]['rich_message']['html'] and 99 not in app.live_cards.cards
        finally:
            app.db.close()
    asyncio.run(run())


def test_proxy_deletion_checks_all_account_pages_and_upstream_activity():
    async def run():
        app = new_app()
        app.api = ProxyApi()
        app.api.accounts = {f'a{i}':{'accountId':f'a{i}','proxy':None} for i in range(201)}
        app.api.accounts['a200']['proxy'] = 'http://other:secret@private0.invalid:8080'
        try:
            await app.callback(99,{'data':'px:delete:px0'})
            await click(app,'px:confirm:')
            assert not app.api.writes and 'привязан к аккаунту' in app.tg.out[-1][1]
            offsets = [body['offset'] for _,path,body in app.api.calls if path=='/v1/accounts']
            assert offsets==[0,200]

            app.api.accounts['a200']['proxy'] = None
            app.api.posts = 1
            await app.callback(99,{'data':'px:delete:px0'})
            await click(app,'px:confirm:')
            assert not app.api.writes and 'активных публикаций' in app.tg.out[-1][1]

            app.api.posts = 0
            await app.callback(99,{'data':'px:delete:px0'})
            await click(app,'px:confirm:')
            assert app.api.writes==[('DELETE',PROXY_LIST,{'ids':['px0']})]
            assert not app.db.workspace_action_active()
        finally:
            app.db.close()
    asyncio.run(run())


def test_proxy_binding_revalidates_assignee_and_uses_exact_put_schema():
    async def run():
        app = new_app()
        app.api = ProxyApi()
        try:
            await app.callback(99,{'data':'px:bind:acc7'})
            await click(app,'px:bind_to:acc7:px0')
            app.db.assign(8,'acc7')
            await click(app,'px:confirm:')
            assert not app.api.writes and 'Назначение аккаунта изменилось' in app.tg.out[-1][1]

            await app.callback(99,{'data':'px:bind:acc7'})
            await click(app,'px:bind_to:acc7:px0')
            await click(app,'px:confirm:')
            assert app.api.writes==[('PUT','/v1/accounts/acc7/proxy',{'proxyId':'px0'})]

            await app.callback(99,{'data':'px:bind:acc7'})
            await click(app,'px:unbind:acc7')
            await click(app,'px:confirm:')
            assert app.api.writes[-1]==('PUT','/v1/accounts/acc7/proxy',{'proxy':None})
        finally:
            app.db.close()
    asyncio.run(run())


def test_raw_cli_proxy_patch_obeys_shared_db_maintenance_guard(tmp_path,monkeypatch):
    async def run():
        db_path = str(tmp_path/'bot.sqlite3')
        monkeypatch.setenv('DATABASE_PATH',db_path)
        spec_args = tmp_path/'proxy.json'
        spec_args.write_text(json.dumps({'path':{'id':'px0'},'body':{'proxy':'http://user:secret@host:1234'}}),
                             encoding='utf-8')
        args = SimpleNamespace(operation_id='patchProxiesById',json_file=spec_args,upload=None,output=None)
        api = ProxyApi()
        db = Store(db_path)
        try:
            db.set_account_check_pending(True)
            with pytest.raises(ValueError,match='Проверка аккаунтов'):
                await run_cli_operation(api,args)
            assert api.calls==[]
            db.set_account_check_pending(False)
            await run_cli_operation(api,args)
            assert api.writes==[('PATCH','/v1/proxies/px0',{'proxy':'http://user:secret@host:1234'})]
        finally:
            db.close()
    asyncio.run(run())


def test_proxy_import_cli_prints_only_counters_and_sync_state(capsys):
    print_proxy_import({'items':[{'address':'http://user:secret@private.invalid:1234'}],
                        'import':{'received':5,'added':3,'duplicates':2,'total':6},
                        'engineSync':'pending'})
    text = capsys.readouterr().out
    assert 'Добавлено 3' in text and 'Повторы 2' in text and 'ожидает повтора' in text
    assert 'secret' not in text and 'private.invalid' not in text


def test_existing_cli_store_refuses_missing_or_unrelated_database(tmp_path,monkeypatch):
    path = tmp_path/'wrong.sqlite3'
    monkeypatch.setenv('DATABASE_PATH',str(path))
    with pytest.raises(ValueError,match='База бота не найдена'):
        open_store(existing=True,configured=True)
    assert not path.exists()
    path.touch()
    with pytest.raises(ValueError,match='нет таблиц Proxima Soft'):
        open_store(existing=True,configured=True)
