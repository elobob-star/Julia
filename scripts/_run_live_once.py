"""Send *one* task to a live Jules session for live validation.

Boots the orchestrator against real Jules + real GitHub, sends one
trivial task ("create CANARY.md with today's date"), waits for the
spine to finish, and prints the state. This is the *first live
task* validation per vision §21.

Preconditions:

  * ``JULIA_JULES_API_KEY`` is set.
  * ``JULIA_GITHUB_TOKEN`` is set.
  * ``JULIA_DEFAULT_REPO`` points at an empty test repo on GitHub.
  * ``JULIA_BEHAVIORS_REPO`` points at the behaviors substrate.

The script uses ``MemoryGateway`` so it auto-feed one message in
and writes every notification out to stdout. It then exits. This
is a one-shot, not the long-running orchestrator.

Manual invocation::

    JULIA_JULES_API_KEY=... \\
    JULIA_GITHUB_TOKEN=... \\
    JULIA_DEFAULT_REPO=elobob-star/julia-sandbox \\
    JULIA_BEHAVIORS_REPO=elobob-star/behaviors \\
    PYTHONPATH=src python scripts/_run_live_once.py
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
from julia.behavior.editor import GitHubBehaviorEditor  # noqa: E402
from julia.config import Settings  # noqa: E402
from julia.gateway.base import Incoming, MemoryGateway  # noqa: E402
from julia.gh.client import HttpGitHubClient  # noqa: E402
from julia.jules.client import HttpJulesClient  # noqa: E402
from julia.llm.provider import RuleBasedModel  # noqa: E402
from julia.orchestrator import Orchestrator  # noqa: E402
from julia.state import Store  # noqa: E402


REPO = os.environ.get("JULIA_DEFAULT_REPO")
BEHAVIORS = os.environ.get("JULIA_BEHAVIORS_REPO")


async def main() -> int:
    if not os.environ.get("JULIA_JULES_API_KEY"):
        print("JULIA_JULES_API_KEY is required.")
        return 2
    if not os.environ.get("JULIA_GITHUB_TOKEN"):
        print("JULIA_GITHUB_TOKEN is required.")
        return 2
    if not REPO:
        print("JULIA_DEFAULT_REPO (owner/name) is required.")
        return 2
    if not BEHAVIORS:
        print("JULIA_BEHAVIORS_REPO (owner/name) is required.")
        return 2

    jules = HttpJulesClient(os.environ["JULIA_JULES_API_KEY"])
    github = HttpGitHubClient(os.environ["JULIA_GITHUB_TOKEN"])
    gh_token = os.environ["JULIA_GITHUB_TOKEN"]
    bh_owner, _, bh_name = BEHAVIORS.partition("/")
    editor = GitHubBehaviorEditor(token=gh_token, owner=bh_owner, repo=bh_name)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # Pick rung 4 for this first live validation: I want to see
        # the merge happen automatically so I know the spinner works.
        # rung 3 (the deployment default) would queue for approval;
        # I am supervising this run by hand.
        settings = Settings(
            _env_file=None,
            dry_run=False,
            state_dir=tmp_path,
            default_repo=REPO,
            poll_interval_s=15,
            stall_timeout_s=1800,
        )
        store = Store(tmp_path / "julia.db")
        gateway = MemoryGateway()
        orchestrator = Orchestrator(
            settings,
            store,
            jules,
            github,
            RuleBasedModel(),
            gateway,
            behavior=editor,
        )
        orchestrator.ladder.set_rung(Rung.FULL_AUTO, "first live validation")

        await orchestrator.handle_message(
            Incoming(
                "Create CANARY.md with today's date as a single line. "
                "Make no other changes.",
                "owner",
            )
        )
        await orchestrator.await_runners()
        await editor.aclose()

        from julia.models import TaskState

        for state in TaskState:
            count = len(store.list_tasks(state))
            print(f"{state.value}: {count}")
        merged = store.list_tasks(TaskState.MERGED)
        if merged:
            task = merged[0]
            print(f"\nPR URL: {task.pr_url}")
            print("\nDecision trace:")
            for at, actor, action, reason, meta in store.decisions_for(task.id):
                print(f"  {at}  {actor}/{action}: {reason}")
        else:
            print("\nNo merged task. Inspect the trace above.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
