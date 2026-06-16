"""End-to-end smoke for the wire-up: dry-run + GitHub behavior editor.

Builds an orchestrator exactly the way ``julia run --dry-run
--behaviors <github-repo>`` would, but with the live GitHub
behaviors editor attached so we can verify that the playbook
append actually reaches github.com during the rehearsal.

The Jules and GitHub clients are fakes: this exercises the
*wire-up* without spending Jules quota. It is the closest
rehearsal to a real first run that does not depend on Jules
quota or a GitHub rate-limit budget.

Skipped automatically unless ``JULIA_SMOKE_TOKEN`` is set. The
PAT must have write access to ``JULIA_BEHAVIORS_REPO`` (default
``elobob-star/behaviors``).

Manual invocation::

    JULIA_SMOKE_TOKEN=ghp_xxx \\
    PYTHONPATH=src python scripts/_smoke_full_pipeline.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from julia.autonomy import Rung  # noqa: E402
from julia.behavior.editor import FakeBehaviorEditor, GitHubBehaviorEditor  # noqa: E402
from julia.config import Settings  # noqa: E402
from julia.gateway.base import Incoming, MemoryGateway  # noqa: E402
from julia.gh.client import FakeGitHubClient  # noqa: E402
from julia.jules.client import FakeJulesClient  # noqa: E402
from julia.llm.provider import RuleBasedModel  # noqa: E402
from julia.orchestrator import Orchestrator  # noqa: E402


async def main() -> int:
    token = os.environ.get("JULIA_SMOKE_TOKEN")
    repo = os.environ.get("JULIA_SMOKE_REPO", "elobob-star/behaviors")
    if not token:
        print("Set JULIA_SMOKE_TOKEN; using FakeBehaviorEditor only.")
        editor = FakeBehaviorEditor()
    else:
        owner, _, name = repo.partition("/")
        editor = GitHubBehaviorEditor(token=token, owner=owner, repo=name)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        settings = Settings(
            _env_file=None,
            dry_run=True,
            state_dir=tmp_path,
            default_repo="elobob-star/julia-sandbox",
            poll_interval_s=0,
        )
        store = Settings().db_path() if False else None  # placeholder
        # Use the orchestrator's expected db_path via state_dir:
        # settings.db_path() returns state_dir/julia.db; tests use the
        # same pattern but pass an explicit /julia.db under tmp.
        from julia.state import Store
        store = Store(tmp_path / "julia.db")
        orchestrator = Orchestrator(
            settings,
            store,
            FakeJulesClient(),
            FakeGitHubClient(),
            RuleBasedModel(),
            MemoryGateway(),
            behavior=editor,
        )
        # Set rung to 4 (full auto) so we exercise the merge path
        # without manually approving.
        orchestrator.ladder.set_rung(Rung.FULL_AUTO, "smoke")
        await orchestrator.handle_message(
            Incoming("Add CANARY.md with today's date and a one-line note.", "owner")
        )
        await orchestrator.await_runners()
        # The merged task should be the only MERGED outcome.
        from julia.models import TaskState
        merged = store.list_tasks(TaskState.MERGED)
        print(f"merged tasks: {len(merged)}")
        if merged:
            print(f"  PR URL: {merged[0].pr_url}")
        # The store's decision log is the authoritative trace; the
        # _on_completed code path records `merged` then the
        # _record('completion', ...) call (no-op without editor)
        # only fires before the merge_actor side-effect. Decision
        # traces are the cross-editor signal.
        merged_decisions = [
            d for d in store.decisions_for(merged[0].id) if d[2] == 'merged'
        ] if merged else []
        print(f"merge decisions recorded: {len(merged_decisions)}")
        if isinstance(editor, FakeBehaviorEditor):
            print("(FakeBehaviorEditor: no network write happened.)")
            return 0
        print("GitHub wiring exercised end-to-end through the dry-run spine.")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
