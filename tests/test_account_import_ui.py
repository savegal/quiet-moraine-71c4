import asyncio
from dataclasses import asdict
import json

import pytest

from proxima_bot.advanced_db import ExtendedStore
from proxima_bot.config import Config
from proxima_bot.extended import ProximaApp
from proxima_bot.imports import ImportSummary

from test_extended import FakeAPI, FakeTG, button_callback, new_app


def account_rows(*values):
    return '\n'.join(json.dumps([{'name':'sid_guard','value':value}]) for value in values).encode()


def document(file_id,size=100,uid=99):
    return {'message': {'chat': {'type':'private','id':uid}, 'from': {'id':uid},
                        'document': {'file_name':'accounts.txt','file_id':file_id,'file_size':size}}}


class TelegramWithEdits(FakeTG):
    async def rich(self,uid,rich,plain,markup=None):
        await super().rich(uid,rich,plain,markup)
        return {'message_id':len(self.out),'rich_message':True}

    async def edit_rich(self,uid,message_id,rich,markup):
        self.out.append((uid,rich,markup))


@pytest.mark.parametrize('target',[7,99])
def test_owner_imports_uploaded_files_to_selected_recipient(target):
    async def run():
        app=new_app()
        app.tg=TelegramWithEdits()
        files={'doc-1':account_rows('private-one','private-two'), 'doc-2':account_rows('private-three')}
        downloads=[]
        imports=[]

        async def fetch(file_id,limit):
            downloads.append(file_id)
            return files[file_id],'remote-file.txt'

        async def provider(method,path,**kwargs):
            if path=='/v1/accounts/import/check':
                return {'exists':False,'accountId':None,'uid':'123','hidden':False}
            assert (method,path)==('POST','/v1/accounts/import')
            imports.append(kwargs['payload']['cookies'][0]['value'])
            return {'account':{'accountId':f'new-account-{len(imports)}'},'ready':True}

        app.tg.fetch_upload=fetch
        app.api.call=provider
        await app.callback(7,{'data':f'acctimp:start:{target}'})
        assert app.db.draft(7) is None
        if target==99:await app.admin_dashboard(99)
        else:await app.admin_user(99,target)
        await app.callback(99,{'data':button_callback(app,f'acctimp:start:{target}')})
        await app.handle(document('doc-1'))
        stale=button_callback(app,'acctimp:apply:')
        await app.handle(document('doc-2'))
        latest=button_callback(app,'acctimp:apply:')
        await app.handle(document('doc-2'))
        assert button_callback(app,'acctimp:apply:')==latest
        assert 'Этот файл уже добавлен' in app.tg.out[-1][1]
        assert imports==[] and downloads==['doc-1','doc-2','doc-2']
        assert 'private-one' not in json.dumps(app.db.draft(99))
        await app.callback(7,{'data':latest})
        await app.callback(99,{'data':stale})
        assert imports==[]
        await app.callback(99,{'data':latest})
        await asyncio.wait_for(app.account_imports.task,timeout=2)
        job=app.db.account_import(99,target_id=target)
        assert job['state']=='finished'
        assert job['summary']['total']==job['summary']['assigned']==3
        assert imports==['private-one','private-two','private-three']
        assert all(f'new-account-{index}' in app.db.account_ids(target) for index in range(1,4))
        other=7 if target==99 else 99
        assert any(aid.startswith('new-account-') for aid in app.db.account_ids(other)) if target==7 else not any(aid.startswith('new-account-') for aid in app.db.account_ids(other))
        assert all('private-one' not in text for _,text,_ in app.tg.out)
        assert 'Итог импорта' in app.tg.out[-1][1]
        result=button_callback(app,'acctimp:result:')
        await app.callback(7,{'data':result})
        assert app.tg.documents==[]
        await app.callback(99,{'data':result})
        assert 'new-account-3 · готов'.encode() in app.tg.documents[-1][1]
        assert b'private-one' not in app.tg.documents[-1][1]
        await app.callback(99,{'data':latest})
        assert len(imports)==3
        await app.account_imports.close()
        app.db.close()

    asyncio.run(run())


