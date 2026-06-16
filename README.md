# Julia

An always-on AI developer orchestrator that sits on top of [Jules](https://jules.google/)
and GitHub and runs the full development loop autonomously: it prompts Jules,
reviews and approves plans, answers clarifications, runs parallel sessions,
reviews pull requests, merges or sends them back, recovers from stalls, and
reports back through a messaging gateway.

See [`vision.md`](vision.md) for the full brief. The guiding idea is
**behavior as code**: everything Julia believes about the outside world
(Jules' behavior, prompts, policies) is isolated and correctable through
small, reviewable changes.

## Requirements

- Python **3.12+**
- A Jules API key and a GitHub token for live operation (not needed for `--dry-run`)

## Installation

```bash
git clone https://gitlab.com/bobincorp-group/julia.git
cd julia
pip install -e ".[dev]"
```

This installs the `julia` command (see `[project.scripts]` in `pyproject.toml`).

## Configuration

All settings are read from environment variables prefixed `JULIA_` (or an
`.env` file in the working directory) and validated at startup. Credentials
are held as secrets and never written to logs or decision traces.

| Variable | Purpose | Default |
| --- | --- | --- |
| `JULIA_JULES_API_KEY` | Jules API key (required for live runs) | — |
| `JULIA_GITHUB_TOKEN` | GitHub access token (required for live runs) | — |
| `JULIA_DEFAULT_REPO` | Default `owner/name` repo for new tasks | — |
| `JULIA_MODEL_API_KEY` / `JULIA_MODEL_BASE_URL` / `JULIA_MODEL_NAME` | BYOK runtime model (any OpenAI-compatible provider) | rule-based fallback |
| `JULIA_TELEGRAM_BOT_TOKEN` / `JULIA_TELEGRAM_CHAT_ID` | Telegram gateway (console gateway if unset) | console |
| `JULIA_HEARTBEAT_URL` | External dead-man's switch endpoint | — |
| `JULIA_JULES_DAILY_QUOTA` | Rolling 24h Jules task budget | `100` |

See `src/julia/config.py` for the complete list.

## Usage

```bash
julia run --dry-run    # full rehearsal with fakes; spends nothing
julia run              # start the orchestrator (live; needs credentials)
julia status           # print the task ledger
julia backup DEST      # back up durable state to DEST
julia restore SRC      # restore durable state from a backup
```

Once running, send plain-text requests through the gateway to create
development tasks. Slash commands control the orchestrator:

- `/status` — current rung, remaining quota, task counts
- `/digest` — one-glance daily summary
- `/approve <task-id>` — merge a PR queued for approval
- `/explain <task-id>` — the decision trace behind a task
- `/rung <0-4>` — set the autonomy ladder (0 safe → 4 full auto)
- `/panic` — drop to safe mode immediately
- `/help` — list commands

## Project layout

```
src/julia/
  orchestrator.py   core loop: intake → session → review → merge → notify
  config.py         settings and the secrets workspace
  autonomy.py       the autonomy ladder
  quota.py          rolling 24h Jules quota guard
  state.py          durable task ledger and decision log (backup/restore)
  watchdog.py       watchdog hierarchy and heartbeat
  models.py         Task and shared data models
  cli.py            command-line entry point
  jules/            Jules client and behavioral dossier
  gh/               GitHub client
  llm/              BYOK runtime model providers
  gateway/          console and Telegram gateways
tests/              end-to-end and unit rehearsals (run with --dry-run parity)
```

## Development

The project is linted, type-checked, and tested in CI:

```bash
ruff check src tests
mypy src
pytest -q
```

## Status

Phase 1 (core loop) per `vision.md` §19. This serves a single user and is
not a multi-user or SaaS product.
