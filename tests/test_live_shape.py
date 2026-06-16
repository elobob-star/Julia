"""Tests the orchestrator's reaction to live-wire activity shapes.

Verified shape observations are baked into `behaviors/playbook/jules-api-shape.md`
(PR elobob-star/behaviors#1). These tests exercise the orchestrator-side
projection so a future dossier edit cannot regress the wire compatibility
without a red CI.

The shapes below are verbatim from `/sessions/2421384606932503230/activities`
observed on 2026-06-16.
"""

from __future__ import annotations

from julia.jules import dossier


def test_plan_generated_classifies_as_plan():
    activity = {
        'id': 'activities/abc',
        'name': 'sessions/123/activities/abc',
        'originator': 'agent',
        'planGenerated': {
            'plan': {
                'id': 'plan-x',
                'steps': [
                    {'id': 's0', 'title': 'Create CANARY.md', 'index': 0},
                    {'id': 's1', 'title': 'Pre-commit steps', 'index': 1},
                    {'id': 's2', 'title': 'Submit the changes', 'index': 2},
                ],
            }
        },
    }
    assert dossier.classify_activity(activity) == 'plan'
    text = dossier.extract_plan_text(activity)
    assert 'Create CANARY.md' in text
    assert '1.' in text and '3.' in text


def test_plan_approved_is_progress():
    activity = {
        'id': 'activities/approved',
        'planApproved': {'planId': 'plan-x'},
    }
    # planApproved is a moment in time, not a work state; treat as
    # progress so the orchestrator keeps polling without acting.
    assert dossier.classify_activity(activity) == 'progress'


def test_agent_messaged_classifies_as_question():
    activity = {
        'id': 'activities/q',
        'agentMessaged': {'agentMessage': 'Which branch should I target?'},
    }
    assert dossier.classify_activity(activity) == 'question'


def test_session_completed_extracts_pr_url():
    activity = {
        'id': 'activities/c',
        'sessionCompleted': {},
    }
    assert dossier.classify_activity(activity) == 'completed'


def test_session_completed_with_pr_url():
    activity = {
        'id': 'activities/c2',
        'sessionCompleted': {},
        'pullRequestUrl': 'https://github.com/example/repo/pull/7',
    }
    assert dossier.classify_activity(activity) == 'completed'
    assert dossier.extract_pr_url(activity) == 'https://github.com/example/repo/pull/7'


def test_session_completed_with_only_gitpatch():
    activity = {
        'id': 'activities/c3',
        'sessionCompleted': {},
        'artifacts': [
            {
                'changeSet': {
                    'source': 'sources/github/example/repo',
                    'gitPatch': {
                        'unidiffPatch': 'diff --git a/CANARY.md b/CANARY.md\n',
                        'baseCommitId': 'abc123',
                    },
                }
            }
        ],
    }
    assert dossier.classify_activity(activity) == 'completed'
    assert dossier.extract_pr_url(activity) == ''
    patch = dossier.extract_git_patch(activity)
    assert patch.startswith('diff --git')


def test_session_failed_classifies():
    activity = {'id': 'activities/f', 'sessionFailed': {'reason': 'budget exhausted'}}
    assert dossier.classify_activity(activity) == 'failed'


def test_activity_key_prefers_id():
    activity = {
        'id': 'unique-activity-id',
        'sessionCompleted': {},
    }
    assert dossier.activity_key(activity) == 'id:unique-activity-id'


def test_activity_key_falls_back_when_no_id():
    activity = {
        'sessionCompleted': {},
        'pullRequestUrl': 'https://github.com/x/y/pull/3',
    }
    key = dossier.activity_key(activity)
    assert key.startswith('completed:')
    assert 'pull/3' in key

def test_list_activities_handles_transient_404():
    """A freshly-created session may briefly 404; orchestrator retries.

    Verified live 2026-06-16: the session we created returned 404
    from /sessions/{id}/activities on the first poll, then 200 with
    activity data after a couple of seconds. The HttpJulesClient
    treats the initial 404 as "try again with backoff."
    """
    import asyncio
    import httpx

    from julia.jules.client import HttpJulesClient

    call_count = 0

    async def handler(request):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return httpx.Response(404, json={'error': 'session warmup'})
        return httpx.Response(200, json={'activities': [{'id': 'a1'}]})

    transport = httpx.MockTransport(handler)
    client = HttpJulesClient(
        api_key='AQ.test',
        base_url='https://jules.googleapis.com/v1alpha',
    )
    client._http = httpx.AsyncClient(
        base_url='https://jules.googleapis.com/v1alpha',
        headers={'X-Goog-Api-Key': 'AQ.test'},
        transport=transport,
        timeout=30.0,
    )
    # Patch the small sleep to avoid real time waiting in the test.
    real_sleep = asyncio.sleep
    asyncio.sleep = lambda s: real_sleep(0)
    try:
        activities = asyncio.run(client.list_activities('sessions/x'))
    finally:
        asyncio.sleep = real_sleep
        asyncio.run(client._http.aclose())
    assert len(activities) == 1
    assert call_count == 3  # two 404s, one 200