def test_stop_finishes_current_account_and_leaves_the_rest_unprocessed():
    async def run():
        app=new_app()
        started=asyncio.Event()
        release=asyncio.Event()
        calls=[]

        async def fetch(file_id,limit):
            return account_rows('one','two'),'remote-file.txt'

        async def provider(method,path,**kwargs):
            calls.append(path)
            if path=='/v1/accounts/import/check':
                return {'exists':False,'accountId':None,'uid':'123','hidden':False}
            started.set()
            await release.wait()
            return {'account':{'accountId':'first-account'},'ready':True}

        app.tg.fetch_upload=fetch
        app.api.call=provider
        await app.callback(99,{'data':'acctimp:start:7'})
        await app.handle(document('source'))
        await app.callback(99,{'data':button_callback(app,'acctimp:apply:')})
        await asyncio.wait_for(started.wait(),timeout=2)
        await app.callback(99,{'data':button_callback(app,'acctimp:stop:')})
        assert 'после текущей записи' in app.tg.out[-1][1]
        release.set()
        await asyncio.wait_for(app.account_imports.task,timeout=2)
        job=app.db.account_import(99,target_id=7)
        assert job['summary']['assigned']==1 and job['summary']['attempted']==1
        assert job['summary']['stopped']=='остановлено владельцем'
        assert calls==['/v1/accounts/import/check','/v1/accounts/import']
        app.db.close()

    asyncio.run(run())


def test_only_summary_survives_restart(tmp_path):
    async def run():
        path=str(tmp_path/'bot.sqlite3')
        db=ExtendedStore(path)
        db.create_account_import('a'*12,99,7,asdict(ImportSummary(3,attempted=1,assigned=1)))
        db.assign(7,'confirmed')
        db.close()

        db=ExtendedStore(path)
        app=ProximaApp(Config('test','test',99),db,FakeTG(),FakeAPI())
        await app.callback(99,{'data':'acctimp:last:7'})
        assert 'Импорт прерван' in app.tg.out[-1][1]
        assert db.account_import(99,target_id=7)['summary']['assigned']==1
        assert db.account_ids(7)==['confirmed']
        assert not app.account_imports.previews
        assert app.api.calls==[]
        db.close()

    asyncio.run(run())


def test_import_allows_selecting_folder_and_preserves_choice_across_files():
    async def run():
        app=new_app()
        uploads={'f1':account_rows('one'),'f2':account_rows('two')}
        posted=[]

        async def fetch(file_id,limit):
            return uploads[file_id],file_id+'.txt'

        async def provider(method,path,**kwargs):
            if path=='/v1/account-folders':
                earlier=[{'id':f'{i:08x}','name':f'Папка {i}','count':0,'active':0} for i in range(1,21)]
                return {'available':True,'unassigned':0,'items':earlier+[{'id':'1234abcd','name':'Трафер 7','count':0,'active':0}]}
            if path=='/v1/accounts/import/check':
                return {'exists':False,'accountId':None,'uid':'123','hidden':False}
            if path=='/v1/accounts/import':
                posted.append(kwargs['payload'])
                return {'account':{'accountId':'imp-'+str(len(posted))},'ready':True}
            raise AssertionError(path)

        app.tg.fetch_upload=fetch
        app.api.call=provider
        try:
            await app.callback(99,{'data':'acctimp:start:7'})
            await app.handle(document('f1'))
            await app.callback(99,{'data':button_callback(app,'acctimp:folders:')})
            assert 'Трафер 7' not in str(app.tg.out[-1][2])
            token=app.db.draft(99)['data']['token']
            await app.callback(99,{'data':f'acctimp:folder:{token}:1234abcd'})
            assert 'folder_id' not in app.db.draft(99)['data']
            await app.callback(99,{'data':button_callback(app,f'acctimp:folders:{token}:1')})
            assert 'Трафер 7' in str(app.tg.out[-1][2])
            folder_cb=next(b['callback_data'] for r in app.tg.out[-1][2]['inline_keyboard'] for b in r if b['callback_data'].endswith(':1234abcd'))
            await app.callback(99,{'data':folder_cb})
            # Ensure folder selection sticks when another file is added before apply
            await app.handle(document('f2'))
            assert 'Трафер 7' in app.tg.out[-1][1]
            await app.callback(99,{'data':button_callback(app,'acctimp:apply:')})
            await asyncio.wait_for(app.account_imports.task,timeout=2)
            assert [p.get('folderId') for p in posted]==['1234abcd','1234abcd']
        finally:
            await app.account_imports.close()
            app.db.close()

    asyncio.run(run())


