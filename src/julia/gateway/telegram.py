'''Telegram gateway: a thin layer on an existing transport (vision section 12).

Security: only messages from the single configured chat id are accepted;
everything else is silently dropped. The bot token grants no host access
beyond delivering text to the orchestrator, which still enforces the
autonomy ladder and panic-stop on every command.
'''

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx

from .base import Incoming


class TelegramGateway:
    def __init__(self, token: str, chat_id: str) -> None:
        self._base = f'https://api.telegram.org/bot{token}'
        self._chat_id = str(chat_id)
        self._offset = 0

    async def send(self, text: str) -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(
                f'{self._base}/sendMessage',
                json={'chat_id': self._chat_id, 'text': text[:4000]},
            )

    async def incoming(self) -> AsyncIterator[Incoming]:
        async with httpx.AsyncClient(timeout=70.0) as client:
            while True:
                try:
                    response = await client.get(
                        f'{self._base}/getUpdates',
                        params={'timeout': 60, 'offset': self._offset},
                    )
                    updates = response.json().get('result', [])
                except httpx.HTTPError:
                    await asyncio.sleep(5)
                    continue
                for update in updates:
                    self._offset = int(update['update_id']) + 1
                    message = update.get('message') or {}
                    chat = message.get('chat') or {}
                    if str(chat.get('id')) != self._chat_id:
                        continue  # only the owner may control the host
                    text = message.get('text')
                    if text:
                        yield Incoming(text=str(text), sender=self._chat_id)
