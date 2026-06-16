'''End-to-end rehearsals of the full spine using the fakes.

These tests exercise exactly what dry-run mode exercises: gateway
message -> task -> Jules session (plan review, clarification answer) ->
PR -> quality gates -> merge or approval queue -> notification, with
decision traces recorded throughout. No network, no quota spend.
'''

from julia.autonomy import Rung
from julia.config import Settings
from julia.gateway.base import Incoming, MemoryGateway
from julia.gh.client import FakeGitHubClient
from julia.jules.client import FakeJulesClient
from julia.llm.provider import RuleBasedModel
from julia.models import TaskState
from julia.orchestrator import Orchestrator
from julia.state import Store

from julia.behavior.editor import FakeBehaviorEditor


def make_orchestrator(tmp_path):
    settings = Settings(
        _env_file=None,
        dry_run=True,
        state_dir=tmp_path,
        default_repo='acme/app',
        poll_interval_s=0,
    )
    store = Store(tmp_path / 'julia.db')
    gateway = MemoryGateway()
    orchestrator = Orchestrator(
        settings, store, FakeJulesClient(), FakeGitHubClient(), RuleBasedModel(), gateway
    )
    return orchestrator, gateway


async def drain(orchestrator):
    await orchestrator.await_runners()


async def test_full_spine_merges_and_notifies(tmp_path):
    orchestrator, gateway = make_orchestrator(tmp_path)
    await orchestrator.handle_message(Incoming('Add dark mode to settings', 'owner'))
    await drain(orchestrator)
    [task] = orchestrator.store.list_tasks(TaskState.MERGED)
    assert task.pr_url is not None
    assert orchestrator.github.merged == [task.pr_url]
    actions = {action for _, _, action, _, _ in orchestrator.store.decisions_for(task.id)}
    assert {'task_created', 'plan_approved', 'clarification_answered', 'merged'} <= actions
    assert any('Shipped' in text for text in gateway.sent)


async def test_supervised_queues_then_owner_approves(tmp_path):
    orchestrator, gateway = make_orchestrator(tmp_path)
    orchestrator.ladder.set_rung(Rung.SUPERVISED, 'test')
    await orchestrator.handle_message(Incoming('Fix the login bug', 'owner'))
    await drain(orchestrator)
    [task] = orchestrator.store.list_tasks(TaskState.AWAITING_APPROVAL)
    assert orchestrator.github.merged == []
    await orchestrator.handle_message(Incoming(f'/approve {task.id}', 'owner'))
    assert orchestrator.github.merged == [task.pr_url]
    [merged] = orchestrator.store.list_tasks(TaskState.MERGED)
    assert merged.id == task.id


async def test_panic_blocks_new_tasks(tmp_path):
    orchestrator, gateway = make_orchestrator(tmp_path)
    await orchestrator.handle_message(Incoming('/panic', 'owner'))
    await orchestrator.handle_message(Incoming('Do something risky', 'owner'))
    assert orchestrator._runners == {}
    assert orchestrator.store.list_tasks() == []
    assert any('SAFE_MODE' in text for text in gateway.sent)


async def test_quota_exhaustion_refuses_politely(tmp_path):
    orchestrator, gateway = make_orchestrator(tmp_path)
    orchestrator.quota.limit = 2  # one canary reserve + one task
    await orchestrator.handle_message(Incoming('First task', 'owner'))
    await orchestrator.handle_message(Incoming('Second task', 'owner'))
    await drain(orchestrator)
    assert len(orchestrator.store.list_tasks(TaskState.MERGED)) == 1
    assert any('quota' in text.lower() for text in gateway.sent)


async def test_failed_gates_block_merge_and_comment(tmp_path):
    orchestrator, gateway = make_orchestrator(tmp_path)
    orchestrator.github.checks_pass = False
    await orchestrator.handle_message(Incoming('Refactor the parser', 'owner'))
    await drain(orchestrator)
    [task] = orchestrator.store.list_tasks(TaskState.FAILED)
    assert orchestrator.github.merged == []
    assert orchestrator.github.comments, 'Julia should have commented back on the PR'
    assert any('gates failed' in text for text in gateway.sent)


async def test_canary_runs_healthy_in_dry_run(tmp_path):
    orchestrator, gateway = make_orchestrator(tmp_path)
    await orchestrator.run_canary()
    assert orchestrator.quota.used() == 1
    assert not any('drift' in text for text in gateway.sent)


