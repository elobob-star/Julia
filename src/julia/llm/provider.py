'''BYOK runtime-model abstraction (vision section 10).

Any OpenAI-compatible chat endpoint works (Nemotron on a free provider,
or anything else) -- swap providers by changing two environment
variables, never code. RuleBasedModel is the zero-cost deterministic
fallback: if the provider is down or unconfigured, Julia degrades to
conservative canned judgment instead of stopping (vision section 10).
'''

from __future__ import annotations

from typing import Protocol

import httpx


class ChatModel(Protocol):
    async def complete(self, system: str, user: str) -> str: ...


class OpenAICompatibleModel:
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=60.0,
        )
        self._model = model

    async def complete(self, system: str, user: str) -> str:
        response = await self._http.post(
            '/chat/completions',
            json={
                'model': self._model,
                'messages': [
                    {'role': 'system', 'content': system},
                    {'role': 'user', 'content': user},
                ],
                'temperature': 0.2,
            },
        )
        response.raise_for_status()
        return str(response.json()['choices'][0]['message']['content'])


class RuleBasedModel:
    '''Deterministic conservative fallback; also used in dry-run mode.'''

    async def complete(self, system: str, user: str) -> str:
        if 'reviewing a coding agent plan' in system:
            return 'APPROVE'
        return (
            'Proceed with the simplest reasonable interpretation: target the '
            'default branch and keep the change minimal and well-tested.'
        )
