"""Import account files and bind only IDs confirmed by the provider."""
from __future__ import annotations

import asyncio
import gzip
import io
import json
import math
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile

from .api import ApiError, Tokora
from .db import MAX_USER_ID, Store
from .operations import VALID_ID, load_spec, valid_body


MAX_UNPACKED_BYTES = 20 * 1024 * 1024
MAX_ACCOUNT_FILE_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_FILES = 2000
ACCOUNT_FILE_SUFFIXES = {'.json', '.txt', '.zip', '.tar', '.gz', '.gzip'}


def _json_accounts(text:str)->list[dict]:
    value=json.loads(text)
    if isinstance(value,dict) and 'accounts' in value:
        value=value['accounts']
        if not isinstance(value,list):raise ValueError('Поле accounts должно быть массивом.')
    elif isinstance(value,dict):
        value=[value]
    elif isinstance(value,list) and value and all(
            isinstance(cookie,dict) and 'name' in cookie and 'value' in cookie for cookie in value):
        value=[value]
    if not isinstance(value,list):raise ValueError('Нужен JSON с аккаунтами или cookies.')
    return [{'cookies':entry} if isinstance(entry,list) else entry for entry in value]


def _text_accounts(text:str)->list[dict]:
    lines=[line.strip(' ') for line in text.splitlines()
           if line.strip() and (not line.lstrip().startswith('#') or line.lstrip().startswith('#HttpOnly_'))]
    if any('\t' in line for line in lines):
        cookies=[]
        for line in lines:
            fields=line.split('\t',6)
            if len(fields)!=7:raise ValueError('Некорректная строка Netscape cookies.txt.')
            domain,subdomains,path,secure,expires,name,value=fields
            if (subdomains not in ('TRUE','FALSE') or secure not in ('TRUE','FALSE')
                    or not expires.isascii() or not expires.isdecimal()):
                raise ValueError('Некорректный формат Netscape cookies.txt.')
            cookies.append({'domain':domain.removeprefix('#HttpOnly_'),'path':path,'hostOnly':subdomains=='FALSE',
                            'secure':secure=='TRUE','httpOnly':domain.startswith('#HttpOnly_'),
                            'expirationDate':int(expires),'name':name,'value':value})
        return [{'cookies':cookies}]
    accounts=[]
    for line in lines:
        if line.lower().startswith('cookie:'):line=line[7:].strip()
        cookies=[]
        for item in line.rstrip(';').split(';'):
            name,separator,value=item.strip().partition('=')
            if not separator or not name.strip():raise ValueError('В TXT нужна одна строка Cookie на аккаунт.')
            cookies.append({'name':name.strip(),'value':value.strip()})
        accounts.append({'cookies':cookies})
    return accounts


def _plain_accounts(content:bytes,suffix:str)->list[dict]:
    text=content.decode('utf-8-sig')
    if suffix!='.json' and not text.lstrip().startswith(('[','{')):
        return _text_accounts(text)
    try:return _json_accounts(text)
    except json.JSONDecodeError:
        accounts=[]
        for number,line in enumerate(text.splitlines(),1):
            if not line.strip():continue
            try:entries=_json_accounts(line)
            except (json.JSONDecodeError,ValueError):
                raise ValueError(f'Не удалось прочитать JSON в строке {number}.') from None
            if len(entries)!=1:raise ValueError(f'В строке {number} должен быть один аккаунт.')
            accounts.extend(entries)
        return accounts


def _tar_accounts(content: bytes) -> list[dict]:
    accounts = []
    size = files = 0
    with tarfile.open(fileobj=io.BytesIO(content), mode='r:') as archive:
        for member in archive:
            if member.isdir():
                continue
            extension = PurePosixPath(member.name).suffix.lower()
            if not member.isfile() or extension not in ('.json','.txt'):
                raise ValueError('TAR должен содержать обычные JSON и TXT файлы.')
            size += member.size
            files += 1
            if size>MAX_UNPACKED_BYTES or files>MAX_ARCHIVE_FILES:
                raise ValueError('Архив превышает допустимый объём или число файлов.')
            with archive.extractfile(member) as file:
                accounts.extend(_plain_accounts(file.read(), extension))
    return accounts


def parse_account_file(content:bytes,suffix:str)->list[dict]:
    """Validate the complete file before sending the first account."""
    try:
        if suffix=='.zip':
            accounts=[]
            with ZipFile(io.BytesIO(content)) as archive:
                files=[item for item in archive.infolist() if not item.is_dir()]
                if len(files)>MAX_ARCHIVE_FILES or sum(item.file_size for item in files)>MAX_UNPACKED_BYTES:
                    raise ValueError('Архив превышает допустимый объём или число файлов.')
                for item in files:
                    extension=PurePosixPath(item.filename).suffix.lower()
                    if extension not in ('.json','.txt'):
                        raise ValueError('Для назначения ZIP должен содержать только JSON и TXT.')
                    accounts.extend(_plain_accounts(archive.read(item),extension))
        elif suffix == '.tar':
            accounts = _tar_accounts(content)
        elif suffix in ('.gz','.gzip'):
            with gzip.GzipFile(fileobj=io.BytesIO(content)) as stream:
                data = stream.read(MAX_UNPACKED_BYTES+1)
            if len(data)>MAX_UNPACKED_BYTES:
                raise ValueError('Распакованное содержимое превышает 20 МиБ.')
            try:
                accounts = _tar_accounts(data)
            except tarfile.ReadError:
                accounts = _plain_accounts(data,'.txt')
        elif suffix in ('.json','.txt'):
            accounts=_plain_accounts(content,suffix)
        else:
            raise ValueError('Используй JSON, TXT, ZIP, TAR или GZIP.')
    except (BadZipFile,RuntimeError,NotImplementedError,UnicodeError,json.JSONDecodeError,
            tarfile.TarError,EOFError,OSError):
        raise ValueError('Файл не удалось прочитать. Проверь формат и кодировку UTF-8.') from None
    schema=load_spec()['components']['schemas']['AccountImport']
    if not accounts:raise ValueError('Файл не содержит аккаунтов.')
    for number,entry in enumerate(accounts,1):
        cookies=entry.get('cookies') if isinstance(entry,dict) else None
        if not isinstance(cookies,list) or not cookies:
            raise ValueError(f'Некорректные cookies у записи №{number}.')
        for cookie in cookies:
            expires=cookie.get('expirationDate') if isinstance(cookie,dict) else None
            if isinstance(expires,float) and math.isfinite(expires):cookie['expirationDate']=int(expires)
        if not valid_body(schema,entry):
            raise ValueError(f'Некорректные cookies у записи №{number}.')
        if 'proxy' in entry and 'proxyId' in entry:
            raise ValueError(f'У записи №{number} укажи один способ привязки прокси.')
        accounts[number-1] = {key:value for key,value in entry.items() if key in schema['properties']}
    return accounts


