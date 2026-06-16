from julia.autonomy import AutonomyLadder, Rung
from julia.state import Store


def test_default_and_per_repo_override(tmp_path):
    ladder = AutonomyLadder(Store(tmp_path / 'ladder.db'))
    assert ladder.current() is Rung.AUTO_NOTIFY
    ladder.set_rung(Rung.SUPERVISED, 'test', repo='acme/app')
    assert ladder.current('acme/app') is Rung.SUPERVISED
    assert ladder.current() is Rung.AUTO_NOTIFY


def test_rungs_persist_across_restarts(tmp_path):
    path = tmp_path / 'ladder.db'
    AutonomyLadder(Store(path)).set_rung(Rung.PROPOSE_ONLY, 'test')
    assert AutonomyLadder(Store(path)).current() is Rung.PROPOSE_ONLY


def test_panic_blocks_everything(tmp_path):
    ladder = AutonomyLadder(Store(tmp_path / 'ladder.db'))
    ladder.panic()
    assert ladder.current() is Rung.SAFE_MODE
    assert not ladder.allows_execution()
    assert not ladder.allows_merge()


def test_repeated_anomalies_drop_one_rung(tmp_path):
    ladder = AutonomyLadder(Store(tmp_path / 'ladder.db'))
    ladder.set_rung(Rung.FULL_AUTO, 'test')
    rung = ladder.current()
    for _ in range(3):
        rung = ladder.record_anomaly('boom')
    assert rung is Rung.AUTO_NOTIFY
