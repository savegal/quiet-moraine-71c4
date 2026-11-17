"""One pool of credentials for one Tokora cabinet."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from .accounts import ACCOUNT_FOLDER_ID
from .api import ApiError, Tokora
from .operations import VALID_ID

CABINET_REQUESTS_PER_MINUTE = 590


class CabinetBudget:
    def __init__(self):
        self.requests: deque[float] = deque()
        self.blocked_until = 0.0

    def take(self) -> None:
        now = time.monotonic()
        if now < self.blocked_until:
            raise ApiError('rate_limited', 429, self.blocked_until - now, rate_scope='account')
        while self.requests and now - self.requests[0] >= 60:
            self.requests.popleft()
        if len(self.requests) >= CABINET_REQUESTS_PER_MINUTE:
            raise ApiError('rate_limited', 429, self.requests[0] + 60 - now, rate_scope='account')
        self.requests.append(now)


class CabinetKey(Tokora):
    def __init__(self, session, base_url: str, key: str, timeout: int, budget: CabinetBudget):
        super().__init__(session, base_url, key, timeout, admit_shared=budget.take)
        self.budget = budget

    def response_error(self, response, body, fallback):
        error = super().response_error(response, body, fallback)
        if error.status == 429 and error.rate_scope == 'account':
            self.budget.blocked_until = max(self.budget.blocked_until, self.rate_limit_until)
        return error


class CabinetAPI(Tokora):
    def __init__(self, session, base_url: str, timeout: int):
        super().__init__(session, base_url, '', timeout)
        self.budget = CabinetBudget()
        self.clients: dict[str, CabinetKey] = {}
        self.next_key = 0
        self.rejected_until: dict[str, float] = {}

    def set_keys(self, credentials: dict[str, str]) -> None:
        previous = self.clients
        self.clients = {
            key_id: previous[key_id] if key_id in previous and previous[key_id].key == token else
            CabinetKey(self.session, self.base, token, self.timeout, self.budget)
            for key_id, token in credentials.items()
        }
        self.rejected_until = {key_id: until for key_id, until in self.rejected_until.items()
                               if key_id in self.clients and self.clients[key_id] is previous.get(key_id)}

    async def _request(self, operation: str, *args, **kwargs):
        clients = list(self.clients.items())
        if not clients:
            raise ApiError('api_key_invalid', 401)
        start = self.next_key % len(clients)
        self.next_key += 1
        errors = []
        for key_id, client in clients[start:] + clients[:start]:
            if self.clients.get(key_id) is not client or self.rejected_until.get(key_id, 0) > time.monotonic():
                continue
            try:
                return await getattr(client, operation)(*args, **kwargs)
            except ApiError as exc:
                if exc.request_id:
                    self.last_error_request_id = exc.request_id
                # Try the next key only for a rejection that could not execute the operation.
                # A timeout or ambiguous write stays with the existing reconciliation logic.
                if exc.status == 401:
                    self.rejected_until[key_id] = time.monotonic() + 60
                elif exc.status == 403 and exc.code == 'api_scope_denied':
                    pass
                elif exc.status != 429 or exc.rate_scope != 'key':
                    raise
                errors.append(exc)
        if errors:
            throttled = [error for error in errors if error.status == 429]
            if throttled:
                raise min(throttled, key=lambda error: error.retry_after)
            raise errors[-1]
        raise ApiError('api_key_invalid', 401)

    async def call(self, method: str, path: str, **kwargs) -> dict:
        return await self._request('call', method, path, **kwargs)

    async def upload(self, path: str, data: bytes, content_type: str = 'application/octet-stream', **kwargs) -> dict:
        return await self._request('upload', path, data, content_type, **kwargs)

    async def download(self, path: str, params: dict | None = None, max_bytes: int = 15 * 1024 * 1024) -> tuple[bytes, str]:
        return await self._request('download', path, params, max_bytes)


@dataclass(frozen=True)
class CabinetEvidence:
    markers: frozenset[str]
    complete_folders: bool


async def cabinet_witnesses(api: Tokora) -> CabinetEvidence:
    """Use declared read endpoints. The contract exposes no cabinet ID."""
    folders = await api.call('GET', '/v1/account-folders', retries=0)
    tasks = await api.task_page(0, 25)
    items = folders.get('items')
    if not isinstance(items, list) or any(not isinstance(folder, dict)
                                         or not isinstance(folder.get('id'), str) for folder in items):
        raise ApiError('invalid_response')
    witnesses = {f'task:{item["id"]}' for item in tasks['items']}
    for folder in items:
        if ACCOUNT_FOLDER_ID.fullmatch(folder['id']):
            witnesses.add(f'folder:{folder["id"]}:{folder.get("createdAt") or ""}')
    # This endpoint is unpaginated. Its total counts accounts, including accounts outside folders.
    complete = folders.get('available') is True and len({folder['id'] for folder in items}) == len(items)
    return CabinetEvidence(frozenset(witnesses), complete)


async def has_task_witness(api: Tokora, marker: str) -> bool:
    task_id = marker.removeprefix('task:')
    try:
        task = await api.task(task_id)
    except ApiError as exc:
        if exc.status == 404:
            return False
        raise
    if task.get('id') != task_id:
        raise ApiError('invalid_response')
    return True


def task_witnesses(markers: frozenset[str] | set[str]) -> list[str]:
    return sorted(marker for marker in markers
                  if marker.startswith('task:') and VALID_ID.fullmatch(marker.removeprefix('task:')))


async def same_cabinet(left: Tokora, left_evidence: CabinetEvidence,
                       right: Tokora, right_evidence: CabinetEvidence) -> bool | None:
    """Return None when reads do not establish either equality or separation."""
    if left_evidence.markers & right_evidence.markers:
        return True
    for source, evidence, peer, peer_evidence in (
        (left, left_evidence, right, right_evidence), (right, right_evidence, left, left_evidence),
    ):
        tasks = task_witnesses(evidence.markers)
        if tasks:
            if await has_task_witness(peer, tasks[0]):
                return True
            if await has_task_witness(source, tasks[0]):
                return False
        folder_markers = {marker for marker in evidence.markers if marker.startswith('folder:')}
        if folder_markers and peer_evidence.complete_folders:
            peer_again = await cabinet_witnesses(peer)
            observed_again = await cabinet_witnesses(source)
            if observed_again.markers & peer_again.markers:
                return True
            peer_ids = {marker.split(':', 2)[1] for marker in peer_again.markers if marker.startswith('folder:')}
            stable_ids = {marker.split(':', 2)[1] for marker in folder_markers & observed_again.markers}
            if (peer_again.complete_folders and observed_again.complete_folders
                    and stable_ids and stable_ids.isdisjoint(peer_ids)):
                return False
    return None
