from __future__ import annotations
from dataclasses import dataclass
import os
from urllib.parse import urlsplit
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

from .catalog import MODES


TOKORA_TASK_SLOTS = 5
DEFAULT_OPEN_TASKS = TOKORA_TASK_SLOTS


def tokora_credentials() -> tuple[str, str]:
    load_dotenv()
    key=os.environ.get('TOKORA_API_KEY','').strip()
    if key.startswith('PUT_'):
        key = ''
    url=os.environ.get('TOKORA_BASE_URL','https://tokora.online').strip().rstrip('/')
    parsed=urlsplit(url)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username is not None or parsed.query or parsed.fragment:
        raise ValueError('TOKORA_BASE_URL must be an HTTPS base URL without credentials, query or fragment')
    return url,key


@dataclass(frozen=True)
class Config:
    telegram_token: str
    upstream_key: str
    owner_id: int
    base_url: str = 'https://tokora.online'
    database_path: str = 'data/proxima.sqlite3'
    dispatch_enabled: bool = False
    poll_seconds: int = 5
    upstream_timeout: int = 25
    max_active_external: int = TOKORA_TASK_SLOTS
    max_tasks_per_day: int = 100
    max_open_tasks: int = DEFAULT_OPEN_TASKS
    timezone_name: str = 'Asia/Jerusalem'
    restricted_modes: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'Config':
        url,key=tokora_credentials()
        token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
        owner = int(os.environ.get('OWNER_ID', '0'))
        if not token or token.startswith('PUT_') or owner <= 0:
            raise ValueError('Set TELEGRAM_BOT_TOKEN and OWNER_ID in .env')
        timezone_name = os.getenv('BOT_TIMEZONE', 'Asia/Jerusalem').strip()
        ZoneInfo(timezone_name)
        max_active = int(os.environ.get('MAX_ACTIVE_TASKS', str(TOKORA_TASK_SLOTS)))
        if max_active < 1:
            raise ValueError('MAX_ACTIVE_TASKS must be at least 1')
        raw_restricted = [item.strip().lower() for item in os.environ.get('RESTRICTED_MODES', '').split(',') if item.strip()]
        unknown_modes = [item for item in raw_restricted if item not in MODES]
        if unknown_modes:
            raise ValueError(f'RESTRICTED_MODES contains unknown modes: {", ".join(unknown_modes)}')
        return cls(
            telegram_token=token, upstream_key=key, owner_id=owner,
            base_url=url, database_path=os.environ.get('DATABASE_PATH','data/proxima.sqlite3'),
            dispatch_enabled=os.environ.get('ENABLE_TASK_DISPATCH','false').lower() == 'true',
            poll_seconds=max(5,int(os.environ.get('POLL_SECONDS','5'))),
            upstream_timeout=max(5,int(os.environ.get('UPSTREAM_TIMEOUT','25'))),
            max_active_external=max_active,
            max_tasks_per_day=max(0,int(os.environ.get('MAX_TASKS_PER_DAY','100'))),
            max_open_tasks=max(0,int(os.environ.get('MAX_OPEN_TASKS',str(DEFAULT_OPEN_TASKS)))),
            timezone_name=timezone_name,
            restricted_modes=tuple(dict.fromkeys(raw_restricted)),
        )
