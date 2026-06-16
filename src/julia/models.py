'''Domain model: the task is the unit of work in the ledger (vision section 18).

Phase 0/1 keeps decomposition simple: one user request maps to one task
and one Jules session. The model leaves room for decomposition later
(tasks already carry their own ids, states and session mappings).
'''

from __future__ import annotations

import enum
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class TaskState(str, enum.Enum):
    QUEUED = 'queued'
    PLANNING = 'planning'
    EXECUTING = 'executing'
    REVIEWING = 'reviewing'
    AWAITING_APPROVAL = 'awaiting_approval'
    MERGED = 'merged'
    FAILED = 'failed'
    ABANDONED = 'abandoned'


@dataclass
class Task:
    id: str
    prompt: str
    repo: str
    state: TaskState = TaskState.QUEUED
    session_id: str | None = None
    pr_url: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    # Step 4: make behaviour PRs discoverable from the ledger so
    # ``/explain``, the daily digest, and ``/approve-behavior`` can
    # all reference them. ``kind='dev'`` is the historical default
    # for ordinary Jules-driven tasks; ``kind='behavior_pr'`` marks
    # ``/improve``-originated entries. ``source_url`` carries the
    # editor's return value verbatim — GitHub html_url for the
    # ``GitHubBehaviorEditor``, a SHA for the ``LocalBehaviorEditor``,
    # ``fake-...:file`` for the ``FakeBehaviorEditor``.
    kind: str = 'dev'
    source_url: str | None = None

    def to_json(self) -> str:
        data = asdict(self)
        data['state'] = self.state.value
        data['created_at'] = self.created_at.isoformat()
        data['updated_at'] = self.updated_at.isoformat()
        return json.dumps(data)

    @classmethod
    def from_json(cls, raw: str) -> 'Task':
        data = json.loads(raw)
        data['state'] = TaskState(data['state'])
        data['created_at'] = datetime.fromisoformat(data['created_at'])
        data['updated_at'] = datetime.fromisoformat(data['updated_at'])
        return cls(**data)