def test_member_import_uses_only_reserved_folder_and_cannot_update_other_accounts():
    async def run():
        app=new_app()
        app.tg=TelegramWithEdits()
        app.db.grant(10,30)
        app.db.grant(11,30)
        folder_id='1234abcd'
        contents={}
        posted=[]
        checks={
            'new':{'exists':False,'accountId':None,'uid':'456','hidden':False},
            'other':{'exists':True,'accountId':'acc8','uid':'789','hidden':False},
            'shared':{'exists':True,'accountId':'common','uid':'999','hidden':False},
            'unknown':{'exists':None,'accountId':None,'uid':None,'hidden':False},
            'missing-uid':{'exists':False,'accountId':None,'uid':None,'hidden':False},
            'own':{'exists':True,'accountId':'member-new','uid':'456','hidden':False},
        }

        async def fetch(file_id,limit):
            values=('new','other','shared','unknown','missing-uid') if file_id=='first' else ('own',)
            entries=[{'cookies':[{'name':'sid_guard','value':value}],
                      'folderId':'bad0f01d','proxyId':'another-proxy'} for value in values]
            return json.dumps(entries).encode(),'accounts.txt'

        async def read_folder(fid,*,expected_count=None):
            assert fid==folder_id
            assert expected_count==(len(contents) or None)
            return dict(contents)

        async def provider(method,path,**kwargs):
            if path=='/v1/account-folders':
                assert method=='GET'
                return {'available':True,'items':[{'id':folder_id,'name':'Трафер 10','count':len(contents),'active':0}]}
            payload=kwargs['payload']
            value=payload['cookies'][0]['value']
            if path=='/v1/accounts/import/check':
                assert set(payload)=={'cookies'}
                return checks[value]
            assert (method,path)==('POST','/v1/accounts/import')
            assert set(payload)=={'cookies','folderId'} and payload['folderId']==folder_id
            assert value in ('new','own')
            posted.append(value)
            account={'accountId':'member-new','folderId':folder_id}
            contents['member-new']=account
            return {'account':account,'ready':True}

        app.tg.fetch_upload=fetch
        app.api.call=provider
        app.api.accounts_in_folder=read_folder
        try:
            await app.callback(10,{'data':'accounts'})
            start=button_callback(app,'acctimp:self')
            await app.callback(10,{'data':start})
            assert 'Попроси владельца' in app.tg.out[-1][1]
            assert app.db.draft(10) is None

            await app.callback(99,{'data':'adm:foldersync:10'})
            await app.callback(99,{'data':button_callback(app,'adm:fsp:10:')})
            bind=button_callback(app,'adm:fsbind:10:')
            await app.callback(10,{'data':bind})
            assert app.db.member_import_folder(10) is None
            await app.callback(99,{'data':bind})
            assert app.db.member_import_folder(10)==folder_id
            await app.callback(99,{'data':f'adm:fsbind:11:{folder_id}'})
            assert 'уже привязана к другому траферу' in app.tg.out[-1][1]
            assert app.db.member_import_folder(11) is None

            await app.callback(10,{'data':start})
            await app.handle(document('first',uid=10))
            assert 'Твоя выделенная папка' in app.tg.out[-1][1]
            assert 'acctimp:folders:' not in str(app.tg.out[-1][2])
            apply=button_callback(app,'acctimp:apply:')
            await app.callback(11,{'data':apply})
            assert posted==[]
            await app.callback(10,{'data':apply})
            await asyncio.wait_for(app.account_imports.task,timeout=2)
            first=app.db.account_import(10,target_id=10)
            assert first['state']=='finished'
            assert first['summary']==asdict(ImportSummary(5,attempted=5,assigned=1,conflicts=2,unverified=2))
            assert app.db.account_ids(10)==['member-new']
            assert app.db.account_assignee('acc8')==8 and app.db.account_assignee('common') is None
            assert 'Итог импорта' in app.tg.out[-1][1]
            result=button_callback(app,'acctimp:result:')
            await app.callback(11,{'data':result})
            assert not app.tg.documents
            await app.callback(10,{'data':result})
            assert app.tg.documents[-1][1]=='member-new · готов\n'.encode()
            await app.callback(99,{'data':'acctimp:last:10'})
            assert button_callback(app,'acctimp:result:')==result

            await app.callback(10,{'data':start})
            await app.handle(document('next',uid=10))
            await app.callback(10,{'data':button_callback(app,'acctimp:apply:')})
            await asyncio.wait_for(app.account_imports.task,timeout=2)
            assert posted==['new','own']
            assert app.db.account_import(10,target_id=10)['summary']['assigned']==1
        finally:
            await app.account_imports.close()
            app.db.close()

    asyncio.run(run())


