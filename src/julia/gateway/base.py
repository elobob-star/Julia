'''Gateway contract plus an in-memory implementation for tests/embedding.

The intelligence lives on the host; a gateway is only a transport that
delivers owner messages in and notifications out (vision section 12).
'''

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass
class Incoming:
    text: str
    sender: str


class Gateway(Protocol):
    async def send(self, text: str) -> None: ...

    def incoming(self) -> AsyncIterator[Incoming]: ...


class MemoryGateway:
    '''In-memory gateway used by the test suite and programmatic embedding.'''

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._queue: asyncio.Queue[Incoming] = asyncio.Queue()

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def push(self, text: str, sender: str = 'user') -> None:
        await self._queue.put(Incoming(text=text, sender=sender))

    async def incoming(self) -> AsyncIterator[Incoming]:
        while True:
            yield await self._queue.get()
