"""Stage complete exports on disk before sending bounded Telegram attachments."""
from __future__ import annotations

from asyncio import sleep
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

from .api import Telegram, TelegramError

EXPORT_PART_BYTES = 8 * 1024 * 1024


class ExportParts:
    """Write whole records and repeat the header when a file is split."""

    def __init__(self, filename:str, *, header:bytes=b'', max_bytes:int=EXPORT_PART_BYTES):
        if max_bytes<=0 or len(header)>max_bytes:
            raise ValueError('Некорректный размер части выгрузки.')
        self._filename = Path(filename)
        self._header = header
        self._max_bytes = max_bytes
        self._buffer = bytearray(header)
        self._paths: list[Path] = []

    def __enter__(self):
        self._temp = TemporaryDirectory(prefix='proxima-export-')
        return self

    def __exit__(self, *_):
        self._temp.cleanup()

    def _flush(self)->None:
        path = Path(self._temp.name)/str(len(self._paths)+1)
        path.write_bytes(self._buffer or b'\n')
        self._paths.append(path)
        self._buffer = bytearray(self._header)

    def append(self, record:bytes)->None:
        if len(self._header)+len(record)>self._max_bytes:
            raise ValueError('Одна строка превышает размер файла выгрузки.')
        if len(self._buffer)+len(record)>self._max_bytes:
            self._flush()
        self._buffer.extend(record)

    async def send(self,tg:Telegram,uid:int,caption:str,*,allowed:Callable[[],bool]|None=None)->bool:
        """Send each part once. Return False if access was withdrawn before delivery."""
        if len(self._buffer)>len(self._header) or not self._paths:
            self._flush()
        count = len(self._paths)
        for index,path in enumerate(self._paths,1):
            if allowed is not None and not allowed():
                return False
            name = self._filename.name
            label = caption
            if count>1:
                name = f'{self._filename.stem}_part{index:03d}{self._filename.suffix}'
                label = f'{caption} · часть {index}/{count}'
            data = path.read_bytes()
            try:
                await tg.attachment(uid,data,name,label)
            except TelegramError as exc:
                if exc.code!=429 or not 0<exc.retry_after<=60:
                    raise
                await sleep(exc.retry_after)
                if allowed is not None and not allowed():
                    return False
                await tg.attachment(uid,data,name,label)
        return True
