# AGENTS.md — `Julia/`

> This is the orientation file for any agent (or human) working in
> this project. It's deliberately a single file rather than a stack
> of per-directory ones, and it's deliberately a *living document*
> rather than a frozen spec — see [§ Keeping this current](#keeping-this-current)
> at the bottom for how to evolve it.

## What this is

`julia-orchestrator` is an always-on AI developer orchestrator sitting
on top of [Jules](https://jules.google/) and GitHub. The full product
brief lives at [`../Vision and docs/vision.md`](../Vision%20and%20docs/vision.md);
a copy is mirrored as `vision.md` next to this file for convenience.
The canonical version is the one in `Vision and docs/`.

The short version: a single, autonomous "developer on call" that
takes plain-text requests, drives Jules sessions, reviews pull
requests, and only bothers its owner when it has to. Phase 1 of the
vision is what's implemented here; the rest is the roadmap.

The project is single-user, single-host, 24/7, currently at
`0.1.0`. It's not a SaaS and not multi-tenant — design choices
follow from that.

## Quick orientation

```
Julia/
  src/julia/
    cli.py             argparse entry point; wires Settings → collaborators → Orchestrator
    config.py          pydantic-settings Settings; the JULIA_ env namespace
    models.py          Task, TaskState, ids, JSON round-trip (kind + source_url added in Phase 2)
    orchestrator.py    the core loop (intake → session → review → merge → notify)
                       plus _pr_watcher_loop and _approve_behavior
    autonomy.py        the five-rung autonomy ladder
    quota.py           rolling 24h Jules quota guard with canary reserve
    state.py           SQLite Store: tasks / decisions / quota / kv / backup
    watchdog.py        in-process watchdog + external heartbeat pings
    behavior/          vision §8 PR opener (Fake + Local + GitHub editors)
                      +_safety.py     safety categoriser shim — reads canonical
                                       from behaviors/scripts/_safety.py with
                                       vendored fallback for fresh installs
                      └─editor.py     re-exports categorise / Category / DENYLIST
                                       from _safety (vision §15 single-source)
    jules/             Jules HTTP + fake client + behavioral dossier
    gh/                GitHub HTTP + fake client (incl. Jules outputs → PR translator)
    llm/               BYOK runtime model (OpenAI-compatible) + rule-based fallback
    gateway/           console + telegram + in-memory gateways
  tests/               pytest (asyncio, fakes only — no network, no live creds)
  deploy/systemd/      Fedora systemd unit (Tier-1 of watchdog hierarchy, vision §16);
                       install.sh is idempotent install/uninstall
  scripts/             systemd_smoke.py (CI guard on the unit), live smokes
  vision.md            copy of the canonical product brief
  RUNBOOK.md           plain-language operator manual for the owner
  README.md            human-facing overview on a repo browser
  pyproject.toml       build + dependencies + tooling config
  .gitlab-ci.yml       CI: lint, typecheck, test on python:3.12-slim

behaviors/  (sibling repo, see ../behaviors/README.md)
  .github/workflows/   GitHub Actions test-prompts.yml — runner for vision §5.4
                       (low-stakes PRs auto-merge after this workflow is green)
  playbook/            living Jules quirks log (vision §8)
  prompts/             versioned plan-review / clarification / canary prompts
  policies/            autonomy rules, quality gates, safety boundaries
  tests/               prompt regression suite + safety equivalence tests
                       (cross-imports self_improve against _safety)
  scripts/             _safety.py (canonical categoriser) + self_improve.py
                       + playbook_append.py
```

The orchestrator and its collaborators are wired together in
`cli.build_orchestrator`. Everything is constructor-injected through
`typing.Protocol` classes (`JulesAPI`, `GitHubAPI`, `ChatModel`,
`Gateway`), so the test suite and `--dry-run` mode swap in fakes
without touching production code.

## Conventions (these are conventions, not laws)

These describe the prevailing style. They aren't load-bearing
invariants; if a clear improvement calls for breaking one, break it
and update this file in the same PR.

- **Async-first.** Anything that touches I/O is `async def`. The
  orchestrator, the session driver, and the LLM/Jules/GitHub/gateway
  calls are all awaited. Plain `def` is fine for pure functions and
  type conversions.
- **Protocols for collaborators.** `JulesAPI`, `GitHubAPI`, `ChatModel`,
  and `Gateway` are `typing.Protocol`. Production code uses the HTTP
  implementations; tests and `--dry-run` use deterministic fakes.
  Collaborators are constructor-injected — no global state, no
  monkey-patching.
- **Fakes mirror real behavior.** `FakeJulesClient` walks
  `planned → asked → completed`; `FakeGitHubClient` records side
  effects. If you add a new field to the real client, add it to the
  fake too.
- **Pydantic `SecretStr` for every credential.** `Settings.validate_live()`
  enumerates missing keys for the CLI. Raw secrets never appear in
  logs or decision traces.
- **Idempotent where it matters.** `merge_pr` re-checks `merged` and
  returns `False` instead of erroring on a retried merge. Quota
  acquisitions are persisted so restarts don't double-count.
- **Decision traces for consequential actions.** `Store.record_decision(actor, action, reason, task_id)`
  powers `/explain`. If the action isn't trivial, log it. Common
  actors in use today: `orchestrator`, `ladder`, `canary`, `owner`.
- **Behavior as code.** Prompts, policies, and the Jules behavioral
  dossier (`src/julia/jules/dossier.py`) are version-controlled
  artifacts. They change only through reviewable commits — see
  vision §5.4 and §8. If you spot Jules drift, fix it in the
  dossier, not in the orchestrator.
- **Type hints everywhere** with `from __future__ import annotations`.
  Mypy is in CI; keep it green.
- **One module-level state allowed: the `dossier` constants.** They
  are the system of record for Jules behavior, and they sit in
  `dossier.py` precisely so they're easy to correct.

## Tech stack

- **Language:** Python 3.12+ (uses `asyncio.TaskGroup`)
- **HTTP:** `httpx` (async)
- **Config / secrets:** `pydantic-settings` + `pydantic.SecretStr`
- **Durable state:** `sqlite3` (stdlib, single file, backed up via
  the SQLite backup API)
- **Tests:** `pytest`, `pytest-asyncio` (auto mode), `pytest-timeout`
- **Lint:** `ruff` (line length 100)
- **Types:** `mypy` (`check_untyped_defs = true`)
- **CI:** GitLab CI (`.gitlab-ci.yml`)

## Slash commands (the gateway's control surface)

The orchestrator accepts these from any gateway (console or
Telegram). Anything else is treated as a development task.

- `/status` — current rung, remaining quota, task counts
- `/digest` — one-glance daily summary
- `/approve <task-id>` — merge a PR queued for approval
- `/explain <task-id>` — the decision trace behind a task
- `/playbook [task-id]` — recent behavioural playbook entries
- `/improve <file>:<category> <new-content>` — open a behaviour PR
  (`low-stakes` or `behavioural`); refused when no editor is wired
- `/rung <0-4>` — set the autonomy ladder (0 safe → 4 full auto)
- `/panic` — drop to safe mode immediately
- `/help` — list commands

The single dispatch point is `orchestrator._command()` — add new
commands there, not scattered across modules.

## Configuration

Everything is `JULIA_`-prefixed env vars (or a `.env` file in the
working directory). Full list in `src/julia/config.py`; the ones you
actually need to know:

| Env var / CLI | Purpose |
| --- | --- |
| `JULIA_JULES_API_KEY` | Jules API key (required live) |
| `JULIA_GITHUB_TOKEN` | GitHub access token (required live) |
| `JULIA_DEFAULT_REPO` | `owner/name` for tasks without an explicit repo |
| `JULIA_BEHAVIORS_PATH` / `--behaviors PATH` | local checkout of the `behaviors/` repo (vision §8); offline-mode editor |
| `JULIA_BEHAVIORS_REPO` | `owner/name` GitHub repo; live editor (preferred when set) |
| `JULIA_MODEL_API_KEY` / `JULIA_MODEL_BASE_URL` / `JULIA_MODEL_NAME` | BYOK runtime model |
| `JULIA_TELEGRAM_BOT_TOKEN` / `JULIA_TELEGRAM_CHAT_ID` | Telegram gateway (else console) |
| `JULIA_HEARTBEAT_URL` | External dead-man's switch endpoint |
| `JULIA_JULES_DAILY_QUOTA` | Rolling 24h Jules budget (default 100) |

## Development workflow

```bash
ruff check src tests       # lint (must pass in CI)
mypy src                   # typecheck (must pass in CI)
pytest -q                  # full test suite (must pass in CI)
```

CI runs the same three jobs on `python:3.12-slim`. There's no merge
without green CI. The end-to-end test file (`tests/test_end_to_end.py`)
is the spec for what the orchestrator does — when you change a code
path, update the test in the same PR.

To rehearse the full system without spending anything:

```bash
julia run --dry-run
```

`--dry-run` exercises the same code path the test suite does — fakes
for Jules, GitHub, and the model, console gateway, zero quota.

## Backups

One command, one file. The SQLite ledger is the only state that
needs to survive a restart.

```bash
julia backup DEST       # writes a self-contained copy of the ledger
julia restore SRC       # restores from a backup
```

Restoring onto a fresh machine is a documented procedure (vision
§14) — the portability proof, not an aspiration.

## On vision and design choices

The vision is the floor, not the ceiling — it's deliberately
generous with creative license. If you find a smarter design, build
that. The numbered vision sections are referenced throughout this
file (e.g. §5.5 for the autonomy ladder, §7 for Jules behavior,
§8 for memory and self-improvement); verify them against the
canonical `Vision and docs/vision.md` when in doubt.

This is a single-user, single-host hobby setup that also wants to
be production-grade. Those two things pull against each other
sometimes — when they do, prefer the boring, well-tested option
and document the choice in the relevant module.

## Keeping this current

This file is a living document. The standard is:

**When you change the project in a way an agent would need to know
about, update this file in the same PR.**

That includes, but is not limited to:

- Adding, removing, or renaming a module under `src/julia/`
- Changing a public Protocol or its method signatures
- Adding or changing a slash command, configuration flag, or env var
- Switching or upgrading a dependency (e.g. swapping `httpx` for
  something else, or bumping Python)
- Changing the test layout, the CI pipeline, or the dev commands
- Adding layout in the sibling `behaviors/` repo (it has its own
  conventions — read [`../behaviors/README.md`](../behaviors/README.md)).
- Updating the operator runbook (`RUNBOOK.md`) so it stays in sync
  with the actual surface.
- Discovering a convention that the existing text doesn't capture
- Discovering a convention the existing text captures *wrong*

The point isn't perfection; it's that the next agent shouldn't have
to rediscover the project. If a future contributor finds something
in this file that's stale, wrong, or missing, the fix is a small
reviewable change — exactly the same posture the orchestrator takes
toward its own behavior (vision §8).

When you update this file, prefer *less* rigidity: if a rule has
nuanced exceptions, say so. If something is a tendency rather than
a rule, frame it as a tendency. A living document is allowed to
disagree with itself across time as long as the disagreement is
visible.

## Cross-references

- Vision: [`../Vision and docs/vision.md`](../Vision%20and%20docs/vision.md)
  (canonical) / [`vision.md`](vision.md) (mirror)
- README: [`README.md`](README.md) (human-facing)
- Jules behavioral dossier: `src/julia/jules/dossier.py` — *the*
  system of record for Jules assumptions
- Core loop: `src/julia/orchestrator.py` — `run()`, `_serve()`,
  `_drive_session()`, `_command()`
- Test spec for the spine: `tests/test_end_to_end.py`
