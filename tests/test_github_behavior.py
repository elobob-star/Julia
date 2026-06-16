"""GitHub behaviour editor tests (vision section 8, networked).

Two layers:

* Safety categoriser runs *before* any HTTP call. Locked paths and
  the secret denylist raise ``BehaviorDenied`` synchronously; the
  network is never touched.
* The full PR flow is exercised over ``httpx.MockTransport`` so the
  test never leaves the host. The recorded request shapes double
  as a contract: any path that says ``PUT .../contents/<path>`` gets
  a ``sha`` reply and any ``POST .../pulls`` returns a stub html_url.
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx

from julia.behavior.editor import (
    BehaviorDenied,
    GitHubBehaviorEditor,
    PlaybookEntry,
    _append_to_playbook,
)


def test_locked_path_refuses_before_http():
    """The safety guard runs first; no client is even constructed.

    Calling the constructor with bad inputs would be a side effect we
    want to avoid. If the guard is correctly placed, the call never
    reaches ``httpx`` at all.
    """
    editor = GitHubBehaviorEditor(token='ghp_dummy', owner='elobob-star', repo='behaviors')
    try:
        asyncio.run(
            editor.propose_behavioral_change(
                'policies/safety.md', 'would-be rewrite', 'rationale'
            )
        )
    except BehaviorDenied:
        return  # expected
    finally:
        asyncio.run(editor.aclose())
    raise AssertionError('expected BehaviorDenied for policies/safety.md')


def test_secret_shaped_path_refuses_before_http():
    editor = GitHubBehaviorEditor(token='ghp_dummy', owner='elobob-star', repo='behaviors')
    try:
        asyncio.run(
            editor.propose_low_stakes_change(
                'secrets/prod.yaml', 'body', 'rationale'
            )
        )
    except BehaviorDenied:
        return
    finally:
        asyncio.run(editor.aclose())
    raise AssertionError('expected BehaviorDenied for secret-shaped path')


def test_category_mismatch_raises():
    editor = GitHubBehaviorEditor(token='ghp_dummy', owner='elobob-star', repo='behaviors')
    try:
        asyncio.run(
            editor.propose_low_stakes_change(
                'policies/autonomy_rules.md', 'body', 'rationale'
            )
        )
    except BehaviorDenied:
        return
    finally:
        asyncio.run(editor.aclose())
    raise AssertionError('expected BehaviorDenied for category mismatch')


def test_append_to_playbook_inserts_under_drift_header():
    text = (
        "# Jules Behavioral Playbook\n"
        "\n## Observed shape drift\n"
        "<!-- existing entry -->\n"
    )
    entry = PlaybookEntry(
        kind='plan', repo='a/b', task_id='t-1', gist='Approved on first pass.'
    )
    new_text = _append_to_playbook(text, entry)
    assert 'kind=plan' in new_text
    assert 'Approved on first pass' in new_text
    # The drift header is the first anchor; ordering matters because
    # the file is append-only in human reading order.
    assert new_text.index('## Observed shape drift') < new_text.index('Approved on first pass')


def test_append_to_playbook_rejects_unknown_kind():
    import pytest
    text = '# Antoine\n\n## Observed shape drift\n'
    entry = PlaybookEntry(kind='bogus', repo='a/b', task_id='t', gist='x')
    with pytest.raises(ValueError):
        _append_to_playbook(text, entry)


def _json_response(payload: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


async def _drive_open_pr(transport: httpx.MockTransport) -> str:
    """Drive a ``propose_low_stakes_change`` end-to-end with a mock transport."""
    editor = GitHubBehaviorEditor(
        token='ghp_dummy', owner='elobob-star', repo='behaviors'
    )
    editor._http = httpx.AsyncClient(
        base_url='https://api.github.com',
        headers={
            'Authorization': 'Bearer ghp_dummy',
            'Accept': 'application/vnd.github+json',
        },
        transport=transport,
        timeout=30.0,
    )
    try:
        return await editor.propose_low_stakes_change(
            'prompts/plan_review.md',
            'new body',
            'tightening the off-goal signal',
        )
    finally:
        await editor.aclose()


async def test_open_pr_records_full_request_flow():
    """Verify the four-request flow: ref -> contents -> pulls."""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == 'GET' and request.url.path.endswith(
            '/git/ref/heads/main'
        ):
            return _json_response({'object': {'sha': 'base-sha-abc'}})
        if request.method == 'POST' and request.url.path.endswith('/git/refs'):
            return _json_response({'ref': 'refs/heads/self-improve/...'}, status_code=201)
        if request.method == 'GET' and request.url.path.endswith(
            '/contents/prompts/plan_review.md'
        ):
            return _json_response({'sha': 'existing-sha'})
        if request.method == 'PUT' and request.url.path.endswith(
            '/contents/prompts/plan_review.md'
        ):
            payload = json.loads(request.content)
            # sha required because the file pre-existed
            assert payload['sha'] == 'existing-sha'
            decoded = base64.b64decode(payload['content']).decode()
            assert decoded == 'new body'
            return _json_response(
                {'content': {'sha': 'new-sha'}, 'commit': {'sha': 'commit-sha'}}
            )
        if request.method == 'POST' and request.url.path.endswith('/pulls'):
            payload = json.loads(request.content)
            assert payload['head'] != 'main'
            assert payload['base'] == 'main'
            assert payload['draft'] is False  # low-stakes don't draft
            return _json_response(
                {'html_url': 'https://github.com/elobob-star/behaviors/pull/42',
                 'number': 42},
                status_code=201,
            )
        raise AssertionError(f'unexpected call {request.method} {request.url.path}')

    transport = httpx.MockTransport(handler)
    pr_url = await _drive_open_pr(transport)
    assert pr_url == 'https://github.com/elobob-star/behaviors/pull/42'
    methods = [m for m, _ in calls]
    assert methods == ['GET', 'POST', 'GET', 'PUT', 'POST']


async def test_open_pr_drafts_for_behavioural():
    """Behavioural PRs land as drafts; the owner approves via the gateway."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == 'GET' and request.url.path.endswith(
            '/git/ref/heads/main'
        ):
            return _json_response({'object': {'sha': 'base'}})
        if request.method == 'POST' and request.url.path.endswith('/git/refs'):
            return _json_response({}, status_code=201)
        if request.method == 'GET' and request.url.path.endswith(
            '/contents/policies/autonomy_rules.md'
        ):
            return _json_response({'sha': 'sha-a'})
        if request.method == 'PUT' and request.url.path.endswith(
            '/contents/policies/autonomy_rules.md'
        ):
            return _json_response({'content': {'sha': 'sha-b'}})
        if request.method == 'POST' and request.url.path.endswith('/pulls'):
            payload = json.loads(request.content)
            assert payload['draft'] is True
            return _json_response({'html_url': 'https://x/pull/7', 'number': 7})
        raise AssertionError(f'unexpected {request.method} {request.url.path}')

    editor = GitHubBehaviorEditor(
        token='ghp_dummy', owner='elobob-star', repo='behaviors'
    )
    editor._http = httpx.AsyncClient(
        base_url='https://api.github.com',
        headers={'Authorization': 'Bearer ghp_dummy',
                 'Accept': 'application/vnd.github+json'},
        transport=httpx.MockTransport(handler),
        timeout=30.0,
    )
    try:
        await editor.propose_behavioral_change(
            'policies/autonomy_rules.md', 'new autonomy text', 'rationale'
        )
    finally:
        await editor.aclose()


