"""Server CLI for private imports and Tokora cabinet maintenance."""
from __future__ import annotations
import argparse
import asyncio
from contextlib import asynccontextmanager, closing
import json
import os
from pathlib import Path
import sqlite3
import aiohttp
from . import account_checks
from .accounts import check_status
from .api import Tokora,ApiError
from .config import tokora_credentials
from .db import MAX_USER_ID,Store
from .imports import ImportSummary, import_and_assign, parse_account_file
from .operations import VALID_ID,declared_operations,operation_channel,operation_plan
from .proxy_console import (bind_account_proxy, ensure_proxies_unbound, ensure_proxy_workspace_idle,
                            proxy_assignee, proxy_inventory, request_proxy_check)
from .workspace_api import CabinetAPI
from .workspace_registry import PRIMARY_WORKSPACE, WorkspaceRegistry, workspace_database


SENSITIVE_OPERATIONS = {'postAccountsImport','postAccountsImportFile','postProxies','patchProxiesById'}


def open_store(*, existing:bool=False, configured:bool=False, location:str|None=None)->Store:
    location = location if location is not None else os.environ.get('DATABASE_PATH','').strip()
    if configured and not location:
        raise ValueError('Укажи DATABASE_PATH рабочей базы бота в .env.')
    path = Path(location or 'data/proxima.sqlite3')
    if existing:
        if not path.is_file():
            raise ValueError('База бота не найдена. Проверь DATABASE_PATH в .env.')
        try:
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        except (OSError, sqlite3.DatabaseError):
            raise ValueError('Не удалось открыть рабочую базу бота. Проверь DATABASE_PATH.') from None
        if not {'users','metadata','accounts','tasks'}.issubset(tables):
            raise ValueError('В указанной базе нет таблиц Proxima Soft. Проверь DATABASE_PATH.')
    return Store(str(path))


def cli_cabinet(workspace: str, env_key: str) -> tuple[str, dict[str, str]]:
    primary = os.environ.get('DATABASE_PATH', '').strip()
    path = primary or 'data/proxima.sqlite3'
    if Path(path).is_file():
        with closing(open_store(existing=True, location=path)) as store:
            registry = WorkspaceRegistry(store.conn, path, env_key)
            if not registry.workspace(workspace):
                raise ValueError('Кабинет не найден. Укажи код из админки.')
            credentials = registry.credentials(workspace)
        return workspace_database(path, workspace) if workspace != PRIMARY_WORKSPACE else primary, credentials
    if workspace != PRIMARY_WORKSPACE:
        raise ValueError('База кабинетов не найдена. Проверь DATABASE_PATH.')
    return primary, {'env': env_key} if env_key else {}


@asynccontextmanager
async def cabinet_maintenance(api:Tokora, db:Store):
    with db.account_action(None):
        if db.account_check_pending():
            raise ValueError('Проверка аккаунтов выполняется или ожидает подтверждения.')
        await ensure_proxy_workspace_idle(api)
        yield


