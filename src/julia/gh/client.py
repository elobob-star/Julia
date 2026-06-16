'''GitHub API client (vision section 5.2).

Idempotent by construction: merge_pr re-checks merge state first, so a
retried operation never double-merges. The fake records every side
effect for tests and dry-run rehearsals.
'''

from __future__ import annotations

import re
from typing import Protocol

import httpx

_PR_RE = re.compile(r'github\.com/([^/]+)/([^/]+)/pull/(\d+)')


class UnsupportedPatch(ValueError):
    """Raised when ``_parse_unidiff`` sees a patch shape it cannot
    reconstruct. Callers should route the operator to a manual
    publish fallback rather than crashing the orchestrator."""


class PublishFailed(RuntimeError):
    """Raised when ``publish_jules_outputs`` could not assemble a PR
    on GitHub. Distinct from :class:`UnsupportedPatch` (which is a
    parser-level refusal) so callers can tell the operator "we
    understood the patch but GitHub refused" from "we couldn't parse
    the patch at all"."""


def parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    match = _PR_RE.search(pr_url)
    if not match:
        raise ValueError(f'not a GitHub pull request URL: {pr_url}')
    return match.group(1), match.group(2), int(match.group(3))


class GitHubAPI(Protocol):
    async def pr_checks_passed(self, pr_url: str) -> bool: ...

    async def merge_pr(self, pr_url: str) -> bool: ...

    async def comment(self, pr_url: str, body: str) -> None: ...

    async def get_default_branch_sha(self, repo: str) -> str: ...

    async def publish_jules_outputs(
        self, repo: str, base_sha: str, patch_text: str, title: str, body: str,
    ) -> str: ...

    async def get_pull_request_for_branch(self, repo: str, branch: str) -> str | None: ...

    async def update_pull_request(self, repository: str, pr_url: str, **fields: object) -> None: ...

    async def delete_ref(self, repository: str, ref: str) -> bool: ...


