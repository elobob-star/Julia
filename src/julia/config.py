'''Runtime configuration and the secrets workspace (vision sections 15, 21).

All credentials arrive via environment variables prefixed JULIA_ (or an
.env file next to the working directory), are validated at startup, and
are held as SecretStr so they never leak into logs or decision traces.
'''

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='JULIA_', env_file='.env', extra='ignore')

    # Mode
    dry_run: bool = False

    # Durable state
    state_dir: Path = Path.home() / '.julia'

    # Jules
    jules_api_key: SecretStr | None = None
    jules_base_url: str = 'https://jules.googleapis.com/v1alpha'
    jules_daily_quota: int = 100
    jules_canary_budget: int = 1

    # GitHub
    github_token: SecretStr | None = None
    github_api_url: str = 'https://api.github.com'
    default_repo: str | None = None  # 'owner/name'

    # Runtime model (BYOK, any OpenAI-compatible provider; vision section 10)
    model_api_key: SecretStr | None = None
    model_base_url: str | None = None
    model_name: str = 'nvidia/nemotron-3-ultra'

    # Behavior repo (vision section 8). Optional; the orchestrator
    # behaves exactly as Phase 1 when absent. When ``behaviors_repo``
    # is set (``owner/name``), the GitHub editor opens real PRs
    # against it. When only ``behaviors_path`` is set, the local
    # filesystem editor is used. With both, GitHub wins (live is the
    # authoritative posture).
    behaviors_path: Path | None = None
    behaviors_repo: str | None = None

    # Gateway (vision section 12). Telegram if configured, console otherwise.
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    # Watchdog / loop timing (vision section 5.3)
    heartbeat_url: str | None = None  # external dead-man's switch, e.g. healthchecks.io
    heartbeat_interval_s: int = 300
    poll_interval_s: int = 15
    stall_timeout_s: int = 1800
    # Step 5: how often the PR watcher polls each behaviour PR for
    # CI status (vision §5.4). Default 60s keeps GitHub's rate
    # limits happy while still feeling live during /improve flows.
    poll_prs_interval_s: int = 60

    # Daily rhythm (vision sections 5.6 and 6)
    digest_interval_s: int = 86400
    canary_repo: str | None = None  # sandbox repo for the daily canary probe

    def db_path(self) -> Path:
        return self.state_dir / 'julia.db'

    def validate_live(self) -> list[str]:
        '''Return missing credential names for live (non-dry-run) operation.'''
        missing: list[str] = []
        if not self.jules_api_key:
            missing.append('JULIA_JULES_API_KEY')
        if not self.github_token:
            missing.append('JULIA_GITHUB_TOKEN')
        return missing
