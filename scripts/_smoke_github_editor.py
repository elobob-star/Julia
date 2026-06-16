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
created by default (``--no-cleanup`` disables this for debugging).
The ``--cleanup-only`` flag runs *just* the cleanup using an
existing token + repo target, useful between sessions.

Required scopes for cleanup to actually succeed:

  * ``repo`` (PR close + branch delete via git/refs).
  * ``delete_repo`` is *not* required unless you also want to
    scrub a scratch repo between runs.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from julia.behavior.editor import GitHubBehaviorEditor, PlaybookEntry  # noqa: E402
from julia.gh.client import HttpGitHubClient, parse_pr_url  # noqa: E402
from julia.jules import dossier  # noqa: E402

OWNER = "elobob-star"
REPO = os.environ.get("JULIA_SMOKE_REPO", "behavior-smoke")


def _branch_from_pr_url(pr_url: str, repo: str) -> str:
    """Best-effort: read the branch out of the just-opened PR.

    The PR was opened with `head: branch_name`; if you need to close
    it via the API without trusting the local log, fetch via
    `GitHubAPI.get_pull_request_for_branch` first. Here we trust the
    smoke's own recent PR html_url and look it up with the API.
    """
    return ''  # filled in by HttpGitHubClient.aclose'd cleanup below


async def _cleanup(
    token: str, owner: str, repo: str, pr_url: str,
) -> None:
    """Close a PR and delete its feature branch. Idempotent.

    Documentation references vision §3 (least-surprise cleanup):
    the smoke should not accumulate stale branches across runs.
    Failures here print but do not raise — cleanup is best-effort.
    """
    http_client = HttpGitHubClient(token=token)
    try:
        # Close the PR (idempotent: already-closed returns False).
        try:
            await http_client.update_pull_request(
                repository=repo, pr_url=pr_url, state='closed'
            )
            print(f"cleanup: PR {pr_url} closed")
        except Exception as exc:
            print(f"cleanup: close PR failed ({exc!r})")
        # Fetch the PR to learn its branch, then delete the ref.
        owner_name, repo_name, number = parse_pr_url(pr_url)
        try:
            pr_view = (
                await http_client._http.get(
                    f'/repos/{owner_name}/{repo_name}/pulls/{number}'
                )
            ).json()
            branch = pr_view.get('head', {}).get('ref', '')
            if branch:
                deleted = await http_client.delete_ref(
                    f'{owner_name}/{repo_name}', f'heads/{branch}'
                )
                print(
                    f"cleanup: branch {branch} "
                    f"{'deleted' if deleted else 'absent'}"
                )
        except Exception as exc:
            print(f"cleanup: read PR / delete branch failed ({exc!r})")
    finally:
        await http_client.aclose()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--no-cleanup',
        action='store_true',
        help='leave the PR open and the feature branch in place',
    )
    parser.add_argument(
        '--cleanup-only',
        action='store_true',
        help='do not open a new PR; close and delete any found',
    )
    args = parser.parse_args()

    token = os.environ.get("JULIA_SMOKE_TOKEN")
    if not token:
        print(
            "Set JULIA_SMOKE_TOKEN to a GitHub PAT with write access to "
            f"{OWNER}/{REPO}. Skipping real-network smoke."
        )
        sys.exit(0)

    if args.cleanup_only:
        # Allow operator to pass --pr-url explicitly or read from a
        # last-run scrape; for now, list open PRs and close any
        # `jules/` branch. This is a deliberately small surface;
        # the everyday case is "the previous run left a PR open".
        # The branch prefix `jules/` matches the smoke PR convention.
        http = HttpGitHubClient(token=token)
        try:
            response = await http._http.get(
                f'/repos/{OWNER}/{REPO}/pulls',
                params={'state': 'open', 'per_page': '30'},
            )
            response.raise_for_status()
            items = response.json()
            targets = [
                item['html_url'] for item in items
                if item.get('head', {}).get('ref', '').startswith('jules/')
            ]
            if not targets:
                print('cleanup-only: no open jules/-prefixed PRs found.')
                return
            for pr_url in targets:
                await _cleanup(token, OWNER, REPO, pr_url)
        finally:
            await http.aclose()
        return

    editor = GitHubBehaviorEditor(token=token, owner=OWNER, repo=REPO)
    pr_url: str | None = None
    try:
        # Append a playbook entry straight to main.
        await editor.record_playbook_entry(
            PlaybookEntry(
                kind="info",
                repo=f"{OWNER}/Julia",
                task_id="smoke-test",
                gist="Real-network smoke: GitHubBehaviorEditor wired live.",
            )
        )
        print("playbook entry committed to main")
        # Open a low-stakes PR over an existing prompt.
        prompt_body = dossier.load_prompt("canary", None)
        pr_url = await editor.propose_low_stakes_change(
            "prompts/canary.md",
            prompt_body + "\n# smoke: opened by GitHubBehaviorEditor\n",
            "smoke-test: low-stakes propose flow",
        )
        print(f"low-stakes PR open: {pr_url}")
    finally:
        await editor.aclose()
        if pr_url and not args.no_cleanup:
            await _cleanup(token, OWNER, REPO, pr_url)


if __name__ == "__main__":
    asyncio.run(main())