async def main() -> None:
    parser=argparse.ArgumentParser(description='Proxima Soft local administrator operations')
    parser.add_argument('--workspace', default=PRIMARY_WORKSPACE, help='Cabinet code from the bot admin panel')
    commands=parser.add_subparsers(dest='action',required=True)
    acc=commands.add_parser('import-account',help='Import authorized TikTok cookies from a local JSON file')
    acc.add_argument('json_file',type=Path)
    acc.add_argument('--user-id',required=True,type=int)
    proxy=commands.add_parser('import-proxies',help='Import proxies from a local file (not via Telegram)')
    proxy.add_argument('text_file',type=Path)
    proxy_edit=commands.add_parser('edit-proxy',help='Replace proxy credentials using one line from a private file')
    proxy_edit.add_argument('proxy_id')
    proxy_edit.add_argument('text_file',type=Path)
    batch=commands.add_parser('import-accounts-file',help='Import a local JSON/TXT/ZIP/TAR/GZ cookies archive (<=10 MiB)')
    batch.add_argument('file',type=Path)
    batch.add_argument('--user-id',type=int,help='Assign imported accounts to this Telegram ID')
    commands.add_parser('check-accounts',help='Start account session checks')
    purge=commands.add_parser('purge-accounts',help='Remove all Tokora accounts after confirming the cabinet is idle and unassigned')
    purge.add_argument('--confirm',choices=('DELETE',),required=True)
    proxy_check=commands.add_parser('check-proxies',help='Start proxy checks')
    proxy_check.add_argument('--id',dest='ids',action='append',help='Check a specific proxy ID (repeat to select more)')
    bind=commands.add_parser('set-account-proxy',help='Bind existing provider proxy ID to an account')
    bind.add_argument('account_id');bind.add_argument('proxy_id')
    commands.add_parser('api-list',help='List operation IDs in the bundled Tokora OpenAPI')
    operation=commands.add_parser('api',help='Run an OpenAPI operation on the server')
    operation.add_argument('operation_id')
    operation.add_argument('--json-file',type=Path,help='Local JSON with path, query and body')
    operation.add_argument('--upload',type=Path,help='Local binary request body')
    operation.add_argument('--content-type',default='application/octet-stream')
    operation.add_argument('--output',type=Path,help='Write response to a new local file')
    operation.add_argument('--max-download-mib',type=int,choices=range(1,129),default=32,metavar='1–128',
                           help='Maximum binary response size in MiB (default: 32)')
    args=parser.parse_args()
    if args.action=='api-list':
        for path,method,info in declared_operations():
            print(f'{info["operationId"]:42} {method.upper():7} {path:50} {operation_channel(info)}')
        return
    try:
        base_url,key=tokora_credentials()
        location,credentials=cli_cabinet(args.workspace,key)
        if not credentials:
            raise ValueError('Подключи ключ кабинета в /admin → Кабинеты и ключи.')
        timeout=max(5,int(os.environ.get('UPSTREAM_TIMEOUT','25')))
        async with aiohttp.ClientSession() as session:
            if set(credentials) == {'env'}:
                api=Tokora(session,base_url,credentials['env'],timeout)
            else:
                api=CabinetAPI(session,base_url,timeout)
                api.set_keys(credentials)
            if args.action=='api':
                await run_cli_operation(api,args, location=location)
            elif args.action=='import-account':
                if not 0<args.user_id<=MAX_USER_ID:raise ValueError('Invalid user ID')
                accounts=parse_account_file(read_upload(args.json_file,10*1024*1024),'.json')
                if len(accounts)!=1:
                    raise ValueError('Ожидалась одна запись. Для файла с несколькими аккаунтами используй import-accounts-file.')
                with closing(open_store(location=location)) as db:
                    summary=await import_and_assign(api,db,args.user_id,accounts)
                print_account_import(summary)
            elif args.action=='import-accounts-file':
                content=read_upload(args.file,10*1024*1024)
                if args.user_id is None:
                    with closing(open_store(existing=True, location=location)) as db:
                        async with cabinet_maintenance(api,db):
                            result=await api.upload('/v1/accounts/import/file',content)
                    print('Tokora приняла файл. ID задачи:',safe_id(result.get('id')))
                else:
                    if not 0<args.user_id<=MAX_USER_ID:raise ValueError('Invalid user ID')
                    accounts=parse_account_file(content,args.file.suffix.lower())
                    with closing(open_store(location=location)) as db:
                        summary=await import_and_assign(api,db,args.user_id,accounts)
                    print_account_import(summary)
            elif args.action=='check-accounts':
                with closing(open_store(location=location)) as db:
                    status=await account_checks.start_workspace_account_check(api,db)
                print_check_status(status)
            elif args.action=='purge-accounts':
                with closing(open_store(existing=True,configured=True, location=location)) as db:
                    deleted=await account_checks.purge_workspace_accounts(api,db,args.confirm)
                print(f'Удалено аккаунтов: {deleted}.')
            elif args.action=='check-proxies':
                ids=list(dict.fromkeys(args.ids)) if args.ids else None
                if ids and (len(ids)>50 or any(not VALID_ID.fullmatch(pid) for pid in ids)):
                    raise ValueError('Укажи до 50 действительных ID прокси.')
                with closing(open_store(location=location)) as db, db.account_action(None):
                    status,_=await request_proxy_check(api,ids)
                print_check_status(status)
            elif args.action=='set-account-proxy':
                proxy_id=None if args.proxy_id=='-' else args.proxy_id
                with closing(open_store(existing=True, location=location)) as db:
                    assignee=proxy_assignee(db,args.account_id)
                    await bind_account_proxy(api,db,args.account_id,proxy_id,assignee)
                print('Привязка прокси обновлена.')
            elif args.action=='import-proxies':
                lines=read_proxy_lines(args.text_file,1000)
                with closing(open_store(existing=True, location=location)) as db:
                    async with cabinet_maintenance(api,db):
                        result=await api.call('POST','/v1/proxies',payload={'lines':lines,'replaceEngine':False},retries=0)
                print_proxy_import(result)
            elif args.action=='edit-proxy':
                if not VALID_ID.fullmatch(args.proxy_id):raise ValueError('Некорректный ID прокси.')
                proxy,=read_proxy_lines(args.text_file,1)
                with closing(open_store(existing=True, location=location)) as db:
                    async with cabinet_maintenance(api,db):
                        await api.call('PATCH','/v1/proxies/'+args.proxy_id,payload={'proxy':proxy},retries=0)
                print('Реквизиты прокси обновлены. Запусти проверку перед работой.')
    except ApiError as e:
        print(f'Ошибка Tokora (HTTP {e.status or "нет ответа"}):',safe_id(e.code))
        if e.request_id:print('ID запроса Tokora:',e.request_id)
        raise SystemExit(1)
    except (OSError,ValueError) as e:
        parser.error(str(e))