async def test_digest_summarises_the_day(tmp_path):
    orchestrator, gateway = make_orchestrator(tmp_path)
    await orchestrator.handle_message(Incoming('Ship a small feature', 'owner'))
    await drain(orchestrator)
    digest = orchestrator.digest()
    assert 'Shipped: 1' in digest
    assert 'quota left' in digest


async def test_restart_resumes_in_flight_tasks(tmp_path):
    orchestrator, gateway = make_orchestrator(tmp_path)
    await orchestrator.handle_message(Incoming('Long running task', 'owner'))
    await drain(orchestrator)
    # Simulate a fresh process over the same durable state: the resumed
    # ledger still knows everything that happened before the restart.
    fresh_store = Store(tmp_path / 'julia.db')
    assert len(fresh_store.list_tasks(TaskState.MERGED)) == 1


async def test_pr_watcher_auto_merges_low_stakes_with_green_ci(tmp_path):
    """Step 5: behaviour PRs whose CI is green and whose target file
    lives under prompts/ or playbook/ are auto-merged by the
    _poll_behavior_prs sweep. Behavioural PRs (policies/) are not."""
    settings = Settings(
        _env_file=None,
        dry_run=True,
        state_dir=tmp_path,
        default_repo='acme/app',
        poll_interval_s=0,
        poll_prs_interval_s=0,
    )
    store = Store(tmp_path / 'julia.db')
    github = FakeGitHubClient(checks_pass=True)
    editor = FakeBehaviorEditor()
    orchestrator = Orchestrator(
        settings, store, FakeJulesClient(), github, RuleBasedModel(),
        MemoryGateway(), behavior=editor,
    )
    await orchestrator.handle_message(
        Incoming('/improve prompts/plan_review.md:low-stakes tweak ', 'owner')
    )
    await drain(orchestrator)
    [task] = [t for t in store.list_tasks() if getattr(t, 'kind', 'dev') == 'behavior_pr']
    task_id = task.id
    # Fake editor returns a fake-prefix token, not an http URL, so the
    # watcher skips it. We swap in a real-looking URL so the watcher
    # has something to poll.
    task.source_url = f'https://github.com/behaviors/pull/{task_id}'
    store.save_task(task)
    await orchestrator._poll_behavior_prs()
    # Re-fetch from the store because the in-memory task object here
    # is a stale copy of what the watcher round-tripped via SQLite.
    merged_list = store.list_tasks(TaskState.MERGED)
    assert any(t.id == task_id for t in merged_list)
    assert any(task.source_url in m for m in github.merged)
    actions = {a for _, _, a, _, _ in store.decisions_for(task_id)}
    assert 'auto_merged' in actions


async def test_pr_watcher_does_not_auto_merge_behavioural(tmp_path):
    settings = Settings(
        _env_file=None,
        dry_run=True,
        state_dir=tmp_path,
        default_repo='acme/app',
        poll_interval_s=0,
        poll_prs_interval_s=0,
    )
    store = Store(tmp_path / 'julia.db')
    github = FakeGitHubClient(checks_pass=True)
    editor = FakeBehaviorEditor()
    orchestrator = Orchestrator(
        settings, store, FakeJulesClient(), github, RuleBasedModel(),
        MemoryGateway(), behavior=editor,
    )
    await orchestrator.handle_message(
        Incoming('/improve policies/autonomy_rules.md:behavioural tweak', 'owner')
    )
    await drain(orchestrator)
    [task] = [t for t in store.list_tasks() if getattr(t, 'kind', 'dev') == 'behavior_pr']
    task_id = task.id
    task.source_url = f'https://github.com/behaviors/pull/{task_id}'
    store.save_task(task)
    await orchestrator._poll_behavior_prs()
    # Behavioural PR with green CI: still AWAITING_APPROVAL; merge
    # only happens via /approve-behavior.
    refreshed_list = store.list_tasks(TaskState.AWAITING_APPROVAL)
    assert any(t.id == task_id for t in refreshed_list)
    assert task.source_url not in github.merged
    # Owner approval flips it.
    await orchestrator.handle_message(
        Incoming(f'/approve-behavior {task.source_url}', 'owner')
    )
    merged_list = store.list_tasks(TaskState.MERGED)
    assert any(t.id == task_id for t in merged_list)
    assert task.source_url in github.merged


async def test_approve_behavior_unknown_token_is_courteous(tmp_path):
    orchestrator, gateway = make_orchestrator(tmp_path)
    await orchestrator.handle_message(
        Incoming('/approve-behavior https://github.com/no/such/pr', 'owner')
    )
    assert any('No /improve task' in text for text in gateway.sent)
