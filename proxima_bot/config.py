from __future__ import annotations
from dataclasses import dataclass
import os
from urllib.parse import urlsplit
from dotenv import load_dotenv
from zoneinfo import ZoneInfo


def tokora_credentials() -> tuple[str, str]:
    load_dotenv()
    key=os.environ.get('TOKORA_API_KEY','').strip()
    if not key or key.startswith('PUT_'):
        raise ValueError('Set TOKORA_API_KEY in .env')
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
    stars_price: int = 0
    subscription_days: int = 30
    poll_seconds: int = 5
    upstream_timeout: int = 25
    max_active_external: int = 1
    max_tasks_per_day: int = 20
    max_open_tasks: int = 3
    stars_plans: tuple[tuple[int, int], ...] = ()
    timezone_name: str = 'Asia/Jerusalem'

    def plans(self) -> dict[int, int]:
        plans = {self.subscription_days: self.stars_price} if self.stars_price else {}
        plans.update(self.stars_plans)
        return dict(sorted(plans.items()))

    @classmethod
    def from_env(cls) -> 'Config':
        url,key=tokora_credentials()
        token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
        owner = int(os.environ.get('OWNER_ID', '0'))
        if not token or token.startswith('PUT_') or owner <= 0:
            raise ValueError('Set TELEGRAM_BOT_TOKEN and OWNER_ID in .env')
        stars=int(os.environ.get('STARS_30D_PRICE','0'))
        days=int(os.environ.get('SUBSCRIPTION_DAYS','30'))
        if stars<0 or not 1<=days<=365:
            raise ValueError('STARS_30D_PRICE must be nonnegative and SUBSCRIPTION_DAYS in 1..365')
        plans = {}
        for entry in filter(None, os.getenv('STARS_PLANS', '').split(',')):
            try:
                duration, price = map(int, entry.split(':'))
                if duration in plans or not 1 <= duration <= 365 or not 1 <= price <= 100000:
                    raise ValueError
            except ValueError:
                raise ValueError('STARS_PLANS: use unique days:price pairs, e.g. 1:100,7:500') from None
            plans[duration] = price
        timezone_name = os.getenv('BOT_TIMEZONE', 'Asia/Jerusalem').strip()
        ZoneInfo(timezone_name)
        return cls(
            telegram_token=token, upstream_key=key, owner_id=owner,
            base_url=url, database_path=os.environ.get('DATABASE_PATH','data/proxima.sqlite3'),
            dispatch_enabled=os.environ.get('ENABLE_TASK_DISPATCH','false').lower() == 'true',
            stars_price=stars,
            subscription_days=days,
            poll_seconds=max(5,int(os.environ.get('POLL_SECONDS','5'))),
            upstream_timeout=max(5,int(os.environ.get('UPSTREAM_TIMEOUT','25'))),
            max_active_external=1,  # one shared workspace, preserve serialization
            max_tasks_per_day=max(0,int(os.environ.get('MAX_TASKS_PER_DAY','20'))),
            max_open_tasks=max(0,int(os.environ.get('MAX_OPEN_TASKS','3'))),
            stars_plans=tuple(plans.items()),
            timezone_name=timezone_name,
        )
