from julia.quota import QuotaGuard
from julia.state import Store


def test_limit_with_canary_reserve(tmp_path):
    store = Store(tmp_path / 'quota.db')
    quota = QuotaGuard(store, limit=3, canary_budget=1)
    assert quota.try_acquire('task:1')
    assert quota.try_acquire('task:2')
    # The last slot is reserved for the daily canary probe.
    assert not quota.try_acquire('task:3')
    assert quota.try_acquire('canary', canary=True)
    assert not quota.try_acquire('canary-again', canary=True)


def test_remaining_counts(tmp_path):
    store = Store(tmp_path / 'quota.db')
    quota = QuotaGuard(store, limit=10, canary_budget=2)
    assert quota.remaining() == 8
    assert quota.remaining(canary=True) == 10
    quota.try_acquire('task:1')
    assert quota.remaining() == 7
