# Julia — Operator Runbook

This is the plain-language operator's manual for Julia. It is intended
for **me** (the owner), not for engineers — I am not a developer and
I should be able to install, restore, panic, and migrate a host
without re-reading the source.

For the *what* and *why*, see [`vision.md`](../Vision%20and%20docs/vision.md).
For the *how*, see this file.

---

## What is running

Julia is one Python process that:

1. listens to a chat (the **gateway**),
2. drives Jules sessions,
3. watches the resulting PRs on GitHub,
4. merges them when the **autonomy ladder** says it's safe to.

It runs 24/7 under a service supervisor (`launchd` on macOS, `systemd`
on Linux). The watchdog hierarchy in `src/julia/watchdog.py` keeps
the process alive across crashes; an **external dead-man's switch**
(`JULIA_HEARTBEAT_URL`) tells me when the host itself has died —
because a dead machine cannot report its own death.

---

## Quickstart (dry-run, no credentials)

```bash
cd Julia
julia run --dry-run
```

This is the rehearsal mode. It exercises the full spine — gateway
message → Jules session (fake) → plan review → clarification
answer → PR → quality gates → merge → notification — with zero
quota cost, zero credentials, zero network. Use it to verify
behaviour changes before going live.

---

## Quickstart (live)

```bash
cd Julia
export JULIA_JULES_API_KEY=...
export JULIA_GITHUB_TOKEN=...
export JULIA_DEFAULT_REPO=owner/name
# Optional: jump to a Telegram gateway instead of the local console.
export JULIA_TELEGRAM_BOT_TOKEN=...
export JULIA_TELEGRAM_CHAT_ID=...
# Optional: external dead-man's switch (e.g. healthchecks.io).
export JULIA_HEARTBEAT_URL=https://hc-ping.com/abc-123

julia run
```

To enable the behavioural playbook (vision §8) so Julia learns
from each session and opens reviewable PRs against its own
behaviour repo:

**Live (preferred):** point Julia at the GitHub repo so every
behaviour change opens a real PR through the GitHub API.

```bash
export JULIA_BEHAVIORS_REPO=elobob-star/behaviors
# JULIA_GITHUB_TOKEN is already required for live runs
julia run
```

`JULIA_BEHAVIORS_REPO` takes precedence over `JULIA_BEHAVIORS_PATH`.
Playbook entries commit *directly* to `main` (append-only data,
never policy); low-stakes prompt changes and behavioural policy
changes both go through PR review on the configured repo.

**Offline:** for development without network calls:

```bash
julia run --behaviors /path/to/behaviors
```

`/path/to/behaviors` should be a git checkout of the **behaviors**
repo (sibling of `Julia/`). Playbook entries write straight
to the local file; prompt / policy changes commit locally.

---

## Slash commands (the gateway's control surface)

These work from any gateway — the local console during dry-run or
Telegram during live runs. Anything else is treated as a development
task and, rung permitting, dispatched to Jules.

| Command | What it does |
| --- | --- |
| `/status` | Current rung, remaining quota (rolling 24h), task counts by state |
| `/digest` | One-glance summary: shipped, in flight, blocked, anomalies, quota posture |
| `/approve <task-id>` | Merge a PR that ladder policy left queued for approval |
| `/explain <task-id>` | The decision trace behind a task — inputs, options, action taken |
| `/playbook [task-id]` | Recent behavioural playbook entries; optional task filter |
| `/improve <file>:<category> <new-content>` | Open a behaviour PR through the editor |
| `/rung <0-4>` | Set the autonomy ladder (0 safe, 4 full auto) |
| `/panic` | Drop to SAFE_MODE immediately |
| `/help` | List commands |

`/improve` requires `--behaviors PATH`. From a rung other than
SAFE_MODE, it opens a PR: **low-stakes** if the path is under
`prompts/` or `playbook/`, **behavioural** if it's under `policies/`.
The locked file `policies/safety.md` and anything matching the
secret denylist will be refused.

---

## Autonomy ladder (what the rungs mean)

| Rung | Plans | Executes | Merges (gates passed) | Owner ping |
| --- | --- | --- | --- | --- |
| 0 SAFE_MODE | yes | no | no | every action |
| 1 PROPOSE_ONLY | yes (queued) | no | no | every plan |
| 2 SUPERVISED | yes | yes | no — queues for `/approve` | each queue |
| 3 AUTO_NOTIFY | yes | yes | yes | per merge |
| 4 FULL_AUTO | yes | yes | yes | digest only |

The ladder is per-repo: set `rung:acme/app` to 2 and `rung:__global__`
to 3, and Julia will supervise Acme while autonomously merging
helpers elsewhere.

After three anomalies in one repo (or globally), the ladder
auto-drops one rung and the anomaly counter resets. Recovery is via
`/rung N` from the gateway. This is intentional — confidence is
recovered from observed stability, not by ticking itself up.

---

## Panic stop

`/panic` drops the ladder to SAFE_MODE (0). It is the only
unconditional command. Recovery is via `/rung N`, a deliberate
operator action.

If Julia itself has crashed, the OS supervisor (launchd/systemd)
restarts the process. If the host has died, the external
heartbeat service sends me an alert — I handle the rest by hand.

---

## Backup and restore

One command, one file. The SQLite ledger is the only state that
needs to survive a restart.

```bash
julia backup /path/to/copy.db      # local file backup
julia restore /path/to/copy.db     # restore onto any host
```

A cron-style remote backup just runs `julia backup` against a
mounted volume. **Tested by the test suite** (`tests/test_state.py`).

---

## Migrating to a new host

1. Transfer `~/.julia/julia.db` (the SQLite ledger) and the
   behaviours clone (`--behaviors PATH`) to the new machine.
2. Install Julia (`pip install -e ".[dev]"`).
3. Restore the ledger (`julia restore /path/to/copy.db`).
4. Run `julia run` with the same env vars.

The orchestrator's `_resume` rehydrates in-flight tasks (`PLANNING`,
`EXECUTING`, `REVIEWING`) automatically. A reboot never orphans work.

---

## Reading what happened

- `/explain <task-id>` returns the decision trace: who acted, what
  they did, and why. Pure chronological text.
- `/playbook` returns recent entries from the behavioural playbook
  (when `--behaviors` is configured).
- `julia status` prints the task ledger with state per task.
- The decision log lives in the SQLite `decisions` table; query it
  with `sqlite3 ~/.julia/julia.db "SELECT * FROM decisions WHERE
  task_id = '<id>'"`.

---

## Phased rollout

- **Phase 0** — Walking skeleton: one message in, one PR out, one
  notification back.
- **Phase 1** — Core loop: parallel sessions, plan handling,
  clarification answering, PR review + quality gates, watchdog
  tiers, durable state, quota handling, secrets workspace, autonomy
  ladder.

  ✅ **Delivered in this build**.
- **Phase 2** — Harness intelligence: analytics, two-layer memory,
  behavioural playbook, self-improvement loop, notification
  routing.

  ✅ **Delivered in this build** — see `behaviors/`.
  The prompt regression suite now runs on every push via
  `elobob-star/behaviors/.github/workflows/test-prompts.yml`.
  Low-stakes behaviour PRs auto-merge after that workflow goes
  green; behavioural ones stay in `AWAITING_APPROVAL` until
  `/approve-behavior <pr-url>` is sent.
- **Phase 3** — Polish: gateway UX, repo onboarding wizard, task
  templates, NotebookLM adapter.

  🚧 Phase 3 backlog; the answers repo and `GitHubBehaviorEditor`
  will land there.
