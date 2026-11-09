"""OpenAPI owner-only operation planner with strict path params, schema checks,
confirmation of every mutation, and no generic remote URL input.

Purpose: expose obscure provider operations without hardcoding unsafe raw HTTP
commands or leaking authentication. Never use to bypass the member job queue.
"""
from __future__ import annotations
import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote
import re
from jsonschema import Draft202012Validator

SPEC_PATH=Path(__file__).parent.parent/'docs/tokora-openapi.json'
QUEUE_OPERATIONS={'postTasks','postSchedules','deleteTasksById'}
LOCAL_ONLY={'postAccountsImport','postAccountsImportFile','postProxies','patchProxiesById','putAccountsByIdProxy'}
VALID_ID=re.compile(r'^[a-zA-Z0-9_-]{1,128}$')


@lru_cache(maxsize=1)
def load_spec() -> dict:
    return json.loads(SPEC_PATH.read_text(encoding='utf-8'))


def valid_body(schema:dict,body:dict)->bool:
    return Draft202012Validator({**schema,'components':load_spec()['components']}).is_valid(body)


def declared_operations():
    for path,methods in load_spec()['paths'].items():
        for method,operation in methods.items():
            if method in ('get','post','put','patch','delete') and operation.get('operationId'):
                yield path,method,operation


def binary_response(operation:dict)->bool:
    return any(item.get('schema',{}).get('format')=='binary'
               for status,response in operation.get('responses',{}).items() if str(status).startswith('2')
               for item in response.get('content',{}).values())


def operation_channel(operation:dict)->str:
    name=operation['operationId']
    if name=='postAccountsPurge':return 'cabinet'
    if name in QUEUE_OPERATIONS or name=='deleteAccountsById':return 'managed'
    body_types=operation.get('requestBody',{}).get('content',{})
    if name in LOCAL_ONLY or binary_response(operation) or (body_types and 'application/json' not in body_types):
        return 'cli'
    return 'op/cli'


def operation_plan(operation_id:str,raw:dict,*,local:bool=False,upload_type:str='')->dict:
    if not isinstance(raw,dict) or set(raw)-{'path','query','body'}:
        raise ValueError('Use JSON {"path":{},"query":{},"body":{}}')
    found=next(((path,method,entry) for path,method,entry in declared_operations()
                if entry['operationId']==operation_id),None)
    if not found:raise ValueError('Operation is not declared in OpenAPI')
    path,method,op=found
    if operation_id in QUEUE_OPERATIONS:
        raise ValueError('Операция выполняется через очередь задач или отдельный экран.')
    if operation_id=='deleteAccountsById':
        raise ValueError('Отвяжи аккаунт от команды и удали его из карточки владельца.')
    if operation_id=='postAccountsPurge':
        raise ValueError('Массовая очистка выполняется в кабинете после завершения заказов.')
    if operation_channel(op)=='cli' and not local:
        raise ValueError('Эта операция доступна в серверной CLI. Для медиа и результатов также есть отдельные экраны бота.')
    rp=raw.get('path',{})
    if not isinstance(rp,dict):raise ValueError('path must be object')
    needed=re.findall(r'{([^}]+)}',path)
    if set(rp)!=set(needed):raise ValueError('Path parameters: '+', '.join(needed))
    path_schemas={p['name']:p.get('schema',{}) for p in op.get('parameters',[]) if p.get('in')=='path'}
    for param in needed:
        value=rp[param]
        if (not isinstance(value,str) or not VALID_ID.fullmatch(value)
                or not Draft202012Validator(path_schemas.get(param,{})).is_valid(value)):
            raise ValueError('Invalid path parameter '+param)
        path=path.replace('{'+param+'}',quote(value,safe=''))
    params=raw.get('query',{})
    if not isinstance(params,dict):raise ValueError('query must be object')
    allowed={p['name']:p for p in op.get('parameters',[]) if p.get('in')=='query'}
    if set(params)-set(allowed):raise ValueError('Unknown query field')
    if any(p.get('required') and name not in params for name,p in allowed.items()):
        raise ValueError('Required query parameter missing')
    for k,v in params.items():
        if type(v) not in (str,int,float,bool) or len(str(v))>300:raise ValueError('Invalid query parameter '+k)
        if not Draft202012Validator(allowed[k].get('schema',{})).is_valid(v):
            raise ValueError('Query parameter does not match OpenAPI: '+k)
    body=raw.get('body')
    data=op.get('requestBody',{}).get('content',{})
    if method=='get' and body is not None:raise ValueError('GET does not accept body')
    if upload_type:
        mime=upload_type.partition('/')[0]+'/*'
        if body is not None or not any(data.get(kind,{}).get('schema',{}).get('format')=='binary'
                                       for kind in (upload_type,mime)):
            raise ValueError('Upload does not match the declared OpenAPI request body')
    elif body is not None:
        if not isinstance(body,dict):raise ValueError('JSON body must be object')
        if len(json.dumps(body,ensure_ascii=False))>30000:raise ValueError('JSON body too large')
        schema=data.get('application/json',{}).get('schema')
        if not schema:raise ValueError('This operation requires a binary upload or no body')
        if not valid_body(schema,body):
            raise ValueError('Body does not match OpenAPI schema')
    elif op.get('requestBody',{}).get('required') and method!='get':
        raise ValueError('Required JSON body missing')
    return {'operation':operation_id,'method':method.upper(),'path':path,'params':params,'payload':body,
            'binary':binary_response(op)}
