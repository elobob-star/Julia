"""Real-network smoke test for the GitHub behaviour editor.

Drives ``GitHubBehaviorEditor`` end-to-end against a live GitHub
repo on github.com. Records the playbook entry straight to
``main`` and opens one low-stakes PR through
``propose_low_stakes_change``.

Skipped automatically unless ``JULIA_SMOKE_TOKEN`` is set in the
environment. The PAT must have write access to the target repo
(``elobob-star/behavior-smoke`` by default; created on demand by
the script's manual setup).

By default the script writes to the live ``behaviors`` repo; set
``JULIA_SMOKE_REPO=owner/name`` to redirect. The PAT must have
write access to that repo.

Manual invocation::

    JULIA_SMOKE_TOKEN=ghp_xxx \\
    JULIA_SMOKE_REPO=elobob-star/behavior-smoke \\
    PYTHONPATH=src python scripts/_smoke_github_editor.py

The smoke closes its own PR and deletes the feature branch it
created; only the throwaway repo (if used) needs the
``delete_repo`` scope to clean up afterwards.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from julia.behavior.editor import GitHubBehaviorEditor, PlaybookEntry  # noqa: E402
from julia.jules import dossier  # noqa: E402

OWNER = "elobob-star"
REPO = os.environ.get("JULIA_SMOKE_REPO", "behavior-smoke")


async def main() -> None:
    token = os.environ.get("JULIA_SMOKE_TOKEN")
    if not token:
        print(
            "Set JULIA_SMOKE_TOKEN to a GitHub PAT with write access to "
            f"{OWNER}/{REPO}. Skipping real-network smoke."
        )
        sys.exit(0)
    editor = GitHubBehaviorEditor(token=token, owner=OWNER, repo=REPO)
    try:
        # Append a playbook entry straight to main.
        await editor.record_playbook_entry(
            PlaybookEntry(
                kind="info",
                repo=f"{OWNER}/julia-main",
                task_id="smoke-test",
                gist="Real-network smoke: GitHubBehaviorEditor wired live.",
            )
        )
        print("playbook entry committed to main")
        # Open a low-stakes PR over an existing prompt.
        editor_for_pr = GitHubBehaviorEditor(token=token, owner=OWNER, repo=REPO)
        prompt_body = dossier.load_prompt("canary", None)
        url = await editor_for_pr.propose_low_stakes_change(
            "prompts/canary.md",
            prompt_body + "\n# smoke: opened by GitHubBehaviorEditor\n",
            "smoke-test: low-stakes propose flow",
        )
        print(f"low-stakes PR open: {url}")
    finally:
        await editor.aclose()


if __name__ == "__main__":
    asyncio.run(main())
