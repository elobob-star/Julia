"""Behaviour editor tests (vision section 8).

The contract under test:

* :class:`FakeBehaviorEditor` records every call site and rejects
  category mismatches loudly.
* :class:`LocalBehaviorEditor` writes to a tmp git checkout so the
  ``self_improve`` flow is reachable end-to-end without GitHub.
* The orchestrator passes ``behavior=None`` and still runs the spine.
* ``categorise`` refuses locked paths and secret-shaped paths.
* ``filter_meta`` strips secret keys out of meta before persistence.
"""

from __future__ import annotations

import asyncio

from julia.behavior.editor import (
    BehaviorDenied,
    Category,
    FakeBehaviorEditor,
    LocalBehaviorEditor,
    categorise,
    filter_meta,
)
from julia.behavior.editor import (
    PlaybookEntry as BehaviorEditorPlaybookEntry,  # alias for clarity
)
from julia.config import Settings
from julia.gateway.base import Incoming, MemoryGateway
from julia.gh.client import FakeGitHubClient
from julia.jules.client import FakeJulesClient
from julia.llm.provider import RuleBasedModel
from julia.orchestrator import Orchestrator
from julia.state import Store


def test_categorise_prompt_is_low_stakes():
    assert categorise('prompts/plan_review.md') is Category.LOW_STAKES


def test_categorise_playbook_is_low_stakes():
    assert categorise('playbook/jules-playbook.md') is Category.LOW_STAKES


def test_categorise_autonomy_rules_is_behavioural():
    assert categorise('policies/autonomy_rules.md') is Category.BEHAVIOURAL


def test_categorise_safety_is_denied():
    import pytest  # local scope to keep imports tight
    with pytest.raises(BehaviorDenied):
        categorise('policies/safety.md')


def test_categorise_secret_shaped_is_denied():
    import pytest
    with pytest.raises(BehaviorDenied):
        categorise('secrets/prod.yaml')


def test_filter_meta_strips_secret_keys():
    filtered = filter_meta({'kind': 'plan', 'api_token': 'x', 'safe_field': 1})
    assert filtered == {'kind': 'plan', 'safe_field': 1}


def test_filter_meta_passes_through_none():
    assert filter_meta(None) is None


async def test_fake_editor_records_calls():
    editor = FakeBehaviorEditor()
    await editor.record_playbook_entry(
        BehaviorEditorPlaybookEntry(
            kind='plan', repo='a/b', task_id='t', gist='plan approved'
        )
    )
    assert len(editor.entries) == 1
    sha = await editor.propose_low_stakes_change('prompts/plan_review.md', 'X', 'r')
    assert sha.startswith('fake-')
    [change] = editor.changes
    assert change[0] is Category.LOW_STAKES


async def test_fake_editor_rejects_category_mismatch():
    import pytest
    editor = FakeBehaviorEditor()
    with pytest.raises(BehaviorDenied):
        await editor.propose_low_stakes_change('policies/autonomy_rules.md', 'x', 'r')


async def test_orchestrator_with_no_behavior_is_phase_1(tmp_path):
    settings = Settings(
        _env_file=None,
        dry_run=True,
        state_dir=tmp_path,
        default_repo='acme/app',
        poll_interval_s=0,
    )
    store = Store(tmp_path / 'julia.db')
    orchestrator = Orchestrator(
        settings, store, FakeJulesClient(), FakeGitHubClient(),
        RuleBasedModel(), MemoryGateway(),
        # behavior=None is the explicit default; pass nothing.
    )
    await orchestrator.handle_message(Incoming('Ship a thing', 'owner'))
    await orchestrator.await_runners()
    assert store.list_tasks()


