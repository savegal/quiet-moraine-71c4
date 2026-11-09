"""Administrator-only server CLI for sensitive account/proxy imports.

Never send cookie files or proxy passwords through Telegram bot chat.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import aiohttp
from .api import Tokora,ApiError
from .config import tokora_credentials
from .db import MAX_USER_ID,Store
from .operations import VALID_ID,declared_operations,operation_channel,operation_plan


async def main() -> None:
    parser=argparse.ArgumentParser(description='Proxima Soft local administrator operations')
    commands=parser.add_subparsers(dest='action',required=True)
    acc=commands.add_parser('import-account',help='Import authorized TikTok cookies from a local JSON file')
    acc.add_argument('json_file',type=Path)
    acc.add_argument('--user-id',required=True,type=int)
    proxy=commands.add_parser('import-proxies',help='Import proxies from a local file (not via Telegram)')
    proxy.add_argument('text_file',type=Path)
    batch=commands.add_parser('import-accounts-file',help='Import a local JSON/TXT/ZIP/TAR/GZ cookies archive (<=10 MiB)')
    batch.add_argument('file',type=Path)
    account_check=commands.add_parser('check-accounts',help='Start account session checks')
    proxy_check=commands.add_parser('check-proxies',help='Start proxy checks')
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
        timeout=max(5,int(os.environ.get('UPSTREAM_TIMEOUT','25')))
        async with aiohttp.ClientSession() as session:
            api=Tokora(session,base_url,key,timeout)
            if args.action=='api':
                await run_operation(api,args)
            elif args.action=='import-account':
                data=json.loads(args.json_file.read_text(encoding='utf-8-sig'))
                if isinstance(data,list):data={'cookies':data}
                if not isinstance(data,dict) or not isinstance(data.get('cookies'),list) or not data['cookies']:
                    raise ValueError('Input must be a JSON cookies array or {"cookies": [...]}')
                if not 0<args.user_id<=MAX_USER_ID:raise ValueError('Invalid user ID')
                db=Store(os.environ.get('DATABASE_PATH','data/proxima.sqlite3'))
                try:
                    result=await api.call('POST','/v1/accounts/import',payload=data,retries=0)
                    view=result.get('account') or {}
                    aid=str(view.get('accountId') or '')
                    if not VALID_ID.fullmatch(aid):raise ValueError('Import returned no valid account ID; inspect API status in dashboard')
                    db.assign(args.user_id,aid)
                finally:db.close()
                print('Imported and assigned account ID:',aid,'to Telegram user',args.user_id)
                if result.get('ready') is not True:
                    print('Session readiness is pending or unconfirmed. Check the account status before creating a task.')
            elif args.action=='import-accounts-file':
                content=read_upload(args.file,10*1024*1024)
                result=await api.upload('/v1/accounts/import/file',content)
                print('Account import accepted, job ID:',str(result.get('id') or result.get('jobId') or 'check API tasks'))
            elif args.action=='check-accounts':
                await api.call('POST','/v1/accounts/check',retries=0)
                print('Account check started')
            elif args.action=='check-proxies':
                await api.call('POST','/v1/proxies/check',retries=0)
                print('Proxy check started')
            elif args.action=='set-account-proxy':
                import re
                if not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}',args.account_id) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}',args.proxy_id):
                    raise ValueError('Invalid identifier')
                proxy_binding={'proxy':None} if args.proxy_id=='-' else {'proxyId':args.proxy_id}
                await api.call('PUT','/v1/accounts/'+args.account_id+'/proxy',payload=proxy_binding,retries=0)
                print('Proxy binding updated')
            elif args.action=='import-proxies':
                lines=[x.strip() for x in args.text_file.read_text(encoding='utf-8-sig').splitlines() if x.strip()]
                if not 1<=len(lines)<=1000:raise ValueError('Expected 1..1000 nonempty lines')
                await api.call('POST','/v1/proxies',payload={'lines':lines,'replaceEngine':False},retries=0)
                # Do not print proxy credentials or full response (can include addresses).
                print('Proxy import submitted; check proxy list in external dashboard.')
    except ApiError as e:
        print('API operation failed:',e.code)
        raise SystemExit(1)
    except (OSError,ValueError) as e:
        parser.error(str(e))


def read_upload(path:Path,limit:int)->bytes:
    with path.open('rb') as stream:
        content=stream.read(limit+1)
    if not content or len(content)>limit:
        raise ValueError(f'Expected 1..{limit} bytes')
    return content


async def run_operation(api:Tokora,args)->None:
    payload=json.loads(args.json_file.read_text(encoding='utf-8-sig')) if args.json_file else {}
    plan=operation_plan(args.operation_id,payload,local=True,upload_type=args.content_type if args.upload else '')
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
        with args.output.open('xb') as stream:
            stream.write(result if isinstance(result,bytes) else json.dumps(result,ensure_ascii=False,indent=2).encode('utf-8'))
    print('Operation accepted:',args.operation_id)


if __name__=='__main__':
    asyncio.run(main())
