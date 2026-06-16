from datetime import datetime, timedelta, timezone

from julia.models import Task, TaskState, new_id
from julia.state import Store


def test_task_roundtrip(tmp_path):
    store = Store(tmp_path / 'state.db')
    task = Task(id=new_id(), prompt='do a thing', repo='acme/app')
    store.save_task(task)
    loaded = store.get_task(task.id)
    assert loaded is not None
    assert loaded.prompt == 'do a thing'
    assert loaded.state is TaskState.QUEUED
    task.state = TaskState.MERGED
    store.save_task(task)
    assert [t.id for t in store.list_tasks(TaskState.MERGED)] == [task.id]


def test_decisions_and_kv(tmp_path):
    store = Store(tmp_path / 'state.db')
    store.record_decision('orchestrator', 'merged', 'gates passed', 'abc')
    [(_, actor, action, reason, _meta)] = store.decisions_for('abc')
    assert actor == 'orchestrator'
    assert action == 'merged'
    assert reason == 'gates passed'
    store.kv_set('rung:__global__', '2')
    assert store.kv_get('rung:__global__') == '2'


def test_quota_events_window(tmp_path):
    store = Store(tmp_path / 'state.db')
    store.add_quota_event('task:1')
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    assert store.quota_used_since(since) == 1


def test_backup_is_restorable(tmp_path):
    store = Store(tmp_path / 'state.db')
    task = Task(id=new_id(), prompt='x', repo='a/b')
    store.save_task(task)
    backup_path = store.backup(tmp_path / 'backups' / 'copy.db')
    restored = Store(backup_path)
    assert restored.get_task(task.id) is not None


def test_migrates_legacy_decisions_table(tmp_path):
    """Pre-Phase-2 SQLite databases had no `meta` column. Migrate forward.

    Vision section 14: durable state survives restarts. Schema
    forward-migration must be idempotent and automatic. This test
    simulates a pre-existing database and confirms the upgrade is
    silent.
    """
    import sqlite3
    path = tmp_path / 'legacy.db'
    legacy = sqlite3.connect(str(path))
    legacy.executescript(
        """
        CREATE TABLE tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL,
                            state TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
                                task_id TEXT, actor TEXT NOT NULL,
                                action TEXT NOT NULL, reason TEXT NOT NULL);
        CREATE TABLE quota_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
                                   at TEXT NOT NULL, label TEXT NOT NULL);
        CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    legacy.execute(
        "INSERT INTO decisions (at, actor, action, reason) VALUES (?, ?, ?, ?)",
        ('2026-06-16T00:00:00+00:00', 'orchestrator', 'merged', 'old shape'),
    )
    legacy.commit()
    legacy.close()
    store = Store(path)
    # Now writing a decision with meta must succeed without error.
    store.record_decision('orchestrator', 'merged', 'new shape', 't-1', {'kind': 'plan'})
    rows = store.decisions_for('t-1')
    assert rows and rows[0][4] == {'kind': 'plan'}
