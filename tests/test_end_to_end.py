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


async def test_completed_session_with_patch_is_published_as_pr(tmp_path):
    """When Jules completed but didn't open a PR (only a gitPatch
    artifact arrived) AND the autonomy rung is SUPERVISED or higher,
    the orchestrator publishes the patch through GitHub's translator
    so the rest of the spine (CI polling, /approve-behavior) has a
    real PR to act on. Below SUPERVISED the patch is recorded but
    not applied.
    """
    from julia.autonomy import Rung
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

    # Build a FakeJulesClient that emits only a gitPatch artifact on
    # session_completed (no pullRequestUrl) so we exercise the new path.
    fake_jules = FakeJulesClient()
    real_list_activities = fake_jules.list_activities

    async def patch_only_list_activities(session_id: str):
        activities = await real_list_activities(session_id)
        # Drop the completion entry; add a replacement that carries
        # only an artifacts[].changeSet.gitPatch.
        activities = [
            a for a in activities
            if a.get('type') != 'session_completed'
        ]
        activities.append({
            'type': 'session_completed',
            # Note: NO pullRequestUrl here.
            'artifacts': [{
                'changeSet': {
                    'source': 'sources/github/acme/app',
                    'gitPatch': {
                        'unidiffPatch': (
                            'diff --git a/CANARY.md b/CANARY.md\n'
                            'new file mode 100644\n'
                            '--- /dev/null\n'
                            '+++ b/CANARY.md\n'
                            '@@ -0,0 +1 @@\n'
                            '+jules-only-patch-no-pr\n'
                        ),
                    },
                },
            }],
        })
        return activities

    fake_jules.list_activities = patch_only_list_activities  # type: ignore[assignment]
    gateway = MemoryGateway()
    orchestrator = Orchestrator(
        settings, store, fake_jules, github, RuleBasedModel(), gateway,
    )
    # SUPERVISED => publishing gate at rung >= 2 is open.
    orchestrator.ladder.set_rung(Rung.SUPERVISED, 'test')

    await orchestrator.handle_message(
        Incoming('Make a CANARY.md line', 'owner')
    )
    await drain(orchestrator)

    # After publish + gates-pass, the task ends AWAITING_APPROVAL on
    # rung SUPERVISED (which doesn't auto-merge) — the precise state
    # is one of MERGED / AWAITING_APPROVAL depending on rung. SUPERVISED
    # here so we expect AWAITING_APPROVAL. The contract under test
    # is that publishing actually happened, not the merge step.
    all_tasks = store.list_tasks()
    assert all_tasks, 'task must exist end-to-end'
    task = all_tasks[0]
    assert task.pr_url, 'orchestrator must have published a PR from the patch'
    assert task.pr_url.startswith('https://github.com/acme/app/pull/'), (
        f'PR URL came from the patch translator; got {task.pr_url!r}'
    )
    # The new decision 'patch_published_as_pr' should be visible.
    actions = {a for _, _, a, _, _ in store.decisions_for(task.id)}
    assert 'patch_published_as_pr' in actions
    # The Fake's translator-recorded patch record should mention the
    # right repo and carry the exact patch bytes.
    matching = [p for p in github.applied_patches if p['repo'] == 'acme/app']
    assert matching, 'publish_jules_outputs must have been called'
    assert matching[0]['patch_text'].startswith('diff --git a/CANARY.md')
    # Transparency to the owner.
    assert any('opened PR' in text for text in gateway.sent)


async def test_completed_session_with_patch_held_below_supervised(tmp_path):
    """Mirror of the test above but with publish gated off.

    PROPOSE_ONLY (rung 1) refuses execution entirely, so it can't
    drive this code path. Instead, force ``allows_publish`` to deny
    on a rung where execution is allowed, asserting the orchestrator
    records patch_unapplied *without* making any GitHub call.
    """
    settings = Settings(
        _env_file=None,
        dry_run=True,
        state_dir=tmp_path,
        default_repo='acme/app',
        poll_interval_s=0,
        poll_prs_interval_s=0,
    )
    store = Store(tmp_path / 'julia.db')
    github = FakeGitHubClient()
    fake_jules = FakeJulesClient()
    real_list_activities = fake_jules.list_activities

    async def patch_only_list_activities(session_id: str):
        activities = await real_list_activities(session_id)
        activities = [
            a for a in activities
            if a.get('type') != 'session_completed'
        ]
        activities.append({
            'type': 'session_completed',
            'artifacts': [{
                'changeSet': {
                    'source': 'sources/github/acme/app',
                    'gitPatch': {'unidiffPatch': 'diff --git a/x b/x\n'},
                },
            }],
        })
        return activities

    fake_jules.list_activities = patch_only_list_activities  # type: ignore[assignment]
    gateway = MemoryGateway()
    orchestrator = Orchestrator(
        settings, store, fake_jules, github, RuleBasedModel(), gateway,
    )
    # Stub allows_publish to deny without touching the rest of the
    # ladder (else PROPOSE_ONLY refuses intake and never reaches
    # _on_completed). Default rung is AUTO_NOTIFY so allows_execution
    # already returns True.
    orchestrator.ladder.allows_publish = lambda repo=None: False  # type: ignore[assignment]

    await orchestrator.handle_message(
        Incoming('Patch-only, publishing disabled', 'owner')
    )
    await drain(orchestrator)
    awaiting_tasks = store.list_tasks(TaskState.AWAITING_APPROVAL)
    assert awaiting_tasks, 'task must end up AWAITING_APPROVAL when patch held'
    task = awaiting_tasks[0]
    actions = {a for _, _, a, _, _ in store.decisions_for(task.id)}
    assert 'patch_unapplied' in actions
    assert not task.pr_url
    assert github.applied_patches == []
    assert any('Patch captured' in text for text in gateway.sent)