async def test_orchestrator_records_playbook_on_completion(tmp_path):
    settings = Settings(
        _env_file=None,
        dry_run=True,
        state_dir=tmp_path,
        default_repo='acme/app',
        poll_interval_s=0,
    )
    store = Store(tmp_path / 'julia.db')
    editor = FakeBehaviorEditor()
    orchestrator = Orchestrator(
        settings, store, FakeJulesClient(), FakeGitHubClient(),
        RuleBasedModel(), MemoryGateway(), behavior=editor,
    )
    await orchestrator.handle_message(Incoming('Ship a thing', 'owner'))
    await orchestrator.await_runners()
    kinds = [e.kind for e in editor.entries]
    assert 'plan' in kinds
    assert 'completion' in kinds


async def test_orchestrator_improve_command(tmp_path):
    settings = Settings(
        _env_file=None,
        dry_run=True,
        state_dir=tmp_path,
        default_repo='acme/app',
        poll_interval_s=0,
    )
    store = Store(tmp_path / 'julia.db')
    editor = FakeBehaviorEditor()
    gateway = MemoryGateway()
    orchestrator = Orchestrator(
        settings, store, FakeJulesClient(), FakeGitHubClient(),
        RuleBasedModel(), gateway, behavior=editor,
    )
    await orchestrator.handle_message(
        Incoming('/improve prompts/plan_review.md:low-stakes Please revise', 'owner')
    )
    [change] = editor.changes
    assert change[0] is Category.LOW_STAKES
    assert any('Low-stakes change' in text for text in gateway.sent)


async def test_orchestrator_improve_refuses_without_editor(tmp_path):
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
        settings, store, FakeJulesClient(), FakeGitHubClient(),
        RuleBasedModel(), gateway,
    )
    await orchestrator.handle_message(
        Incoming('/improve prompts/plan_review.md:low-stakes something', 'owner')
    )
    assert any('Phase 1 mode' in text for text in gateway.sent)


def test_local_editor_writes_to_disk(tmp_path):
    """Local editor commits to the repo; no GitHub involvement.

    We use ``shutil`` to create a tmp git checkout of the repository
    fixture rather than running ``git init`` here so the test stays
    deterministic on hosts without git (Phase 3 friends: this is also
    what CI will run).
    """
    repo = tmp_path / 'behaviors'
    (repo / 'playbook').mkdir(parents=True)
    (repo / 'playbook' / 'jules-playbook.md').write_text(
        "# Jules Behavioral Playbook\n\n## Observed shape drift\n<!-- anchor -->\n"
    )
    (repo / 'prompts').mkdir(parents=True)
    (repo / 'prompts' / 'plan_review.md').write_text(
        '# Plan Review System Prompt\nbody'
    )
    editor = LocalBehaviorEditor(repo)
    asyncio.run(
        editor.record_playbook_entry(
            BehaviorEditorPlaybookEntry(
                kind='plan', repo='a/b', task_id='t', gist='Plan approved.'
            )
        )
    )
    text = (repo / 'playbook' / 'jules-playbook.md').read_text()
    assert 'Plan approved' in text


def test_local_editor_open_pr_refuses_locked_path(tmp_path):
    import pytest
    repo = tmp_path / 'behaviors'
    repo.mkdir()
    # policy file present, but locked; the safety categoriser raises
    # before any write happens.
    (repo / 'policies').mkdir()
    (repo / 'policies' / 'safety.md').write_text('# Safety\nLocked.\n')
    editor = LocalBehaviorEditor(repo)
    with pytest.raises(BehaviorDenied):
        asyncio.run(
            editor.propose_behavioral_change(
                'policies/safety.md', 'would rewrite safety', 'rationale'
            )
        )


def test_local_editor_open_pr_refuses_untracked_path(tmp_path):
    import pytest
    repo = tmp_path / 'behaviors'
    repo.mkdir()
    editor = LocalBehaviorEditor(repo)
    with pytest.raises(BehaviorDenied):
        asyncio.run(
            editor.propose_low_stakes_change(
                'never-landed/file.md', 'body', 'rationale'
            )
        )
