"""Behavior editor: the orchestrator's bridge to its own behavior repo.

Three small Protocol methods (``record_playbook_entry``,
``propose_low_stakes_change``, ``propose_behavioral_change``) and
three implementations (Fake, Local, GitHub). Backwards compat:
the orchestrator treats ``None`` as "no editor" and behaves exactly
as it did before this module existed.
"""

from __future__ import annotations

import base64
import enum
import re
import subprocess  # nosec - B603
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
import time as _time
import uuid as _uuid

# Don't lose track: the engineer's behavior (Claude Fable 5) reads
# safety.md but never edits it through this surface. The denylist is
# the line that holds (vision section 8 + section 15).
DENYLIST = re.compile(
    r"(secret|password|credential|\.env$|\.key$|token)",
    re.IGNORECASE,
)
LOCKED_FILES = frozenset({"policies/safety.md"})
LOW_STAKES_DIRS = ("playbook/", "prompts/")
BEHAVIOURAL_DIRS = ("policies/",)
SECRET_KEYS = re.compile(r"(secret|password|token|api.?key)", re.IGNORECASE)


class BehaviorDenied(RuntimeError):
    """Raised when the editor refuses to write a behavior change."""


class Category(str, enum.Enum):
    LOW_STAKES = "low-stakes"
    BEHAVIOURAL = "behavioural"
    LOCKED = "locked"


def categorise(file: str) -> Category:
    """Resolve a path's category.

    Denied paths raise :class:`BehaviorDenied` regardless of caller.
    The orchestrator never overrides this.
    """
    if file in LOCKED_FILES:
        raise BehaviorDenied(f"refusing to open a PR against locked file {file!r}")
    if DENYLIST.search(file):
        raise BehaviorDenied(f"refusing to open a PR: {file!r} matches the safety denylist")
    if any(file.startswith(prefix) for prefix in BEHAVIOURAL_DIRS):
        return Category.BEHAVIOURAL
    if any(file.startswith(prefix) for prefix in LOW_STAKES_DIRS):
        return Category.LOW_STAKES
    raise BehaviorDenied(
        f"refusing to open a PR against {file!r}: not under a tracked directory"
    )


def filter_meta(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """Filter secret-shaped keys out of a decision meta dict.

    Vision section 15: *logs and analytics never contain secret
    values*. The orchestrator calls this once before persisting or
    forwarding meta on the wire.
    """
    if meta is None:
        return None
    return {k: v for k, v in meta.items() if not SECRET_KEYS.search(str(k))}


@dataclass
class PlaybookEntry:
    """One append-only entry into the behavioral playbook.

    ``task_id`` defaults to the originating decision's task id. The
    orchestrator passes ``extra`` as a ``meta`` filtered object so
    the playbook never stores raw credentials by accident.
    """

    kind: str  # 'plan' | 'question' | 'drift' | 'completion' | 'failure' | 'info'
    repo: str  # owner/name
    task_id: str
    gist: str
    extra: dict[str, Any] | None = None

    def render(self) -> str:
        date = self.extra.get("date", "") if self.extra else ""
        date_part = f" ({date})" if date else ""
        extra_part = ""
        if self.extra:
            kept = {k: v for k, v in self.extra.items() if k != "date"}
            if kept:
                extra_part = "\n  meta: " + repr(kept)
        return (
            f"## kind={self.kind}{date_part} - repo={self.repo} - task={self.task_id}\n"
            f"{self.gist.strip()}"
            f"{extra_part}\n"
        )


class BehaviorEditor(Protocol):
    """The orchestrator's write surface for its own behavior.

    Backwards compat: callers may pass ``None`` and skip writes;
    the protocol is intentionally narrow so a Fake implementation
    can pin test expectations without dragging in the rest of the
    editor machinery.
    """

    async def record_playbook_entry(self, entry: PlaybookEntry) -> None: ...

    async def propose_low_stakes_change(
        self, file: str, new_content: str, rationale: str
    ) -> str: ...

    async def propose_behavioral_change(
        self, file: str, new_content: str, rationale: str
    ) -> str: ...


@dataclass
class FakeBehaviorEditor:
    """Test/dry-run behavior editor. Records every call."""

    entries: list[PlaybookEntry] = field(default_factory=list)
    changes: list[tuple[Category, str, str]] = field(default_factory=list)

    async def record_playbook_entry(self, entry: PlaybookEntry) -> None:
        self.entries.append(entry)

    async def propose_low_stakes_change(
        self, file: str, new_content: str, rationale: str
    ) -> str:
        category = categorise(file)
        if category is not Category.LOW_STAKES:
            raise BehaviorDenied(
                f"propose_low_stakes_change called on {file!r}: not low-stakes"
            )
        self.changes.append((category, file, rationale))
        return f"fake-low-stakes:{file}"

    async def propose_behavioral_change(
        self, file: str, new_content: str, rationale: str
    ) -> str:
        category = categorise(file)
        if category is not Category.BEHAVIOURAL:
            raise BehaviorDenied(
                f"propose_behavioral_change called on {file!r}: not behavioural"
            )
        self.changes.append((category, file, rationale))
        return f"fake-behavioural:{file}"


@dataclass
class LocalBehaviorEditor:
    """Commits behaviour changes to a local git checkout of the behaviors repo.

    Mirrors ``behaviors/scripts/self_improve.py`` for parity with the
    git cli workflow. The orchestrator configures this when
    ``--behaviors PATH`` is set; ``GitHubBehaviorEditor`` will replace
    it once the behaviors repo lands on GitHub in Phase 3.
    """

    repo: Path

    async def record_playbook_entry(self, entry: PlaybookEntry) -> None:
        kind_pattern = r"^(plan|question|drift|completion|failure|info)$"
        if not re.match(kind_pattern, entry.kind):
            raise ValueError(f"invalid playground kind: {entry.kind!r}")
        target = self.repo / "playbook" / "jules-playbook.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(
                "# Jules Behavioral Playbook\n\n"
                "<!-- empty; populated by LocalBehaviorEditor -->\n"
            )
        with target.open("a") as handle:
            handle.write("\n" + entry.render() + "\n")

    async def propose_low_stakes_change(
        self, file: str, new_content: str, rationale: str
    ) -> str:
        return await _commit(self.repo, file, Category.LOW_STAKES, new_content, rationale)

    async def propose_behavioral_change(
        self, file: str, new_content: str, rationale: str
    ) -> str:
        return await _commit(self.repo, file, Category.BEHAVIOURAL, new_content, rationale)


