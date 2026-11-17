"""Persistent cabinet routing and encrypted Tokora credentials."""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from pathlib import Path
from secrets import token_hex

from cryptography.fernet import Fernet, InvalidToken

PRIMARY_WORKSPACE = 'main'
WORKSPACE_ID = re.compile(r'main|[a-f0-9]{12}')
KEY_COMMAND = '/tokora_key'
KEY_FORMAT = re.compile(r'tk_[A-Za-z0-9_-]{12,256}')


def workspace_database(primary_path: str, workspace: str) -> str:
    if not WORKSPACE_ID.fullmatch(workspace):
        raise ValueError('Неверный код кабинета.')
    if workspace == PRIMARY_WORKSPACE or primary_path == ':memory:':
        return primary_path
    path = Path(primary_path)
    return str(path.with_name(f'{path.name}.{workspace}.sqlite3'))


class WorkspaceRegistry:
    def __init__(self, conn: sqlite3.Connection, database_path: str, env_key: str = ''):
        self.conn = conn
        self.database_path = database_path
        self._cipher: Fernet | None = None
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS tokora_workspaces (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, env_enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS tokora_keys (
                id TEXT PRIMARY KEY, workspace TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE,
                secret BLOB NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS tokora_witnesses (
                workspace TEXT NOT NULL, witness TEXT NOT NULL, PRIMARY KEY(workspace, witness)
            );
            CREATE TABLE IF NOT EXISTS tokora_workspace_members (
                workspace TEXT NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY(workspace, user_id)
            );
            CREATE TABLE IF NOT EXISTS tokora_workspace_selection (
                user_id INTEGER PRIMARY KEY, workspace TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tokora_message_routes (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, workspace TEXT NOT NULL,
                PRIMARY KEY(chat_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS tokora_callback_routes (
                id TEXT PRIMARY KEY, workspace TEXT NOT NULL, callback TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tokora_distinct_workspaces (
                first TEXT NOT NULL, second TEXT NOT NULL, PRIMARY KEY(first, second)
            );
            INSERT OR IGNORE INTO tokora_workspaces(id, name) VALUES ('main', 'Основной');
        """)
        self.env_key = self.bind_legacy_key(env_key)

    def bind_legacy_key(self, key: str) -> str:
        if not key:
            return ''
        fingerprint = hashlib.sha256(key.encode()).hexdigest()
        if self.conn.execute('SELECT 1 FROM tokora_keys WHERE fingerprint=?', (fingerprint,)).fetchone():
            return ''
        saved = self.conn.execute("SELECT value FROM metadata WHERE key='tokora_env_fingerprint'").fetchone()
        if saved:
            return key if saved[0] == fingerprint else ''
        if self.conn.execute("SELECT 1 FROM tokora_keys WHERE workspace=? LIMIT 1", (PRIMARY_WORKSPACE,)).fetchone():
            return ''
        if self.witnesses(PRIMARY_WORKSPACE) or self.conn.execute(
                'SELECT 1 FROM tokora_distinct_workspaces WHERE first=? OR second=?',
                (PRIMARY_WORKSPACE, PRIMARY_WORKSPACE)).fetchone():
            return ''
        self.conn.execute("INSERT OR IGNORE INTO metadata VALUES ('tokora_env_fingerprint', ?)", (fingerprint,))
        saved = self.conn.execute("SELECT value FROM metadata WHERE key='tokora_env_fingerprint'").fetchone()
        return key if saved[0] == fingerprint else ''

    def cipher(self) -> Fernet:
        if self._cipher is not None:
            return self._cipher
        if self.database_path == ':memory:':
            secret = Fernet.generate_key()
        else:
            path = Path(self.database_path + '.keys')
            try:
                secret = path.read_bytes()
            except FileNotFoundError:
                if self.conn.execute('SELECT 1 FROM tokora_keys LIMIT 1').fetchone():
                    raise ValueError('Файл шифрования ключей отсутствует. Восстанови его из резервной копии.') from None
                secret = Fernet.generate_key()
                try:
                    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_BINARY', 0)
                    fd = os.open(path, flags, 0o600)
                except FileExistsError:
                    secret = path.read_bytes()
                else:
                    with os.fdopen(fd, 'wb') as file:
                        file.write(secret)
                        file.flush()
                        os.fsync(file.fileno())
        try:
            self._cipher = Fernet(secret)
        except ValueError:
            raise ValueError('Файл шифрования ключей повреждён.') from None
        return self._cipher

    def workspaces(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute('SELECT * FROM tokora_workspaces ORDER BY rowid')]

    def workspace(self, workspace: str) -> dict | None:
        row = self.conn.execute('SELECT * FROM tokora_workspaces WHERE id=?', (workspace,)).fetchone()
        return dict(row) if row else None

    def create_workspace(self, name: str) -> str:
        name = ' '.join(name.split())
        if not 1 <= len(name) <= 36 or any(not ch.isprintable() for ch in name):
            raise ValueError('Название должно быть от 1 до 36 символов.')
        if any(row['name'].casefold() == name.casefold() for row in self.workspaces()):
            raise ValueError('Кабинет с таким названием уже есть.')
        workspace = token_hex(6)
        self.conn.execute('INSERT INTO tokora_workspaces(id, name) VALUES (?, ?)', (workspace, name))
        return workspace

    def keys(self, workspace: str) -> list[dict]:
        rows = [dict(row) for row in self.conn.execute(
            'SELECT id, enabled FROM tokora_keys WHERE workspace=? ORDER BY rowid', (workspace,))]
        if workspace == PRIMARY_WORKSPACE and self.env_key:
            rows.insert(0, {'id': 'env', 'enabled': self.workspace(workspace)['env_enabled']})
        return rows

    def credentials(self, workspace: str) -> dict[str, str]:
        credentials = {}
        if workspace == PRIMARY_WORKSPACE and self.env_key and self.workspace(workspace)['env_enabled']:
            credentials['env'] = self.env_key
        for row in self.conn.execute('SELECT id, secret FROM tokora_keys WHERE workspace=? AND enabled=1',
                                     (workspace,)):
            try:
                credentials[row['id']] = self.cipher().decrypt(row['secret']).decode('ascii')
            except (InvalidToken, UnicodeError):
                raise ValueError('Не удалось расшифровать сохранённые ключи.') from None
        return credentials

    def duplicate_key(self, token: str) -> bool:
        digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        return token == self.env_key or self.conn.execute(
            'SELECT 1 FROM tokora_keys WHERE fingerprint=?', (digest,)).fetchone() is not None

    def store_key(self, workspace: str, token: str, witnesses: frozenset[str],
                  *, distinct_from: tuple[str, ...] = (), new_identity: bool = False) -> str:
        key_id = token_hex(6)
        secret = self.cipher().encrypt(token.encode('ascii'))
        fingerprint = hashlib.sha256(token.encode('ascii')).hexdigest()
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            if new_identity:
                self.conn.execute('DELETE FROM tokora_distinct_workspaces WHERE first=? OR second=?',
                                  (workspace, workspace))
            self.conn.execute('INSERT INTO tokora_keys(id, workspace, fingerprint, secret) VALUES (?, ?, ?, ?)',
                              (key_id, workspace, fingerprint, secret))
            self.conn.executemany('INSERT OR IGNORE INTO tokora_witnesses VALUES (?, ?)',
                                  ((workspace, witness) for witness in witnesses))
            for other in distinct_from:
                self.confirm_distinct(workspace, other)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return key_id

    def witnesses(self, workspace: str) -> set[str]:
        return {row[0] for row in self.conn.execute(
            'SELECT witness FROM tokora_witnesses WHERE workspace=?', (workspace,))}

    def distinct(self, workspace: str, other: str) -> bool:
        return self.conn.execute('SELECT 1 FROM tokora_distinct_workspaces WHERE first=? AND second=?',
                                 sorted((workspace, other))).fetchone() is not None

    def confirm_distinct(self, workspace: str, other: str) -> None:
        self.conn.execute('INSERT OR IGNORE INTO tokora_distinct_workspaces VALUES (?, ?)',
                          sorted((workspace, other)))

    def change_key(self, workspace: str, key: str, action: str) -> None:
        if action not in ('enable', 'disable', 'delete'):
            raise ValueError('Неизвестное действие.')
        if key == 'env' and workspace == PRIMARY_WORKSPACE and self.env_key and action != 'delete':
            self.conn.execute('UPDATE tokora_workspaces SET env_enabled=? WHERE id=?',
                              (action == 'enable', workspace))
        elif key != 'env' and action == 'delete':
            self.conn.execute('DELETE FROM tokora_keys WHERE workspace=? AND id=?', (workspace, key))
        elif key != 'env':
            self.conn.execute('UPDATE tokora_keys SET enabled=? WHERE workspace=? AND id=?',
                              (action == 'enable', workspace, key))

    def allow_member(self, workspace: str, user_id: int) -> None:
        self.conn.execute('INSERT OR IGNORE INTO tokora_workspace_members VALUES (?, ?)', (workspace, user_id))

    def can_open(self, workspace: str, user_id: int, owner_id: int) -> bool:
        if not self.workspace(workspace):
            return False
        return workspace == PRIMARY_WORKSPACE or user_id == owner_id or self.conn.execute(
            'SELECT 1 FROM tokora_workspace_members WHERE workspace=? AND user_id=?',
            (workspace, user_id)).fetchone() is not None

    def selected(self, user_id: int, owner_id: int) -> str:
        row = self.conn.execute('SELECT workspace FROM tokora_workspace_selection WHERE user_id=?',
                                (user_id,)).fetchone()
        return row[0] if row and self.can_open(row[0], user_id, owner_id) else PRIMARY_WORKSPACE

    def select(self, user_id: int, workspace: str) -> None:
        self.conn.execute('INSERT OR REPLACE INTO tokora_workspace_selection VALUES (?, ?)', (user_id, workspace))

    def bind_message(self, chat_id: int, message_id: int, workspace: str) -> None:
        self.conn.execute('INSERT OR REPLACE INTO tokora_message_routes VALUES (?, ?, ?)',
                          (chat_id, message_id, workspace))

    def message_workspace(self, chat_id: int, message_id: int) -> str | None:
        row = self.conn.execute('SELECT workspace FROM tokora_message_routes WHERE chat_id=? AND message_id=?',
                                (chat_id, message_id)).fetchone()
        return row[0] if row else None

    def pack_callback(self, workspace: str, callback: str) -> str:
        token = 'wc:' + hashlib.blake2s(f'{workspace}:{callback}'.encode(), digest_size=16).hexdigest()
        self.conn.execute('INSERT OR IGNORE INTO tokora_callback_routes VALUES (?, ?, ?)',
                          (token, workspace, callback))
        return token

    def callback_route(self, token: str) -> tuple[str, str] | None:
        row = self.conn.execute('SELECT workspace, callback FROM tokora_callback_routes WHERE id=?', (token,)).fetchone()
        return (row[0], row[1]) if row else None