@pytest.mark.parametrize('folder_members, revoke_access', [({'acc7'},False),({'acc7','common'},False),({'acc7'},True)])
def test_member_import_reuses_exact_existing_folder_and_checks_access_before_write(folder_members,revoke_access):
    async def run():
        app=new_app()
        folder_id='1234abcd'
        calls=[]

        async def fetch(file_id,limit):
            return account_rows('synthetic'),'accounts.txt'

        async def accounts_for(ids):
            assert ids==['acc7']
            return {'acc7':{'folderId':folder_id}}

        async def read_folder(fid,*,expected_count):
            assert fid==folder_id and expected_count==1
            return {aid:{'folderId':fid} for aid in folder_members}

        async def provider(method,path,**kwargs):
            calls.append(path)
            if path=='/v1/accounts/import/check':
                if revoke_access:app.db.modify_access(7,99,'revoke',0)
                return {'exists':False,'accountId':None,'uid':'456','hidden':False}
            assert kwargs['payload']['folderId']==folder_id
            return {'account':{'accountId':'member-new'},'ready':True}

        app.tg.fetch_upload=fetch
        app.api.call=provider
        app.api.accounts_for=accounts_for
        app.api.accounts_in_folder=read_folder
        try:
            await app.callback(7,{'data':'acctimp:self'})
            await app.handle(document('existing',uid=7))
            await app.callback(7,{'data':button_callback(app,'acctimp:apply:')})
            await asyncio.wait_for(app.account_imports.task,timeout=2)
            job=app.db.account_import(7,target_id=7)
            if 'common' in folder_members:
                assert calls==[]
                assert app.db.member_import_folder(7) is None
                assert 'Состав папки отличается' in job['summary']['stopped']
            elif revoke_access:
                assert calls==['/v1/accounts/import/check']
                assert 'Доступ или пул аккаунтов изменился' in job['summary']['stopped']
            else:
                assert calls==['/v1/accounts/import/check','/v1/accounts/import']
                assert app.db.member_import_folder(7)==folder_id
                assert job['summary']['assigned']==1
        finally:
            await app.account_imports.close()
            app.db.close()

    asyncio.run(run())


@pytest.mark.parametrize('folder_mode',['default','none','file'])
def test_import_preserves_file_folder_or_omits_it_after_explicit_choice(folder_mode):
    async def run():
        app=new_app()
        sent=[]

        async def fetch(file_id,limit):
            body={'cookies':[{'name':'sid_guard','value':file_id}],'folderId':'1234abcd'}
            return json.dumps(body).encode(),file_id+'.json'

        async def provider(method,path,**kwargs):
            if path=='/v1/account-folders':return {'available':True,'items':[]}
            if path=='/v1/accounts/import/check':return {'exists':False,'accountId':None}
            assert path=='/v1/accounts/import'
            sent.append(kwargs['payload'])
            return {'account':{'accountId':f'created-{len(sent)}'},'ready':True}

        async def upload(file_id):
            update=document(file_id)
            update['message']['document']['file_name']=file_id+'.json'
            await app.handle(update)

        async def pick(value):
            await app.callback(99,{'data':button_callback(app,'acctimp:folders:')})
            token=app.db.draft(99)['data']['token']
            await app.callback(99,{'data':button_callback(app,f'acctimp:folder:{token}:{value}')})

        app.tg.fetch_upload=fetch
        app.api.call=provider
        try:
            await app.callback(99,{'data':'acctimp:start:7'})
            await upload('first')
            if folder_mode!='default':await pick('none')
            if folder_mode=='file':await pick('file')
            await upload('second')
            expected_label='Не задавать папку' if folder_mode=='none' else 'Из файла, если указана'
            assert expected_label in app.tg.out[-1][1]
            await app.callback(99,{'data':button_callback(app,'acctimp:apply:')})
            await asyncio.wait_for(app.account_imports.task,timeout=2)
            assert len(sent)==2
            if folder_mode=='none':assert all('folderId' not in entry for entry in sent)
            else:assert all(entry['folderId']=='1234abcd' for entry in sent)
        finally:
            await app.account_imports.close()
            app.db.close()
    asyncio.run(run())
