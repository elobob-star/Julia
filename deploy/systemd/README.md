# Deploying Julia on Fedora (systemd)

Vision §16 says the orchestrator runs 24/7 under a service
supervisor. Tier 2 (in-process watchdog, in `src/julia/watchdog.py`)
and Tier 3 (external heartbeat ping) already ship. This unit ships
**Tier 1** — the OS supervisor — for the Fedora target host.

The Mac Mini deployment uses a parallel LaunchAgent; that lives
under `../launchd/` and is not in this repo yet. Workflow parity,
not file parity, is the goal.

## Quickstart (Fedora 41+)

1. **Install Julia** in the system Python (not a venv — the unit
   uses `/usr/bin/env julia`, which `pip install -e .` puts on
   PATH globally):

   ```bash
   sudo useradd --system --no-create-home --shell /usr/sbin/nologin julia
   sudo install -d -o julia -g julia -m 0750 /opt/julia
   cd /opt/julia
   sudo -u julia git clone <julia-repo-url> .
   sudo -u julia pip install -e ".[dev]"
   ```

2. **Set credentials.** `/etc/julia/julia.env` is sourced by the
   unit. `install.sh install` writes a template; edit it with
   your secrets:

   ```bash
   sudo $EDITOR /etc/julia/julia.env
   sudo systemctl restart julia.service
   ```

3. **Install + start the unit:**

   ```bash
   sudo ./deploy/systemd/install.sh install
   ```

   Idempotent. Running it twice doesn't error.

4. **Verify it's running** (commands from the Runbook):

   ```bash
   systemctl --no-pager status julia.service
   journalctl -u julia.service -n 200 --no-pager
   julia status
   ```

   `julia status` prints the task ledger; an empty one is fine on
   first boot.

## Uninstalling

```bash
sudo ./deploy/systemd/install.sh uninstall
```

The unit file at `/etc/systemd/system/julia.service` is removed.
The credentials file at `/etc/julia/julia.env` is **kept** so a
reinstall preserves your env. Delete manually if you want a clean
slate.

## Verifying the unit before install

`scripts/systemd_smoke.py` parses the unit, verifies the
`[Unit] / [Service] / [Install]` sections are present, and
checks that `ExecStart` references a command that resolves to a
binary on a normal Fedora `$PATH`:

```bash
python scripts/systemd_smoke.py
```

CI on this repo runs the smoke on every PR. A future PR will add
a Forge Action for it.

## Smoke (dry-run) verification of the orchestrator process

Before flipping the unit on, run the orchestrator interactively to
make sure the credentials and Jules connectivity work:

```bash
sudo -u julia -E JULIA_JULES_API_KEY=... JULIA_GITHUB_TOKEN=... \
  /opt/julia/.venv/bin/julia run --dry-run
```

(The `-E` flag preserves the env vars across `su`. A venv is
optional — `pip install -e .` outside a venv is fine if you don't
need version isolation.)

## What the unit does

- Runs `/usr/bin/env julia run` (the console script defined in
  `pyproject.toml [project.scripts]`).
- Sources `/etc/julia/julia.env` for the `JULIA_*` family.
- Restarts on crash with a 10s backoff.
- Caps memory at 512M; the orchestrator is a polling loop, not a
  workload.
- Logs to `journald` (`journalctl -u julia.service`).

## What the unit does NOT do

- **No `RestartPreventExitStatus=`** — the orchestrator should
  bring itself down via `/panic` (slash command from the gateway).
  After panic, the watchdog (`src/julia/watchdog.py:_ping_external`)
  stops pinging the heartbeat service, the external service
  notifies you, and you investigate. The OS supervisor would
  happily restart it; if you want it not to, set
  `Environment=JULIA_BLOCK_RESTART=1` in the `[Service]` section
  and have the orchestrator call `systemctl stop julia.service`
  on panic. Not done today; do it if you actually need it.

## See also

- `src/julia/watchdog.py` — Tier 2 + Tier 3
- `RUNBOOK.md` — operator runbook for *all* hosts
- `../launchd/` (TBD) — Mac Mini LaunchAgent, parallel to this unit
