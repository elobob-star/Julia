'''Jules API client plus a deterministic fake for dry-run mode and tests.

The HTTP client is deliberately thin: all behavioral knowledge lives in
dossier.py so corrections to Jules assumptions never touch transport
code (vision sections 7 and 8).
'''

from __future__ import annotations

from typing import Any, Protocol

import httpx

from . import dossier


class JulesAPI(Protocol):
    async def create_session(self, prompt: str, repo: str) -> str: ...

    async def get_session(self, session_id: str) -> dict[str, Any]: ...

    async def list_activities(self, session_id: str) -> list[dict[str, Any]]: ...

    async def approve_plan(self, session_id: str) -> None: ...

    async def send_message(self, session_id: str, text: str) -> None: ...


class HttpJulesClient:
    def __init__(self, api_key: str, base_url: str = dossier.DEFAULT_BASE_URL) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={dossier.API_KEY_HEADER: api_key},
            timeout=30.0,
        )

    async def create_session(self, prompt: str, repo: str) -> str:
        response = await self._http.post(
            '/sessions',
            json={
                'prompt': prompt,
                'sourceContext': {
                    'source': f'sources/github/{repo}',
                    'githubRepoContext': {'startingBranch': 'main'},
                },
            },
        )
        response.raise_for_status()
        return str(response.json()['name'])  # e.g. 'sessions/abc123'

    async def get_session(self, session_id: str) -> dict[str, Any]:
        response = await self._http.get(f'/{session_id}')
        response.raise_for_status()
        return dict(response.json())

    async def list_activities(self, session_id: str) -> list[dict[str, Any]]:
        response = await self._http.get(f'/{session_id}/activities')
        response.raise_for_status()
        return list(response.json().get('activities', []))

    async def approve_plan(self, session_id: str) -> None:
        response = await self._http.post(f'/{session_id}:approvePlan')
        response.raise_for_status()

    async def send_message(self, session_id: str, text: str) -> None:
        response = await self._http.post(f'/{session_id}:sendMessage', json={'prompt': text})
        response.raise_for_status()


class FakeJulesClient:
    '''Deterministic Jules stand-in: plan -> question -> completed with PR.

    Used by dry-run mode (vision section 5.6) and the test suite, so the
    whole spine can be rehearsed without spending quota or credentials.
    '''

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._count = 0

    async def create_session(self, prompt: str, repo: str) -> str:
        self._count += 1
        session_id = f'sessions/fake-{self._count}'
        self._sessions[session_id] = {
            'phase': 'planned',
            'repo': repo,
            'prompt': prompt,
            'number': self._count,
        }
        return session_id

    async def get_session(self, session_id: str) -> dict[str, Any]:
        session = self._sessions[session_id]
        state = 'COMPLETED' if session['phase'] == 'completed' else 'IN_PROGRESS'
        return {'name': session_id, 'state': state}

    async def list_activities(self, session_id: str) -> list[dict[str, Any]]:
        session = self._sessions[session_id]
        activities: list[dict[str, Any]] = [
            {'type': 'plan_generated', 'plan': '1. Implement the change. 2. Add tests.'}
        ]
        if session['phase'] in ('asked',):
            activities.append(
                {'type': 'agent_question', 'question': 'Which branch should I target?'}
            )
        if session['phase'] == 'completed':
            repo = session['repo']
            number = session['number']
            activities.append(
                {
                    'type': 'session_completed',
                    'pullRequestUrl': f'https://github.com/{repo}/pull/{number}',
                }
            )
        return activities

    async def approve_plan(self, session_id: str) -> None:
        self._sessions[session_id]['phase'] = 'asked'

    async def send_message(self, session_id: str, text: str) -> None:
        session = self._sessions[session_id]
        if session['phase'] == 'asked':
            session['phase'] = 'completed'

    async def list_artifacts(self, session_id):
        session = self._sessions[session_id]
        if session['phase'] != 'completed':
            return []
        return [
            {
                'changeSet': {
                    'source': f"sources/github/{session['repo']}",
                    'gitPatch': {'unidiffPatch': 'fake unidiff'},
                },
                'kind': 'gitPatch',
            }
        ]
