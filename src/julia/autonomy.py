'''The autonomy ladder (vision section 5.5).

Five rungs, per repo and globally. The ladder moves down automatically
when anomalies accumulate and is restored from the gateway. Every rung
change is recorded as a decision so it is explainable and reversible.
'''

from __future__ import annotations

from enum import IntEnum

from .state import Store

ANOMALY_DROP_THRESHOLD = 3


class Rung(IntEnum):
    SAFE_MODE = 0
    PROPOSE_ONLY = 1
    SUPERVISED = 2
    AUTO_NOTIFY = 3
    FULL_AUTO = 4


class AutonomyLadder:
    GLOBAL = '__global__'

    def __init__(self, store: Store, default: Rung = Rung.AUTO_NOTIFY) -> None:
        self.store = store
        self.default = default
        self._anomalies: dict[str, int] = {}

    def current(self, repo: str | None = None) -> Rung:
        keys = ([f'rung:{repo}'] if repo else []) + [f'rung:{self.GLOBAL}']
        for key in keys:
            raw = self.store.kv_get(key)
            if raw is not None:
                return Rung(int(raw))
        return self.default

    def set_rung(self, rung: Rung, reason: str, repo: str | None = None) -> None:
        key = repo or self.GLOBAL
        self.store.kv_set(f'rung:{key}', str(int(rung)))
        self.store.record_decision('ladder', f'set_rung:{rung.name}', reason)

    def record_anomaly(self, reason: str, repo: str | None = None) -> Rung:
        '''Count an anomaly; drop one rung after the threshold (then reset).'''
        key = repo or self.GLOBAL
        count = self._anomalies.get(key, 0) + 1
        self._anomalies[key] = count
        current = self.current(repo)
        if count >= ANOMALY_DROP_THRESHOLD and current > Rung.SAFE_MODE:
            self.set_rung(
                Rung(int(current) - 1),
                f'auto-drop after {count} anomalies: {reason}',
                repo,
            )
            self._anomalies[key] = 0
        return self.current(repo)

    def panic(self) -> None:
        '''Single panic-stop reachable from the gateway (vision section 18).'''
        self.set_rung(Rung.SAFE_MODE, 'panic-stop from gateway')

    # Capability checks -------------------------------------------------
    def allows_execution(self, repo: str | None = None) -> bool:
        return self.current(repo) >= Rung.SUPERVISED

    def allows_merge(self, repo: str | None = None) -> bool:
        return self.current(repo) >= Rung.AUTO_NOTIFY