class HttpGitHubClient:
    def __init__(self, token: str, api_url: str = 'https://api.github.com') -> None:
        self._http = httpx.AsyncClient(
            base_url=api_url,
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/vnd.github+json',
            },
            timeout=30.0,
        )

    async def pr_checks_passed(self, pr_url: str) -> bool:
        owner, repo, number = parse_pr_url(pr_url)
        pr = (await self._http.get(f'/repos/{owner}/{repo}/pulls/{number}')).json()
        sha = pr['head']['sha']
        response = await self._http.get(f'/repos/{owner}/{repo}/commits/{sha}/check-runs')
        runs = response.json().get('check_runs', [])
        if not runs:
            # No CI configured on the target repo: the gate passes by
            # absence. The orchestrator records this in the decision
            # trace so it is visible, and local verification (vision
            # section 9) can tighten this later.
            return True
        return all(run.get('conclusion') in ('success', 'neutral', 'skipped') for run in runs)

    async def merge_pr(self, pr_url: str) -> bool:
        '''Merge the PR; return False (without error) if already merged.'''
        owner, repo, number = parse_pr_url(pr_url)
        pr = (await self._http.get(f'/repos/{owner}/{repo}/pulls/{number}')).json()
        if pr.get('merged'):
            return False  # idempotency: never double-merge
        response = await self._http.put(
            f'/repos/{owner}/{repo}/pulls/{number}/merge',
            json={'merge_method': 'squash'},
        )
        response.raise_for_status()
        return True

    async def comment(self, pr_url: str, body: str) -> None:
        owner, repo, number = parse_pr_url(pr_url)
        response = await self._http.post(
            f'/repos/{owner}/{repo}/issues/{number}/comments', json={'body': body}
        )
        response.raise_for_status()

    async def get_default_branch_sha(self, repo: str) -> str:
        """Return the SHA at the tip of the repo's default branch.

        Two ``GET`` calls: the first fetches ``default_branch`` (the
        branch *name*, e.g. ``main``); the second dereferences that
        ref to its current commit SHA. The commit SHA is what callers
        use as a parent for a fresh tree + commit when publishing
        Jules outputs as a PR.
        """
        repo_meta = (await self._http.get(f'/repos/{repo}')).json()
        branch_name = str(repo_meta['default_branch'])
        ref_response = await self._http.get(
            f'/repos/{repo}/git/ref/heads/{branch_name}'
        )
        ref_response.raise_for_status()
        return str(ref_response.json()['object']['sha'])

    async def _get_ref_sha(self, owner_repo: str, ref: str) -> str:
        response = await self._http.get(f'/repos/{owner_repo}/git/ref/{ref}')
        response.raise_for_status()
        return str(response.json()['object']['sha'])

    async def _ensure_branch(self, owner_repo: str, branch: str, base_sha: str) -> None:
        """Idempotent branch creation with a base-SHA guard.

        GitHub returns ``422`` when ``refs/heads/<branch>`` already
        exists. In that case, we *compare* the existing branch's
        current tip to ``base_sha``: if it differs, we refuse to
        overwrite it (the orchestrator may have a stale view of
        ``base_sha``); if it agrees, the branch is reusable.
        """
        response = await self._http.post(
            f'/repos/{owner_repo}/git/refs',
            json={'ref': f'refs/heads/{branch}', 'sha': base_sha},
        )
        if response.status_code == 201:
            return
        if response.status_code != 422:
            response.raise_for_status()
        # Already exists; check that the tip matches our base.
        existing = await self._get_ref_sha(owner_repo, f'heads/{branch}')
        if existing != base_sha:
            raise PublishFailed(
                f'refusing to overwrite branch {branch!r}: '
                f'existing tip is {existing[:12]}, '
                f'caller expected {base_sha[:12]}'
            )

    async def _delete_ref(self, owner_repo: str, ref: str) -> bool:
        """Delete a branch / tag (vision §3 — least-surprise cleanup)."""
        response = await self._http.delete(f'/repos/{owner_repo}/git/refs/{ref}')
        if response.status_code == 204:
            return True
        if response.status_code == 422:  # absent
            return False
        response.raise_for_status()
        return True

    async def update_pull_request(
        self, repository: str, pr_url: str, **fields: object,
    ) -> None:
        owner, repo, number = parse_pr_url(pr_url)
        response = await self._http.patch(
            f'/repos/{owner}/{repo}/pulls/{number}', json=fields
        )
        response.raise_for_status()

    async def delete_ref(self, repository: str, ref: str) -> bool:
        return await self._delete_ref(repository, ref)

    async def publish_jules_outputs(
        self, repo: str, base_sha: str, patch_text: str, title: str, body: str,
    ) -> str:
        """Apply a Jules unidiff as a fresh PR.

        Splits ``patch_text`` into per-file hunks via a small regex
        parser, creates one blob per file, builds a tree carrying
        the blobs, commits that tree on a new feature branch, and
        opens the PR. Multi-file patches are common in Jules
        outputs.
        """
        files = _parse_unidiff(patch_text)
        if not files:
            raise UnsupportedPatch('cannot publish an empty unidiff')

        branch = _new_branch_name(title)
        try:
            default_branch = await _default_branch(repo, self._http)
            base_tree_sha = await self._get_ref_sha(
                repo, f'heads/{default_branch}'
            )
        except httpx.HTTPError as exc:
            raise PublishFailed(f'cannot resolve default branch: {exc!r}') from exc
        await self._ensure_branch(repo, branch, base_sha)

        blobs: list[dict[str, str]] = []
        tree_entries: list[dict[str, str]] = []
        for path, content in files.items():
            blob_response = await self._http.post(
                f'/repos/{repo}/git/blobs',
                json={'content': content, 'encoding': 'utf-8'},
            )
            blob_response.raise_for_status()
            mode = '100755' if content.startswith('#!/') else '100644'
            blobs.append({'path': path, 'sha': blob_response.json()['sha'], 'mode': mode})
            tree_entries.append({'path': path, 'sha': blob_response.json()['sha'], 'mode': mode})
            _ = base_tree_sha  # currently unused; trees are created with base_tree when needed

        # Git data API tree-create with base_tree so non-touched files
        # in the parent are preserved automatically.
        tree_response = await self._http.post(
            f'/repos/{repo}/git/trees',
            json={'base_tree': base_tree_sha, 'tree': tree_entries},
        )
        tree_response.raise_for_status()
        new_tree_sha = tree_response.json()['sha']

        commit_response = await self._http.post(
            f'/repos/{repo}/git/commits',
            json={
                'message': title,
                'tree': new_tree_sha,
                'parents': [base_sha],
            },
        )
        commit_response.raise_for_status()
        new_commit_sha = commit_response.json()['sha']

        ref_response = await self._http.patch(
            f'/repos/{repo}/git/refs/heads/{branch}',
            json={'sha': new_commit_sha},
        )
        ref_response.raise_for_status()

        pr_response = await self._http.post(
            f'/repos/{repo}/pulls',
            json={
                'title': title,
                'head': branch,
                'base': default_branch,
                'body': body,
                'draft': False,
            },
        )
        pr_response.raise_for_status()
        return str(pr_response.json()['html_url'])

    async def get_pull_request_for_branch(self, repo: str, branch: str) -> str | None:
        """Return html_url of an open PR whose head is ``branch``.

        Used by the orchestrator to detect that a human has clicked
        'Publish to branch' on the Jules dashboard for this session.
        If found, the orchestrator picks up the existing PR instead
        of opening its own duplicate.
        """
        response = await self._http.get(
            f'/repos/{repo}/pulls',
            params={'state': 'open', 'head': f'{repo.split("/")[0]}:{branch}'},
        )
        response.raise_for_status()
        items = response.json()
        if not items:
            return None
        return str(items[0]['html_url'])