async def test_record_playbook_writes_directly_to_main():
    """Append-only data flows straight to ``main``; no branch, no PR."""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == 'GET' and request.url.path.endswith(
            '/contents/playbook/jules-playbook.md'
        ):
            current = '# Jules Behavioral Playbook\n\n## Observed shape drift\n<!-- anchor -->\n'
            return _json_response({'sha': 'sha-x', 'content': _b64(current)})
        if request.method == 'PUT' and request.url.path.endswith(
            '/contents/playbook/jules-playbook.md'
        ):
            payload = json.loads(request.content)
            decoded = base64.b64decode(payload['content']).decode()
            assert 'kind=plan' in decoded
            assert 'repo=a/b' in decoded
            assert payload['branch'] == 'main'
            return _json_response({'commit': {'sha': 'commit-x'}})
        raise AssertionError(f'unexpected {request.method} {request.url.path}')

    editor = GitHubBehaviorEditor(
        token='ghp_dummy', owner='elobob-star', repo='behaviors'
    )
    editor._http = httpx.AsyncClient(
        base_url='https://api.github.com',
        headers={'Authorization': 'Bearer ghp_dummy',
                 'Accept': 'application/vnd.github+json'},
        transport=httpx.MockTransport(handler),
        timeout=30.0,
    )
    try:
        await editor.record_playbook_entry(
            PlaybookEntry(kind='plan', repo='a/b', task_id='t-1', gist='Approved clean.')
        )
    finally:
        await editor.aclose()
    methods = [m for m, _ in calls]
    assert methods == ['GET', 'PUT']
