'''Rolling-window quota guard for the scarce resource (vision sections 4, 18).

Jules allows roughly 100 tasks per rolling 24 hour window. The guard
persists every acquisition in the store so restarts never forget spend,
and always keeps a small reserve for the daily canary task (section 6).
'''

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .state import Store


class QuotaGuard:
    def __init__(
        self,
        store: Store,
        limit: int,
        canary_budget: int = 1,
        window: timedelta = timedelta(hours=24),
    ) -> None:
        self.store = store
        self.limit = limit
        self.canary_budget = canary_budget
        self.window = window

    def used(self) -> int:
        since = datetime.now(timezone.utc) - self.window
        return self.store.quota_used_since(since)

    def remaining(self, *, canary: bool = False) -> int:
        reserve = 0 if canary else self.canary_budget
        return max(0, self.limit - reserve - self.used())

    def try_acquire(self, label: str, *, canary: bool = False) -> bool:
        if self.remaining(canary=canary) <= 0:
            return False
        self.store.add_quota_event(label)
        return True
