"""Proxy inventory and maintenance controls for the owner of the shared cabinet."""
from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
import re
from secrets import token_hex
from urllib.parse import urlsplit

from .accounts import check_status, has_proxy_binding
from .api import ACCOUNT_PAGE_SIZE, ApiError
from .bot import safe_api_error
from .db import now
from .operations import VALID_ID
from .presentation import error_breakdown, h, keyboard, number, pagination, public_system_text, rich_table, table_html

PROXY_LIST = '/v1/proxies'
PROXY_CHECK = '/v1/proxies/check'
ACCOUNT_CHECK = '/v1/accounts/check'
PROXY_MENU = 'px:list:all:0'
PAGE_SIZE = 8
SELECTION_LIMIT = 50
DRAFT_LIFETIME = 300

FILTERS = {'all':'Все', 'valid':'Рабочие', 'failed':'С ошибкой', 'unknown':'Без результата'}
CHECK_FINISHED = {
    'idle':'Ожидает запуска', 'unchecked':'Ожидает запуска',
    'done':'Завершена', 'finished':'Завершена', 'completed':'Завершена',
    'complete':'Завершена', 'success':'Завершена', 'stopped':'Остановлена', 'canceled':'Отменена',
    'cancelled':'Отменена', 'failed':'Ошибка проверки', 'error':'Ошибка проверки',
}


@dataclass(frozen=True)
class ProxyInventory:
    rows: dict[str,dict]
    own_only: bool
    tokora_lines: int


def valid_proxy_id(value:object)->bool:
    return isinstance(value,str) and bool(VALID_ID.fullmatch(value))


def _check_state(status:dict)->str:
    state = status.get('state')
    return state.strip().lower() if isinstance(state,str) else ''


def _require_check_idle(status:dict, label:str)->None:
    state = check_status(status)
    if state=='active':
        raise ValueError(f'Дождись завершения проверки {label}.')
    if state!='idle':
        raise ValueError(f'Не удалось подтвердить, что проверка {label} завершена.')


async def ensure_proxy_workspace_idle(api, *, include_proxy_check:bool = True)->None:
    """Check upstream activity while the caller holds the local maintenance lease."""
    if await api.active_tasks():
        raise ValueError('Дождись завершения активных задач кабинета.')
    _require_check_idle(await api.call('GET',ACCOUNT_CHECK,retries=0), 'аккаунтов')
    if include_proxy_check:
        _require_check_idle(await api.call('GET',PROXY_CHECK,retries=0), 'прокси')
    queue = await api.call('GET','/v1/posts/queue',retries=0)
    if type(queue.get('active')) is not int or queue['active']<0:
        raise ApiError('invalid_response')
    if queue['active']:
        raise ValueError('Дождись завершения активных публикаций.')


def _proxy_status(row:dict)->str:
    value = row.get('status')
    status = value.lower() if isinstance(value,str) else ''
    if status in ('valid','active','ok','working','online','alive','ready','good'):
        return 'valid'
    if status in ('failed','error','invalid','expired','offline','dead','unavailable','broken'):
        return 'failed'
    return 'unknown'


def _proxy_origin(row:dict, own_only:bool)->str:
    return 'Свой' if own_only or row.get('address') else 'Адрес скрыт'


def _country(row:dict)->str:
    country = row.get('countryCode')
    return country.upper() if isinstance(country,str) and re.fullmatch(r'[A-Za-z]{2}',country) else '—'


def _latency(row:dict)->str:
    latency = row.get('latencyMs')
    return f'{number(latency)} мс' if type(latency) is int and latency>=0 else '—'


