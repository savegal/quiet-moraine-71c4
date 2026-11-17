"""Safe, shared interpretation of the provider's account session states."""
from collections import Counter
import re


ACCOUNT_FOLDER_ID = re.compile(r'[0-9a-f]{8}')


def valid_task_folder(folder: object, *, allow_workspace: bool = False) -> bool:
    """Validate an accountFolder value accepted by POST /v1/tasks."""
    if not isinstance(folder, str):
        return False
    if allow_workspace and folder in ('', 'none'):
        return True
    return bool(ACCOUNT_FOLDER_ID.fullmatch(folder))


def valid_account_query_folder(folder: object) -> bool:
    """Validate a folder filter accepted by GET /v1/accounts."""
    if not isinstance(folder, str):
        return False
    return folder in ('', 'all', 'none') or bool(ACCOUNT_FOLDER_ID.fullmatch(folder))


ACCOUNT_STATUS={
    'active':'✓ Активен',
    'expired':'× Сессия истекла',
    'error':'! Ошибка проверки',
    'unknown':'○ Статус неизвестен',
    'missing':'○ Аккаунт не найден',
}
CHECK_ACTIVE_STATES = {'running', 'working', 'checking', 'queued', 'pending',
                       'starting', 'stopping', 'in_progress', 'active'}
CHECK_IDLE_STATES = {'idle', 'unchecked', 'done', 'finished', 'completed', 'complete',
                     'success', 'stopped', 'canceled', 'cancelled', 'error', 'failed'}


def session_state(account:dict|None)->str:
    if account is None:return 'missing'
    status=account.get('status')
    return status if isinstance(status,str) and status in ('active','expired','error') else 'unknown'


def account_counts(ids:list[str],accounts:dict[str,dict])->dict[str,int]:
    counts=Counter(session_state(accounts.get(aid)) for aid in ids)
    return {key:counts[key] for key in ACCOUNT_STATUS}


def has_proxy_binding(account:dict)->bool:
    return bool(account.get('proxyId') or account.get('proxy'))


def check_status(status: dict) -> str:
    """Interpret an account check snapshot without clearing a hold on unknown state."""
    state = status.get('state') if isinstance(status, dict) else None
    if not isinstance(state, str):
        return 'unknown'
    state = state.strip().lower()
    if state in CHECK_ACTIVE_STATES:
        return 'active'
    if state in CHECK_IDLE_STATES:
        return 'idle'
    return 'unknown'


def check_is_active(status: dict) -> bool:
    return check_status(status) == 'active'
