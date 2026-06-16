'''Tiered supervision (vision section 5.3).

Tier 1 (OS supervisor) lives in deploy/ as launchd and systemd units
that restart the process on crash and on boot. Tier 2 is this
in-process watchdog: components beat a heartbeat; stalled task runners
are reported and cleared. Tier 3 is the external dead-man switch: we
ping a heartbeat URL on a schedule, and if the pings stop the external
service alerts the owner -- because a dead machine cannot report its
own death.
'''

from __future__ import annotations

import asyncio
import time

import httpx

from .config import Settings
from .gateway.base import Gateway


class Watchdog:
    def __init__(self, settings: Settings, gateway: Gateway) -> None:
        self.settings = settings
        self.gateway = gateway
        self._beats: dict[str, float] = {}

    def beat(self, component: str) -> None:
        self._beats[component] = time.monotonic()

    def clear(self, component: str) -> None:
        self._beats.pop(component, None)

    def stalled(self) -> list[str]:
        now = time.monotonic()
        return [
            component
            for component, beat in self._beats.items()
            if component.startswith('task:') and now - beat > self.settings.stall_timeout_s
        ]

    async def run(self) -> None:
        while True:
            await self._ping_external()
            for component in self.stalled():
                self.clear(component)
                await self.gateway.send(
                    f'Watchdog: {component} stopped reporting and looks stalled.'
                )
            await asyncio.sleep(self.settings.heartbeat_interval_s)

    async def _ping_external(self) -> None:
        url = self.settings.heartbeat_url
        if not url:
            return
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.get(url)
        except httpx.HTTPError:
            # Silence here is the signal: the external service alerts
            # the owner when pings stop arriving.
            pass