def _endpoint(value:object)->tuple[str,int]|None:
    if not isinstance(value,str) or not value.strip():
        return None
    value = value.strip()
    if any(ch.isspace() or ord(ch)<32 for ch in value):
        return None
    try:
        parsed = urlsplit(value if '://' in value else '//'+value)
        if parsed.hostname and parsed.port:
            host = parsed.hostname.lower().rstrip('.')
            try:
                return str(ip_address(host)), parsed.port
            except ValueError:
                if re.fullmatch(r'[a-z0-9.-]+',host):
                    return host, parsed.port
    except ValueError:
        pass
    # An imported proxy can use host:port:user:pass. Use only host and port.
    fields = value.split(':',3)
    if (len(fields)==4 and re.fullmatch(r'[A-Za-z0-9.-]+',fields[0])
            and re.fullmatch(r'[0-9]{1,5}',fields[1]) and 0<int(fields[1])<=65535):
        return fields[0].lower().rstrip('.'),int(fields[1])
    return None


def _date(app, stamp:object)->str:
    if type(stamp) is int and 0<stamp<32_503_680_000_000:
        return app.format_date(stamp//1000)
    return '—'


def _check_started_at(status:dict)->int|None:
    stamp = status.get('startedAt')
    return stamp if type(stamp) is int and stamp>0 else None


async def proxy_inventory(api)->ProxyInventory:
    data = await api.call('GET',PROXY_LIST,retries=0)
    items = data.get('items')
    if not isinstance(items,list):
        raise ApiError('invalid_response')
    result = {}
    for item in items:
        if not isinstance(item,dict) or not valid_proxy_id(item.get('id')) or item['id'] in result:
            raise ApiError('invalid_response')
        result[item['id']] = item
    lines = data.get('tokoraLines')
    return ProxyInventory(result, data.get('ownOnly') is True, lines if type(lines) is int and lines>0 else 0)


async def request_proxy_check(api, ids:list[str]|None)->tuple[dict,bool]:
    status = await api.call('GET',PROXY_CHECK,retries=0)
    if check_status(status)=='active':
        return status,False
    _require_check_idle(status,'прокси')
    inventory = await proxy_inventory(api)
    if not inventory.rows and not inventory.tokora_lines:
        raise ValueError('Добавь прокси перед проверкой.')
    if ids is not None and (not ids or not set(ids).issubset(inventory.rows)):
        raise ValueError('Список прокси изменился. Выбери записи заново.')
    payload = {'ids':ids} if ids is not None else {}
    result = await api.call('POST',PROXY_CHECK,payload=payload,retries=0)
    return result,True


async def account_for_proxy(api, aid:str)->dict:
    result = await api.call('GET','/v1/accounts/'+aid,retries=0)
    if result.get('accountId')!=aid:
        raise ApiError('invalid_response')
    return result


def proxy_assignee(db, aid:str)->int|None:
    return db.account_assignee(aid)


async def bind_account_proxy(api, db, aid:str, proxy_id:str|None, expected_assignee:int|None)->dict:
    if not valid_proxy_id(aid) or (proxy_id is not None and not valid_proxy_id(proxy_id)):
        raise ValueError('Проверь идентификаторы аккаунта и прокси.')
    with db.account_action(aid):
        await account_for_proxy(api,aid)
        if proxy_id is not None:
            inventory = await proxy_inventory(api)
            if proxy_id not in inventory.rows:
                raise ValueError('Прокси больше нет в списке.')
        await ensure_proxy_workspace_idle(api,include_proxy_check=False)
        if proxy_assignee(db,aid)!=expected_assignee:
            raise ValueError('Назначение аккаунта изменилось. Открой актуальную карточку.')
        body = {'proxyId':proxy_id} if proxy_id is not None else {'proxy':None}
        result = await api.call('PUT',f'/v1/accounts/{aid}/proxy',payload=body,retries=0)
        if result.get('accountId')!=aid:
            raise ApiError('invalid_response')
        return result


async def ensure_proxies_unbound(api, inventory:dict[str,dict], ids:list[str])->None:
    """Check documented account proxy values, refusing ambiguous bindings."""
    values = [inventory[pid].get('address') for pid in ids]
    endpoints = {_endpoint(value) for value in values}
    if None in endpoints:
        raise ValueError('У этих прокси адрес скрыт или не распознан. Проверь привязки в кабинете.')
    raw_values = {value.strip() for value in values}
    selected = set(ids)
    offset = 0
    seen = set()
    total = None
    while total is None or offset<total:
        page = await api.account_page(offset,ACCOUNT_PAGE_SIZE)
        items, page_total = page.get('accounts'), page.get('total')
        if (type(page_total) is not int or page_total<offset or not isinstance(items,list)
                or len(items)>ACCOUNT_PAGE_SIZE or offset+len(items)>page_total
                or (offset<page_total and not items)):
            raise ApiError('invalid_response')
        if total is not None and total!=page_total:
            raise ValueError('Список аккаунтов изменился. Проверь привязки и повтори удаление.')
        total = page_total
        for item in items:
            if not isinstance(item,dict) or not valid_proxy_id(item.get('accountId')):
                raise ApiError('invalid_response')
            aid = item['accountId']
            if aid in seen:
                raise ValueError('Список аккаунтов изменился. Проверь привязки и повтори удаление.')
            seen.add(aid)
            pid = item.get('proxyId')
            if pid is not None:
                if not valid_proxy_id(pid):raise ApiError('invalid_response')
                if pid in selected:
                    raise ValueError(f'Прокси привязан к аккаунту {aid}. Сначала измени или сними привязку.')
                continue
            if 'proxy' not in item:
                raise ApiError('invalid_response')
            value = item['proxy']
            if value is None or value=='':
                if 'proxyId' in item or not item.get('proxySource'):
                    continue
                raise ValueError('Часть привязок скрыта. Проверь эти прокси в кабинете.')
            if not isinstance(value,str):
                raise ApiError('invalid_response')
            value = value.strip()
            endpoint = _endpoint(value)
            if value in selected or value in raw_values or endpoint in endpoints:
                raise ValueError(f'Прокси привязан к аккаунту {aid}. Сначала измени или сними привязку.')
            if endpoint is None and value not in inventory:
                raise ValueError('Привязка пришла в неизвестном формате. Проверь её в кабинете.')
        offset += len(items)


class ProxyConsoleMixin:
    async def proxies(self, uid:int)->None:
        if self.owner(uid):
            return await self._proxy_list(uid,'all',0)
        if not self.db.is_active(uid,self.cfg.owner_id):
            return await self.access(uid)
        accounts = await self.assigned_accounts(self.db.account_ids(uid))
        if not self.db.is_active(uid,self.cfg.owner_id):
            return await self.access(uid)
        assigned = set(self.db.account_ids(uid))
        available = [row for aid,row in accounts.items() if aid in assigned]
        rows = [('Назначено аккаунтов',number(len(assigned))),
                ('С привязкой прокси',number(sum(has_proxy_binding(row) for row in available))),
                ('Аккаунтов в общем пуле',number(sum(not has_proxy_binding(row) and row.get('proxySource')=='tokora' for row in available))),
                ('Активных сессий',number(sum(row.get('status')=='active' for row in available))),
                ('Данные недоступны',number(len(assigned)-len(available)))]
        rich,plain = table_html('◈ Мои прокси',rows,'Прокси и привязки настраивает администратор.')
        await self.tg.rich(uid,rich,plain,keyboard([('◇ Мои аккаунты','accounts',None)],[('↶ Обзор','home',None)]))

    async def _proxy_list(self, uid:int, status:str, page:int, selection:dict|None = None)->None:
        inventory = await proxy_inventory(self.api)
        if not inventory.rows:
            if selection:
                self.db.clear_draft(uid)
            summary = [('Записей с ID','0')]
            if inventory.tokora_lines:
                summary.insert(0,('Линии общего пула',number(inventory.tokora_lines)))
                caption = ('Кабинет показывает объём общего пула. Статусы отдельных линий недоступны. '
                           'Результаты проверки смотри в разделе «Ход проверки».')
                check = [('✓ Запустить проверку','px:check','primary')]
            else:
                caption = 'В кабинете пока нет прокси. Можно добавить их через импорт.'
                check = []
            rich,plain = table_html('◈ Прокси кабинета',summary,caption)
            return await self.tg.rich(uid,rich,plain,self.owner_keyboard(
                check+[('↻ Ход проверки','px:status',None)],
                [('Остаток трафика','px:balance',None)],
                [('↑ Импорт / изменение','px:cli',None)],[('↶ Обзор','home',None)]))
        counts = {key:0 for key in FILTERS if key!='all'}
        entries = []
        for pid,row in inventory.rows.items():
            state = _proxy_status(row)
            counts[state] += 1
            if status in ('all',state):
                entries.append((pid,row,state))
        page = min(page,max(0,(len(entries)-1)//PAGE_SIZE))
        visible = entries[page*PAGE_SIZE:(page+1)*PAGE_SIZE]
        selected = set(selection['ids']) if selection else set()
        if selection:
            token = selection['token']
            prefix = f'px:selection:{token}:'
            title = '◈ Выбор прокси'
            summary = [('Выбрано',number(len(selected))),('В этом списке',number(len(entries)))]
        else:
            prefix = 'px:list:'
            title = '◈ Прокси кабинета'
            summary = [('Записей с ID',number(len(inventory.rows)))]
            if inventory.tokora_lines:
                summary.append(('Линии общего пула',number(inventory.tokora_lines)))
            summary.extend((FILTERS[state],number(value)) for state,value in counts.items())
        rich,plain = table_html(title,summary,f'{FILTERS[status]} · страница {page+1}')
        if visible:
            details = [(pid[:22],FILTERS[state],_country(row),_latency(row)) for pid,row,state in visible]
            rich += rich_table(('ID','Статус','Страна','Задержка'),details)
            plain += '\n\n'+'\n'.join(f'├ <code>{h(pid)}</code> · {h(state)} · {h(country)} · {h(latency)}'
                                      for pid,state,country,latency in details)
        else:
            empty = 'В этом списке пока нет прокси.'
            rich += '<p>'+h(empty)+'</p>'
            plain += '\n\n'+h(empty)
        buttons = []
        for pid,_,state in visible:
            if selection:
                marker = '✓' if pid in selected else '○'
                action = f'px:toggle:{token}:{status}:{page}:{pid}'
            else:
                marker = {'valid':'✓','failed':'!','unknown':'○'}[state]
                action = 'px:item:'+pid
            buttons.append([(f'{marker} {pid[:30]}',action,None)])
        buttons.append(pagination(f'{prefix}{status}:',page,(page+1)*PAGE_SIZE<len(entries)))
        filters = [(label,f'{prefix}{key}:0','primary' if key==status else None) for key,label in FILTERS.items()]
        buttons.extend((filters[:2],filters[2:]))
        if selection:
            if selected:
                buttons.extend(([('✓ Проверить выбранные','px:check_selected:'+token,'success')],
                                [('× Удалить выбранные','px:delete_selected:'+token,'danger')]))
            buttons.append([('↶ Отменить выбор','px:cancel:'+token,None)])
        else:
            buttons.extend(([('✓ Проверить все','px:check','primary'),('↻ Ход проверки','px:status',None)],
                            [('Остаток трафика','px:balance',None)],
                            [('▤ Выбрать несколько','px:select',None),('↑ Импорт / изменение','px:cli',None)],
                            [('↶ Обзор','home',None)]))
        await self.tg.rich(uid,rich,plain,self.owner_keyboard(*buttons))

    async def show_proxy_balance(self,uid:int)->None:
        data = await self.api.call('GET','/v1/proxies/balance',retries=0)
        statuses = {'ok':'Доступен','low':'Заканчивается','exhausted':'Исчерпан','none':'Нет пакета'}
        status = data.get('status')
        if type(data.get('hasPackage')) is not bool or not isinstance(status,str) or status not in statuses:
            raise ApiError('invalid_response')
        rows = [('Пакет трафика',statuses[status])]
        if data['hasPackage']:
            for key,label in (('remainingBytes','Осталось'),('usedBytes','Использовано'),('limitBytes','Объём пакета')):
                amount = data.get(key)
                if type(amount) is not int or amount<0:raise ApiError('invalid_response')
                value = f'{amount/1_000_000_000:.2f} ГБ' if amount>=1_000_000_000 else f'{amount/1_000_000:.2f} МБ'
                rows.append((label,value.replace('.',',')))
        rich,plain = table_html('◈ Трафик прокси',rows,
            'Общий остаток по купленным пакетам. Трафик не сгорает со временем. Данные обновляются раз в 30 секунд.')
        await self.tg.rich(uid,rich,plain,self.owner_keyboard(
            [('↻ Обновить','px:balance',None)],[('↶ Прокси',PROXY_MENU,None)]))

    async def _proxy_card(self, uid:int, pid:str)->None:
        inventory = await proxy_inventory(self.api)
        row = inventory.rows.get(pid)
        if row is None:
            raise ValueError('Прокси больше нет в списке. Обнови раздел.')
        values = [
            ('ID',pid),('Статус',FILTERS[_proxy_status(row)]),('Источник',_proxy_origin(row,inventory.own_only)),
            ('Страна',_country(row)),('Задержка',_latency(row)),('Проверен',_date(self,row.get('checkedAt'))),
            ('Реквизиты','Настроены' if row.get('hasCredentials') is True else 'Нет данных'),
        ]
        rich,plain = table_html('◈ Карточка прокси',values)
        remove = [('× Удалить','px:delete:'+pid,'danger')] if _endpoint(row.get('address')) else []
        await self.tg.rich(uid,rich,plain,self.owner_keyboard(
            [('✓ Проверить','px:check_one:'+pid,'primary')],remove,[('↶ Прокси',PROXY_MENU,None)]))

    @staticmethod
    def proxy_check_active(status:dict)->bool:
        state = check_status(status)
        if state=='active':
            return True
        if state=='idle':
            return False
        checked,total = status.get('checked'),status.get('total')
        return bool(status.get('startedAt') and type(checked) is int and type(total) is int and 0<=checked<total)

    def render_proxy_check(self, status:dict)->tuple[str,str,dict]:
        active = self.proxy_check_active(status)
        state = 'В работе' if active else CHECK_FINISHED.get(_check_state(status),'Состояние уточняется')
        rows = [('Статус',state),('Проверено',f'{number(status.get("checked"))} / {number(status.get("total"))}')]
        for key,label in (('valid','Рабочие'),('failed','Ошибки'),('expired','Недоступны'),
                          ('waiting','Ожидают'),('averageMs','Среднее время · мс')):
            if type(status.get(key)) is int:
                rows.append((label,number(status[key])))
        started = _date(self,status.get('startedAt'))
        if started!='—':
            rows.append(('Начата',started))
        errors = status.get('errors')
        if isinstance(errors,dict):
            grouped = error_breakdown([{'code':code,'count':amount} for code,amount in errors.items()])
            if grouped:
                rows.append(('Причины',' · '.join(f'{label} {number(total)}' for label,total in grouped.items())))
        caption = 'Прогресс обновляется автоматически.' if active else 'Здесь показана сводка последней проверки.'
        rich,plain = table_html('✓ Проверка прокси',rows,caption)
        if active:
            controls = [('■ Остановить','px:stop','danger')] if _check_started_at(status) is not None else []
            if not controls:
                note = 'Для остановки этой проверки открой кабинет.'
                rich += f'<p>{note}</p>'
                plain += '\n\n'+note
        else:
            controls = [('✓ Проверить все','px:check','primary')]
        return rich,plain,keyboard(controls,[('↻ Обновить','px:status',None)],[('↶ Прокси',PROXY_MENU,None)])

    async def show_proxy_check(self, uid:int)->None:
        if not self.owner(uid):
            return
        status = await self.api.call('GET',PROXY_CHECK,retries=0)
        await self.live_cards.open_proxy_check(uid,status)

    async def start_proxy_check(self, uid:int, ids:list[str]|None = None)->None:
        if not self.owner(uid):
            return
        with self.db.account_action(None):
            result,started = await request_proxy_check(self.api,ids)
        if started:
            self.db.log(uid,'proxies_check',str(len(ids)) if ids is not None else 'all')
        await self.live_cards.open_proxy_check(uid,result)

    async def prompt_stop_proxy_check(self, uid:int)->None:
        if not self.owner(uid):
            return
        status = await self.api.call('GET',PROXY_CHECK,retries=0)
        if not self.proxy_check_active(status):
            return await self.live_cards.open_proxy_check(uid,status)
        started_at = _check_started_at(status)
        if started_at is None:
            return await self.live_cards.open_proxy_check(uid,status)
        await self._proxy_confirmation(uid,'stop',{'started_at':started_at},
                                       'Остановить текущую проверку прокси?',danger=True)

    async def proxy_cli_help(self, uid:int)->None:
        if not self.owner(uid):
            return
        await self.say(uid,'<b>↑ Импорт и изменение прокси</b>\n\n'
            'Выполни команду из папки бота и укажи путь к файлу. Для импорта используй одну запись на строку.\n\n'
            '<code>python -m proxima_bot.manage import-proxies "C:/path/to/proxies.txt"</code>\n\n'
            'Для замены реквизитов укажи ID из карточки и файл с одной строкой.\n\n'
            '<code>python -m proxima_bot.manage edit-proxy PROXY_ID "C:/path/to/proxy.txt"</code>',
            keyboard([('↶ Прокси',PROXY_MENU,None)]))

    def _proxy_draft(self, uid:int, token:str, step:str)->dict:
        draft = self.db.draft(uid)
        if (not draft or draft['mode']!='proxy_console' or draft['step']!=step
                or draft['data'].get('token')!=token or now()-draft['updated_at']>DRAFT_LIFETIME):
            raise ValueError('Экран устарел. Открой раздел и повтори действие.')
        return draft['data']

    async def _proxy_confirmation(self, uid:int, action:str, data:dict, text:str, *, danger:bool = False)->None:
        token = token_hex(8)
        self.db.save_draft(uid,'proxy_console','confirm',{**data,'token':token,'action':action})
        await self.say(uid,'<b>Подтверждение</b>\n\n'+text,keyboard(
            [('✓ Подтвердить','px:confirm:'+token,'danger' if danger else 'primary')],
            [('↶ Отмена','px:cancel:'+token,None)]))

    async def _prompt_delete_proxies(self, uid:int, ids:list[str])->None:
        inventory = (await proxy_inventory(self.api)).rows
        if not ids or not set(ids).issubset(inventory):
            raise ValueError('Список прокси изменился. Выбери записи заново.')
        if any(not _endpoint(inventory[pid].get('address')) for pid in ids):
            raise ValueError('У части выбранных прокси адрес скрыт. Их удаление доступно в кабинете.')
        shown = ', '.join(h(pid[:32]) for pid in ids[:5])
        remainder = f' и ещё {len(ids)-5}' if len(ids)>5 else ''
        await self._proxy_confirmation(uid,'delete',{'ids':ids},
            f'Удалить прокси ({number(len(ids))})?\n<code>{shown}</code>{remainder}\n\n'
            'Перед удалением проверю активные задачи и привязки аккаунтов.',danger=True)

    async def _proxy_bind_picker(self, uid:int, aid:str, page:int)->None:
        account = await account_for_proxy(self.api,aid)
        if self.db.account_in_use(aid):
            raise ValueError('Аккаунт участвует в задаче. Дождись завершения.')
        inventory = await proxy_inventory(self.api)
        proxies = sorted(inventory.rows, key=lambda pid: _proxy_status(inventory.rows[pid])!='valid')
        page = min(page,max(0,(len(proxies)-1)//PAGE_SIZE))
        rows = []
        for pid in proxies[page*PAGE_SIZE:(page+1)*PAGE_SIZE]:
            proxy = inventory.rows[pid]
            title = f'{pid[:23]} · {FILTERS[_proxy_status(proxy)]} · {_proxy_origin(proxy,inventory.own_only)}'
            rows.append([(title,f'px:bind_to:{aid}:{pid}',None)])
        rows.append(pagination(f'px:bind:{aid}:',page,(page+1)*PAGE_SIZE<len(proxies)))
        if has_proxy_binding(account):
            rows.append([('× Снять привязку','px:unbind:'+aid,'danger')])
        rows.append([('↶ Аккаунт','account:'+aid,None)])
        text = f'<b>◈ Прокси аккаунта</b>\n\nАккаунт <code>{h(aid)}</code> · страница {page+1}\n\n'
        if proxies:
            text += 'Выбери прокси из кабинета.'
        elif inventory.tokora_lines:
            text += 'Доступен общий пул. Его линии показаны без отдельных ID.'
        else:
            text += 'В кабинете пока нет доступных прокси.'
        await self.say(uid,text,self.owner_keyboard(*rows))

    async def set_account_proxy(self, uid:int, aid:str, proxy_id:str|None)->None:
        """Show a one-use confirmation for both the button flow and /accountproxy."""
        if not self.owner(uid):
            return
        if not valid_proxy_id(aid) or (proxy_id is not None and not valid_proxy_id(proxy_id)):
            raise ValueError('Проверь идентификаторы аккаунта и прокси.')
        await account_for_proxy(self.api,aid)
        if self.db.account_in_use(aid):
            raise ValueError('Аккаунт участвует в задаче. Дождись завершения.')
        if proxy_id is not None:
            inventory = (await proxy_inventory(self.api)).rows
            if proxy_id not in inventory:
                raise ValueError('Прокси больше нет в списке.')
        action = f'Привязать прокси <code>{h(proxy_id)}</code>' if proxy_id is not None else 'Снять привязку прокси'
        await self._proxy_confirmation(uid,'bind',
            {'aid':aid,'proxy_id':proxy_id,'assignee':proxy_assignee(self.db,aid)},
            f'{action} для аккаунта <code>{h(aid)}</code>?',danger=proxy_id is None)

    async def _apply_proxy_confirmation(self, uid:int, token:str)->None:
        data = self._proxy_draft(uid,token,'confirm')
        self.db.clear_draft(uid)
        if data['action']=='stop':
            with self.db.account_action(None,allow_in_flight=True):
                status = await self.api.call('GET',PROXY_CHECK,retries=0)
                active = self.proxy_check_active(status)
                if active:
                    if _check_started_at(status)!=data['started_at']:
                        raise ValueError('Запущена другая проверка. Открой её карточку.')
                    await self.api.call('DELETE',PROXY_CHECK,retries=0)
            if not active:
                return await self.live_cards.open_proxy_check(uid,status)
            self.db.log(uid,'proxies_check_stop')
            return await self.show_proxy_check(uid)
        if data['action']=='bind':
            aid,proxy_id = data['aid'],data['proxy_id']
            result = await bind_account_proxy(self.api,self.db,aid,proxy_id,data['assignee'])
            self.db.log(uid,'account_proxy_changed',aid)
            return await self.account_info(uid,aid,result)
        if data['action']=='delete':
            await self._delete_proxies(uid,data['ids'])

    async def _delete_proxies(self, uid:int, ids:list[str])->None:
        with self.db.account_action(None):
            if self.db.account_check_pending():
                raise ValueError('Проверка аккаунтов ещё выполняется или ожидает подтверждения.')
            inventory = (await proxy_inventory(self.api)).rows
            if not ids or not set(ids).issubset(inventory):
                raise ValueError('Список прокси изменился. Выбери записи заново.')
            await ensure_proxies_unbound(self.api,inventory,ids)
            await ensure_proxy_workspace_idle(self.api)
            await self.api.call('DELETE',PROXY_LIST,payload={'ids':ids},retries=0)
        self.db.log(uid,'proxies_delete',str(len(ids)))
        await self.say(uid,f'✓ Удалено прокси: <b>{number(len(ids))}</b>.',keyboard([('↶ Прокси',PROXY_MENU,None)]))

    async def proxy_console_callback(self, uid:int, data:str)->bool:
        if not data.startswith('px:'):
            return False
        if not self.owner(uid):
            return True
        _,action,*args = data.split(':')
        try:
            await self._proxy_action(uid,action,args)
        except ApiError as exc:
            message = safe_api_error(exc.code)
            if exc.status==403:
                message = 'У ключа API нет права на эту операцию или на одну из проверок перед ней.'
            if exc.request_id:
                message += f'\nID запроса · <code>{h(public_system_text(exc.request_id))}</code>'
            await self.say(uid,message,keyboard([('↶ Прокси',PROXY_MENU,None)]))
        except ValueError as exc:
            await self.say(uid,h(exc),keyboard([('↶ Прокси',PROXY_MENU,None)]))
        return True

    async def _proxy_action(self, uid:int, action:str, args:list[str])->None:
        if not args:
            handlers = {'status':self.show_proxy_check,'check':self.start_proxy_check,
                        'stop':self.prompt_stop_proxy_check,'cli':self.proxy_cli_help,'balance':self.show_proxy_balance}
            if action in handlers:
                return await handlers[action](uid)
        if action=='list' and len(args)==2 and args[0] in FILTERS and re.fullmatch(r'[0-9]{1,5}',args[1]):
            return await self._proxy_list(uid,args[0],int(args[1]))
        if action in ('item','check_one','delete','unbind') and len(args)==1 and valid_proxy_id(args[0]):
            pid = args[0]
            if action=='item':
                return await self._proxy_card(uid,pid)
            if action=='check_one':
                return await self.start_proxy_check(uid,[pid])
            if action=='delete':
                return await self._prompt_delete_proxies(uid,[pid])
            return await self.set_account_proxy(uid,pid,None)
        if action=='bind' and len(args) in (1,2) and valid_proxy_id(args[0]):
            if len(args)==1 or re.fullmatch(r'[0-9]{1,5}',args[1]):
                return await self._proxy_bind_picker(uid,args[0],int(args[1]) if len(args)==2 else 0)
        if action=='bind_to' and len(args)==2 and all(valid_proxy_id(arg) for arg in args):
            return await self.set_account_proxy(uid,args[0],args[1])
        if action=='confirm' and len(args)==1:
            return await self._apply_proxy_confirmation(uid,args[0])
        if action=='cancel' and len(args)==1:
            draft = self.db.draft(uid)
            if draft and draft['mode']=='proxy_console' and draft['data'].get('token')==args[0]:
                self.db.clear_draft(uid)
            return await self.proxies(uid)
        await self._proxy_selection(uid,action,args)

    async def _proxy_selection(self, uid:int, action:str, args:list[str])->None:
        if action=='select' and not args:
            data = {'token':token_hex(8),'ids':[]}
            self.db.save_draft(uid,'proxy_console','select',data)
            return await self._proxy_list(uid,'all',0,data)
        if action in ('selection','toggle') and len(args)==(4 if action=='toggle' else 3):
            token,status,page = args[:3]
            if status not in FILTERS or not re.fullmatch(r'[0-9]{1,5}',page):
                return
            data = self._proxy_draft(uid,token,'select')
            if action=='toggle':
                pid = args[3]
                if not valid_proxy_id(pid):
                    return
                if pid in data['ids']:
                    data['ids'].remove(pid)
                elif len(data['ids'])<SELECTION_LIMIT:
                    data['ids'].append(pid)
                else:
                    raise ValueError(f'За один раз можно выбрать до {SELECTION_LIMIT} прокси.')
                self.db.save_draft(uid,'proxy_console','select',data)
            return await self._proxy_list(uid,status,int(page),data)
        if action in ('check_selected','delete_selected') and len(args)==1:
            data = self._proxy_draft(uid,args[0],'select')
            if action=='delete_selected':
                return await self._prompt_delete_proxies(uid,data['ids'])
            self.db.clear_draft(uid)
            await self.start_proxy_check(uid,data['ids'])
