'''Console gateway: stdin/stdout transport for local operation and dry-run.

When run as a managed service (systemd / launchd with no controlling
tty), ``stdin`` is EOF immediately and the inner loop would otherwise
spin. We treat EOF as "no interactive operator; idle and stay
interruptible". A ``/exit`` command still closes the loop proactively
when an operator does reach the prompt.
'''

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator

from .base import Incoming


class ConsoleGateway:
    '''Reads lines from stdin; tolerates EOF (managed-service mode).

    Lifecycle: ``exit_event`` is set by ``request_shutdown`` so the
    orchestrator's ``run`` task can be stopped cleanly. In service
    mode (EOF on first attempt) we sleep in short intervals and
    yield control to other coroutines instead of blocking a full hour.
    '''

    def __init__(self) -> None:
        self.exit_event = asyncio.Event()

    async def send(self, text: str) -> None:
        print(f'\n[julia] {text}', flush=True)

    def request_shutdown(self) -> None:
        self.exit_event.set()

    async def incoming(self) -> AsyncIterator[Incoming]:
        loop = asyncio.get_running_loop()
        eof_seen = False
        while not self.exit_event.is_set():
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                if not eof_seen:
                    eof_seen = True
                    # No tty (service mode). Sleep a short, interruptible
                    # interval so shutdown signals are honored quickly
                    # and we don't hog the executor.
                    try:
                        await asyncio.wait_for(
                            self.exit_event.wait(), timeout=1.0
                        )
                    except asyncio.TimeoutError:
                        pass
                else:
                    try:
                        await asyncio.wait_for(
                            self.exit_event.wait(), timeout=1.0
                        )
                    except asyncio.TimeoutError:
                        pass
                continue
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.lower() in ('/exit', '/quit'):
                return
            yield Incoming(text=stripped, sender='console')