async def _commit(
    repo: Path, file: str, expected: Category, content: str, rationale: str
) -> str:
    """Shared backing for the two ``propose_*`` methods.

    Reads the file's existing category out of the safety categoriser;
    rejects any call that asks for a category other than the file's.
    """
    actual = categorise(file)
    if actual is not expected:
        raise BehaviorDenied(
            f"category mismatch for {file!r}: file is {actual.value}, "
            f"requested {expected.value}"
        )
    target = repo / file
    if not target.exists():
        raise BehaviorDenied(
            f"{target} is not in the repo; refusing to invent a path"
        )
    target.write_text(content)
    _git(["add", file], repo)
    _git(["commit", "-m", f"{expected.value}: {rationale}"], repo)
    return _git(["rev-parse", "HEAD"], repo)


def _git(args: list[str], repo: Path) -> str:
    return subprocess.run(  # nosec - B603
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

# ----------- GitHub implementation (vision section 8, networked) -----------

class GitHubBehaviorEditor:
    """Open real PRs against a remote ``behaviors`` repo on GitHub.

    Two flows:

    * :meth:`record_playbook_entry` writes a small append-only block
      straight to ``main`` via the contents API. The playbook is
      *data*, append-only, never policy -- so committing to ``main``
      is the right shape and matches the offline editor's semantics.
    * :meth:`propose_low_stakes_change` /
      :meth:`propose_behavioral_change` create a feature branch and
      open a PR. Behavioural PRs surface in the gateway for owner
      approval; low-stakes ones auto-merge after the prompt
      regression suite in the ``behaviors`` repo passes (vision
      section 5.4 + 8).

    The safety categoriser runs first; locked paths raise
    :class:`BehaviorDenied` *before* any HTTP call. The editor never
    reaches a GitHub endpoint for a refused file.
    """

    def __init__(
        self,
        token: str,
        owner: str,
        repo: str,
        base_branch: str = 'main',
    ) -> None:
        self._owner = owner
        self._repo = repo
        self._base_branch = base_branch
        self._http = httpx.AsyncClient(
            base_url='https://api.github.com',
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28',
            },
            timeout=30.0,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def record_playbook_entry(self, entry: PlaybookEntry) -> None:
        path = 'playbook/jules-playbook.md'
        # 409 Conflict means the SHA moved between our GET and PUT
        # (someone else -- including a parallel session -- pushed to
        # ``main`` in the meantime). Retry once with the fresh SHA
        # so the playbook learns correctly. Two attempts is enough;
        # a third is more likely to indicate a real conflict than
        # bobbling.
        for attempt in (1, 2):
            sha, raw = await self._get_file(path)
            new_text = _append_to_playbook(raw, entry)
            payload: dict[str, Any] = {
                'message': f'playbook: {entry.kind} on {entry.repo} ({entry.task_id})',
                'content': base64.b64encode(new_text.encode()).decode(),
                'branch': self._base_branch,
            }
            if sha is not None:
                payload['sha'] = sha
            response = await self._http.put(
                f'/repos/{self._owner}/{self._repo}/contents/{path}',
                json=payload,
            )
            if response.status_code == 409 and attempt == 1:
                continue
            response.raise_for_status()
            return

    async def propose_low_stakes_change(
        self, file: str, new_content: str, rationale: str
    ) -> str:
        return await self._open_pr(file, Category.LOW_STAKES, new_content, rationale)

    async def propose_behavioral_change(
        self, file: str, new_content: str, rationale: str
    ) -> str:
        return await self._open_pr(file, Category.BEHAVIOURAL, new_content, rationale)

    async def _open_pr(
        self, file: str, expected: Category, content: str, rationale: str
    ) -> str:
        actual = categorise(file)
        if actual is not expected:
            raise BehaviorDenied(
                f'category mismatch for {file!r}: file is {actual.value}, '
                f'requested {expected.value}'
            )
        # Branch names carry timestamp + slug so parallel PRs from
        # concurrent sessions do not collide.
        slug = re.sub(r'[^a-z0-9]+', '-', file.lower()).strip('-')
        branch = f'self-improve/{int(_time.time())}-{_uuid.uuid4().hex[:6]}-{slug}'
        await self._create_branch(branch)
        sha = await self._get_file_sha(file)
        payload: dict[str, Any] = {
            'message': f'{expected.value}: {rationale}',
            'content': base64.b64encode(content.encode()).decode(),
            'branch': branch,
        }
        if sha is not None:
            payload['sha'] = sha
        response = await self._http.put(
            f'/repos/{self._owner}/{self._repo}/contents/{file}',
            json=payload,
        )
        response.raise_for_status()
        pr = await self._http.post(
            f'/repos/{self._owner}/{self._repo}/pulls',
            json={
                'title': f'{expected.value}: {rationale_short(rationale)}',
                'head': branch,
                'base': self._base_branch,
                'body': (
                    f'Category: **{expected.value}**\n\n'
                    f'File: `{file}`\n\nRationale: {rationale}\n\n'
                    'Opened by Julia orchestrator (vision section 8).'
                ),
                'draft': expected is Category.BEHAVIOURAL,
            },
        )
        pr.raise_for_status()
        return str(pr.json().get('html_url') or pr.json().get('number'))

    async def _get_file_sha(self, path: str) -> str | None:
        response = await self._http.get(
            f'/repos/{self._owner}/{self._repo}/contents/{path}',
            params={'ref': self._base_branch},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return str(response.json().get('sha'))

    async def _get_file(self, path: str) -> tuple[str | None, str]:
        """Fetch ``(sha, raw_text)`` in one request to halve round trips."""
        response = await self._http.get(
            f'/repos/{self._owner}/{self._repo}/contents/{path}',
            params={'ref': self._base_branch},
        )
        if response.status_code == 404:
            return None, '# Jules Behavioral Playbook\n\n## Observed shape drift\n'
        response.raise_for_status()
        body = response.json()
        return str(body.get('sha')), base64.b64decode(body['content']).decode()

    async def _get_raw_file(self, path: str) -> str:
        response = await self._http.get(
            f'/repos/{self._owner}/{self._repo}/contents/{path}',
            params={'ref': self._base_branch},
        )
        if response.status_code == 404:
            return '# Jules Behavioral Playbook\n\n## Observed shape drift\n'
        response.raise_for_status()
        return base64.b64decode(response.json()['content']).decode()

    async def _create_branch(self, branch: str) -> None:
        ref_response = await self._http.get(
            f'/repos/{self._owner}/{self._repo}/git/ref/heads/{self._base_branch}'
        )
        ref_response.raise_for_status()
        base_sha = ref_response.json()['object']['sha']
        response = await self._http.post(
            f'/repos/{self._owner}/{self._repo}/git/refs',
            json={'ref': f'refs/heads/{branch}', 'sha': base_sha},
        )
        # 422 means the branch already exists from a previous attempt;
        # the editor's idempotent retry is the same as a no-op there.
        if response.status_code not in (201, 422):
            response.raise_for_status()


def _append_to_playbook(text: str, entry: PlaybookEntry) -> str:
    """Append one playbook entry into the canonical drift header."""
    kind_pattern = r'^(plan|question|drift|completion|failure|info)$'
    if not re.match(kind_pattern, entry.kind):
        raise ValueError(f'invalid playbook kind: {entry.kind!r}')
    block = '\n' + entry.render() + '\n'
    header = '## Observed shape drift'
    if header in text:
        head, tail = text.split(header, 1)
        return head + header + block + tail
    return text + '\n' + header + block


def rationale_short(text: str, *, limit: int = 60) -> str:
    """Trim a rationale to a stable PR-title length."""
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + '…'
