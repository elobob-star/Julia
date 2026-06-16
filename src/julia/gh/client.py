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


def parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    match = _PR_RE.search(pr_url)
    if not match:
        raise ValueError(f'not a GitHub pull request URL: {pr_url}')
    return match.group(1), match.group(2), int(match.group(3))


class GitHubAPI(Protocol):
    async def pr_checks_passed(self, pr_url: str) -> bool: ...

    async def merge_pr(self, pr_url: str) -> bool: ...

    async def comment(self, pr_url: str, body: str) -> None: ...


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


class FakeGitHubClient:
    '''Records side effects instead of performing them.'''

    def __init__(self, checks_pass: bool = True) -> None:
        self.checks_pass = checks_pass
        self.merged: list[str] = []
        self.comments: list[tuple[str, str]] = []

    async def pr_checks_passed(self, pr_url: str) -> bool:
        return self.checks_pass

    async def merge_pr(self, pr_url: str) -> bool:
        if pr_url in self.merged:
            return False
        self.merged.append(pr_url)
        return True

    async def comment(self, pr_url: str, body: str) -> None:
        self.comments.append((pr_url, body))