@dataclass
class ImportSummary:
    total:int
    attempted:int=0
    assigned:int=0
    pending:int=0
    duplicates:int=0
    conflicts:int=0
    rejected:int=0
    unverified:int=0
    stopped:str=''


async def _import_one(api:Tokora,db:Store,uid:int,entry:dict,seen:set[str],*,self_service:bool=False,
                      allowed:Callable[[],bool]|None=None)->tuple[str,dict]:
    for attempt in range(2):
        try:
            with db.account_action(None,user_id=uid):
                if allowed and not allowed():
                    raise ValueError('Доступ или пул аккаунтов изменился. Открой импорт заново.')
                if db.account_check_pending():
                    raise ValueError('Дождись окончания общей проверки аккаунтов.')
                check=await api.call('POST','/v1/accounts/import/check',payload={'cookies':entry['cookies']},
                                     retries=0,background=True)
                exists,aid=check.get('exists'),check.get('accountId')
                if 'exists' not in check or 'accountId' not in check or (exists is not None and type(exists) is not bool):
                    raise ApiError('invalid_response')
                if self_service and exists is False:
                    identified_uid=check.get('uid')
                    if (not isinstance(identified_uid,str) or not identified_uid.isascii()
                            or not identified_uid.isdecimal() or check.get('hidden') is not False):
                        return 'unverified',{}
                if exists is True:
                    if not isinstance(aid,str) or not VALID_ID.fullmatch(aid):
                        raise ApiError('invalid_response')
                    assignee=db.account_assignee(aid)
                    if assignee!=uid and (self_service or assignee is not None):return 'conflicts',{}
                    if aid in seen:return 'duplicates',{}
                elif aid is not None:
                    raise ApiError('invalid_response')
                elif exists is None and (self_service or db.has_other_account_assignees(uid)):
                    return 'unverified',{}
                if allowed and not allowed():
                    raise ValueError('Доступ или пул аккаунтов изменился. Открой импорт заново.')
                result=await api.call('POST','/v1/accounts/import',payload=entry,retries=0,background=True)
                account=result.get('account')
                if exists is True and (not isinstance(account,dict) or account.get('accountId')!=aid):
                    raise ApiError('invalid_response')
                return '',result
        except ApiError as exc:
            if exc.status!=429 or attempt or exc.retry_after>60:
                raise
            await asyncio.sleep(max(1,exc.retry_after))


async def import_and_assign(api:Tokora,db:Store,uid:int,accounts:list[dict],*,
                            on_progress:Callable[[ImportSummary],None]|None=None,
                            on_assignment:Callable[[str,bool],None]|None=None,
                            should_stop:Callable[[],bool]|None=None,
                            self_service:bool=False,allowed:Callable[[],bool]|None=None)->ImportSummary:
    if not 0<uid<=MAX_USER_ID:raise ValueError('Некорректный Telegram ID.')
    summary=ImportSummary(len(accounts))
    seen=set()
    for entry in accounts:
        if should_stop and should_stop():
            summary.stopped='Остановлено пользователем' if self_service else 'остановлено владельцем'
            break
        summary.attempted+=1
        try:
            skipped,result=await _import_one(api,db,uid,entry,seen,self_service=self_service,allowed=allowed)
            if skipped:
                setattr(summary,skipped,getattr(summary,skipped)+1)
                continue
            account=result.get('account')
            aid=account.get('accountId') if isinstance(account,dict) else None
            if not isinstance(aid,str) or not VALID_ID.fullmatch(aid):
                summary.rejected+=1
            elif aid in seen:
                summary.duplicates+=1
            else:
                seen.add(aid)
                if db.assign(uid,aid,allow_transfer=False):
                    ready=result.get('ready') is True
                    summary.assigned+=1
                    if not ready:summary.pending+=1
                    if on_assignment:on_assignment(aid,ready)
                else:summary.conflicts+=1
        except ValueError as exc:
            summary.attempted-=1
            summary.stopped=str(exc)
            break
        except ApiError as exc:
            summary.rejected+=1
            if exc.status in (0,401,402,403,429) or exc.status>=500 or exc.code in ('missing_proxies','plan_account_limit'):
                summary.stopped={'missing_proxies':'в кабинете нет рабочих прокси',
                                 'plan_account_limit':'достигнут лимит аккаунтов кабинета'}.get(exc.code)
                if not summary.stopped:
                    summary.stopped=f'HTTP {exc.status}' if exc.status else 'связь с сервисом'
                break
        finally:
            if on_progress:on_progress(summary)
    return summary
