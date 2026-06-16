'''Durable state: task ledger, decision traces, quota events, key/value.

SQLite, simple and boring by design (ADR-0002). Survives restarts so a
reboot never orphans work (vision section 14); backed up and restored
with a single documented command via the sqlite backup API.

Decision traces power the gateway command /explain (vision section 13):
every consequential action records who acted, what they did and why.
'''

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from pathlib import Path

from .models import Task, TaskState

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  data TEXT NOT NULL,
  state TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  task_id TEXT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  reason TEXT NOT NULL,
  meta TEXT
);
CREATE TABLE IF NOT EXISTS quota_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  label TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
'''


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if self.path.name != ':memory:':
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Forward-only schema migrations.

        Each ALTER TABLE is idempotent: a "duplicate column" error
        means we're already at the target schema, and the migration is
        a no-op (sqlite raises ``OperationalError``). Boring, automatic,
        safe to run on every startup (vision section 14).
        """
        migrations: list[tuple[str, type[Exception]]] = [
            (
                "ALTER TABLE decisions ADD COLUMN meta TEXT",
                sqlite3.OperationalError,
            ),
        ]
        for ddl, exc_type in migrations:
            try:
                self._conn.execute(ddl)
                self._conn.commit()
            except exc_type:
                # Duplicate column: already migrated; safe to ignore.
                pass

    # Task ledger ------------------------------------------------------
    def save_task(self, task: Task) -> None:
        task.updated_at = datetime.now(timezone.utc)
        self._conn.execute(
            'INSERT INTO tasks (id, data, state, updated_at) VALUES (?, ?, ?, ?) '
            'ON CONFLICT(id) DO UPDATE SET data=excluded.data, state=excluded.state, '
            'updated_at=excluded.updated_at',
            (task.id, task.to_json(), task.state.value, _utcnow()),
        )
        self._conn.commit()

    def get_task(self, task_id: str) -> Task | None:
        row = self._conn.execute('SELECT data FROM tasks WHERE id = ?', (task_id,)).fetchone()
        return Task.from_json(row[0]) if row else None

    def list_tasks(self, *states: TaskState) -> list[Task]:
        if states:
            marks = ','.join('?' for _ in states)
            query = 'SELECT data FROM tasks WHERE state IN (' + marks + ') ORDER BY updated_at'
            rows = self._conn.execute(query, [s.value for s in states]).fetchall()
        else:
            rows = self._conn.execute('SELECT data FROM tasks ORDER BY updated_at').fetchall()
        return [Task.from_json(r[0]) for r in rows]

    # Decision traces ---------------------------------------------------
    def record_decision(
        self,
        actor: str,
        action: str,
        reason: str,
        task_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        from .behavior.editor import filter_meta

        clean_meta = filter_meta(meta) if meta else None
        meta_json = json.dumps(clean_meta) if clean_meta is not None else None
        self._conn.execute(
            'INSERT INTO decisions (at, task_id, actor, action, reason, meta) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (_utcnow(), task_id, actor, action, reason, meta_json),
        )
        self._conn.commit()

    def decisions_for(self, task_id: str) -> list[tuple[str, str, str, str, dict | None]]:
        rows = self._conn.execute(
            'SELECT at, actor, action, reason, meta FROM decisions WHERE task_id = ? ORDER BY id',
            (task_id,),
        ).fetchall()
        results: list[tuple[str, str, str, str, dict | None]] = []
        for at, actor, action, reason, raw_meta in rows:
            parsed = json.loads(raw_meta) if raw_meta else None
            results.append((at, actor, action, reason, parsed))
        return results

    # Quota events ------------------------------------------------------
    def add_quota_event(self, label: str) -> None:
        self._conn.execute(
            'INSERT INTO quota_events (at, label) VALUES (?, ?)', (_utcnow(), label)
        )
        self._conn.commit()

    def quota_used_since(self, since: datetime) -> int:
        row = self._conn.execute(
            'SELECT COUNT(*) FROM quota_events WHERE at >= ?', (since.isoformat(),)
        ).fetchone()
        return int(row[0])

    # Key/value ----------------------------------------------------------
    def kv_get(self, key: str) -> str | None:
        row = self._conn.execute('SELECT value FROM kv WHERE key = ?', (key,)).fetchone()
        return str(row[0]) if row else None

    def kv_set(self, key: str, value: str) -> None:
        self._conn.execute(
            'INSERT INTO kv (key, value) VALUES (?, ?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (key, value),
        )
        self._conn.commit()

    # Backup / restore ----------------------------------------------------
    def backup(self, dest: Path | str) -> Path:
        destination = Path(dest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(destination))
        with target:
            self._conn.backup(target)
        target.close()
        return destination