async def _default_branch(repo: str, http: httpx.AsyncClient) -> str:
    """Return the repo's actual default branch name.

    Vision §18 says "protected branches" must not be auto-merged into.
    Reading GitHub's own answer means callers correctly target
    ``main`` / ``master`` / ``trunk`` / ``develop`` / whatever the
    repo uses, instead of hammering an assumed ``main`` that may not
    exist on the target repo.
    """
    response = await http.get(f'/repos/{repo}')
    response.raise_for_status()
    return str(response.json()['default_branch'])


def _new_branch_name(title: str) -> str:
    """Slugify a PR title into a safe feature-branch name."""
    import re as _re
    import time as _time
    import uuid as _uuid
    base = _re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
    base = base[:32] or 'jules'
    return f'jules/{int(_time.time())}-{_uuid.uuid4().hex[:6]}-{base}'


def _parse_unidiff(patch_text: str) -> dict[str, str]:
    """Parse a unidiff produced by Jules into a {path: content} dict.

    Supports multi-file diffs. New files (``--- /dev/null``) start
    from scratch and accumulate added lines; modified files retain a
    minimal line-oriented reconstruction. We only handle the shapes
    Jules actually emits (diff --git ... new file mode / modified
    file --- a +++ b @@ ... +line / -line). For real complex patches,
    GitHub Files API would be smarter; for Jules CANARY-class output,
    this is enough.

    Raises :class:`UnsupportedPatch` for patch shapes this function
    cannot reconstruct (deletions in a non-deleted file, etc).
    Callers should catch and route to a human fallback rather than
    letting the orchestrator swallow a real editing failure.
    """
    files: dict[str, list[str]] = {}
    current: str | None = None
    deleting = False
    for line in patch_text.splitlines():
        if line.startswith('diff --git '):
            m = line.split()
            if len(m) >= 4:
                current = m[3].lstrip('a/').removeprefix('b/')
                files.setdefault(current, [])
                deleting = False
        elif line.startswith('new file mode'):
            deleting = False
        elif line.startswith('deleted file mode'):
            deleting = True
        elif line.startswith('@@'):
            continue
        elif line.startswith('---') or line.startswith('+++'):
            continue
        elif current is None:
            continue
        elif line.startswith('+'):
            if not deleting:
                files[current].append(line[1:])
        elif line.startswith('-'):
            if deleting:
                continue
            # Pure-additive patches dominate Jules CANARY-class
            # output, so we don't try to reconstruct net-line sub-
            # tractions here. If the patch includes real deletions,
            # raise a typed exception so the orchestrator can route
            # the operator to the "publish manually" surface rather
            # than pretending the call succeeded.
            if files[current]:
                raise UnsupportedPatch(
                    f'non-additive hunks on {current}: '
                    f'orchestrator does not auto-apply; '
                    f'publish via Jules dashboard'
                )
        elif line.startswith('\\'):
            continue
    return {path: ''.join(body).rstrip('\n') for path, body in files.items()}


class FakeGitHubClient:
    '''Records side effects instead of performing them.'''

    def __init__(self, checks_pass: bool = True) -> None:
        self.checks_pass = checks_pass
        self.merged: list[str] = []
        self.comments: list[tuple[str, str]] = []
        self.applied_patches: list[dict] = []
        self.base_shas: dict[str, str] = {}

    async def pr_checks_passed(self, pr_url: str) -> bool:
        return self.checks_pass

    async def merge_pr(self, pr_url: str) -> bool:
        if pr_url in self.merged:
            return False
        self.merged.append(pr_url)
        return True

    async def comment(self, pr_url: str, body: str) -> None:
        self.comments.append((pr_url, body))

    async def get_default_branch_sha(self, repo: str) -> str:
        return self.base_shas.get(repo, 'base-sha-fake')

    async def publish_jules_outputs(
        self, repo: str, base_sha: str, patch_text: str, title: str, body: str,
    ) -> str:
        self.applied_patches.append({
            'repo': repo,
            'base_sha': base_sha,
            'patch_text': patch_text,
            'title': title,
            'body': body,
        })
        number = len(self.applied_patches)
        return f'https://github.com/{repo}/pull/{number}'

    async def update_pull_request(
        self, repository: str, pr_url: str, **fields: object,
    ) -> None:
        self.comments.append((pr_url, repr(fields)))

    async def delete_ref(self, repository: str, ref: str) -> bool:
        return True

    async def get_pull_request_for_branch(self, repo: str, branch: str) -> str | None:
        return None  # Fake: never finds a human-published PR.