def read_upload(path:Path,limit:int)->bytes:
    with path.open('rb') as stream:
        content=stream.read(limit+1)
    if not content or len(content)>limit:
        raise ValueError(f'Expected 1..{limit} bytes')
    return content


def safe_id(value:object)->str:
    return value if isinstance(value,str) and VALID_ID.fullmatch(value) else '—'


def read_proxy_lines(path:Path, maximum:int)->list[str]:
    try:
        text=read_upload(path,1024*1024).decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValueError('Файл прокси должен быть в UTF-8.') from None
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    if not 1<=len(lines)<=maximum:
        raise ValueError(f'Ожидалось от 1 до {maximum} непустых строк.')
    return lines


def print_proxy_import(result:dict)->None:
    counts=result.get('import')
    fields=(('received','Получено'),('added','Добавлено'),('duplicates','Повторы'),('total','Всего'))
    if isinstance(counts,dict) and all(type(counts.get(key)) is int and counts[key]>=0 for key,_ in fields):
        print(' · '.join(f'{label} {counts[key]}' for key,label in fields))
    else:
        print('Tokora приняла импорт. Сводка недоступна, открой список прокси.')
    if result.get('engineSync')=='pending':
        print('Синхронизация с движком ожидает повтора со стороны Tokora.')


def print_check_status(status:dict)->None:
    state=check_status(status)
    print({'active':'Проверка выполняется.','idle':'Проверка не выполняется.',
           'unknown':'Tokora ещё не подтвердила состояние проверки.'}[state])
    checked,total=status.get('checked'),status.get('total')
    if type(checked) is int and type(total) is int:
        print(f'Проверено: {checked} / {total}.')


def print_account_import(summary:ImportSummary)->None:
    print(f'Записей в файле: {summary.total}. Закреплено за участником: {summary.assigned}.')
    print(f'Ожидают проверки: {summary.pending}. Дубли: {summary.duplicates}. '
          f'Конфликты назначения: {summary.conflicts}. Ошибки: {summary.rejected}. '
          f'UID не определён: {summary.unverified}. '
          f'Не обработано: {summary.total-summary.attempted}.')
    if summary.stopped:print('Импорт прерван:',summary.stopped)
    if summary.assigned+summary.duplicates!=summary.total:raise SystemExit(1)


async def run_cli_operation(api:Tokora,args,*,location:str|None=None)->None:
    """Actual CLI entry point. Protect raw mutation routes using the shared database."""
    if args.operation_id not in SENSITIVE_OPERATIONS:
        return await run_operation(api,args)
    plan=cli_operation_plan(args)
    with closing(open_store(existing=True, location=location)) as db:
        async with cabinet_maintenance(api,db):
            body=plan.get('payload') or {}
            if args.operation_id=='postProxies' and body.get('replaceEngine') is True:
                inventory=await proxy_inventory(api)
                if inventory.tokora_lines:
                    raise ValueError('Замена пула Tokora выполняется в кабинете, где видны его привязки.')
                if inventory.rows:
                    await ensure_proxies_unbound(api,inventory.rows,list(inventory.rows))
                    await ensure_proxy_workspace_idle(api)
            await run_operation(api,args,plan=plan)


def cli_operation_plan(args)->dict:
    payload=json.loads(args.json_file.read_text(encoding='utf-8-sig')) if args.json_file else {}
    return operation_plan(args.operation_id,payload,local=True,upload_type=args.content_type if args.upload else '')


async def run_operation(api:Tokora,args,*,plan:dict|None=None)->None:
    plan=cli_operation_plan(args) if plan is None else plan
    if args.output and args.output.exists():
        raise ValueError('Output already exists')
    if args.upload:
        limit_mib={'postAccountsImportFile':10,'postPhotos':15,'postPostsDraftsByIdPhotos':15}.get(args.operation_id,20)
        limit=limit_mib*1024*1024
        result=await api.upload(plan['path'],read_upload(args.upload,limit),args.content_type,params=plan['params'])
    elif plan['method']=='GET' and plan['binary']:
        if not args.output:
            raise ValueError('Use --output for a file response')
        result,_=await api.download(plan['path'],params=plan['params'],max_bytes=args.max_download_mib*1024*1024)
    else:
        result=await api.call(plan['method'],plan['path'],params=plan['params'],payload=plan['payload'])
    if args.output:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os,'O_BINARY',0)
        fd = os.open(args.output,flags,0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(result if isinstance(result,bytes) else json.dumps(result,ensure_ascii=False,indent=2).encode('utf-8'))
    print('Operation accepted:',args.operation_id)


if __name__=='__main__':
    asyncio.run(main())
