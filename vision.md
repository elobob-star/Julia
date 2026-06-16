# AI Developer Orchestrator — Vision Specification
**v3.0 — 2026-06-11**

> **TL;DR:** Build an always-on orchestrator that sits on top of Jules
> and GitHub and acts as a complete autonomous developer: it prompts
> Jules, approves plans, answers clarifications, runs parallel
> sessions, reviews and merges PRs, recovers stalls, and reports to me
> through a remote gateway. It improves itself the same way it improves
> my code: by opening reviewable PRs against its own behavior. I give
> direction; it executes end to end. You (Claude Fable 5) are the
> engineer with full creative license — this spec is the floor, not the
> ceiling.

<aside>
🧭

**What this document is.** A high-level vision spec capturing *what I
want* and *the real tools I have* — not the technical architecture.

**Who it's for.** A brief for **Claude Fable 5**, the *engineer* of
this project. Fable 5 designs and implements the system; it is **not**
the runtime model (that's the orchestrator — see §10). Wherever
mechanisms are unspecified, that is intentional: Fable 5 has full
creative license.

**Verify before building.** Jules evolves quickly. Before architecting
around any Jules behavior described here, verify it against current
Jules API documentation and changelogs. Treat §7 as a starting dossier,
not ground truth — and design the system so corrections to that dossier
are cheap (see §8).

</aside>

## 1. Vision

A single intelligent system — an **orchestrator** — that acts as an
entire autonomous developer on my behalf. It sits *on top of* Jules
(Google's async coding agent) and GitHub and runs everything:
prompting, accepting plans, parallelizing work, delegating to
sub-agents, answering Jules' clarification requests, reviewing pull
requests, merging or sending changes back, and detecting and restarting
anything that stalls.

The goal: message the system from anywhere — like asking a genie — and
have it behave like a capable professional engineer and personal AI
agent that figures out *how* to get things done. I drive direction; it
handles execution end to end.

> I'm not a developer, and I want the best possible result, so the
> implementing model should interpret intent and build something
> smarter and more elegant than what I've described.

## 2. A day in the life (the north star)

At 9:00 I message it from my phone: *"Add dark mode to the settings
page."* It decomposes the goal, checks remaining Jules quota, and spins
up two parallel sessions — theme system and settings UI. At 9:40 Jules
asks a clarifying question; the orchestrator answers from project
memory without involving me. At 11:00 both PRs pass tests via the
Render MCP; the orchestrator reviews the diffs, merges, and sends me
**one** notification: *"Dark mode shipped — here's the summary."*
Overnight, a third session stalled; the orchestrator noticed at 3 a.m.,
abandoned it, re-prompted with a different approach, and never woke me.
At 6 a.m. its daily canary task detected that Jules started phrasing
plan proposals differently after an update; it adjusted its parser,
logged the drift, and opened a PR against its own playbook documenting
the change. My morning digest mentions all of it in five lines.

## 3. Guiding principles

- **Direction, not boundaries.** General direction, not fixed scope.
  Improve, restructure, and add quality-of-life features freely.
- **Genie-grade autonomy.** Autonomous by default; asks me only when it
  genuinely needs to.
- **Intuitive harness over raw intelligence.** So well-designed that a
  smaller model runs it reliably without frontier-model "work ethic."
- **Everything observable, everything configurable, everything
  explainable.** I can see it, reconfigure it from afar, and ask *why*
  it did anything.
- **Adaptive unless pinned.** Behaviors default to smart and adaptive;
  any can be pinned to "always" / "never."
- **Build on Jules, don't duplicate it.** Wherever Jules has a native
  mechanism (repo memory, `AGENTS.md`, environment snapshots, per-repo
  secrets), integrate with and drive it — never shadow it.
- **Behavior as code.** The orchestrator's own prompts, policies, and
  playbook live in version control and change through reviewable PRs
  (§8). Nothing about its behavior is opaque or unrevertible.
- **Fail loudly upward, degrade gracefully downward.** Problems it can
  handle, it handles silently with a log. Problems it can't, it
  escalates clearly — never silent failure, never panic spam.
- **Portable by design** (§16). **Reuse over reinvention** — full
  permission to fork and repurpose existing open-source work.

## 4. The tools I have

| Component | What I have / want to use |
| --- | --- |
| Primary dev tool | **Jules** via the **Jules API** (plus SDK / CLI as useful) |
| Jules capacity | ~**100 tasks/day**, rolling 24h window, multiple **concurrent** sessions |
| Jules model | Gemini 3.1 Pro — orchestrate around it, don't assume control over it |
| Version control | **GitHub API** — PRs, merges, comments, labels, issues, full repo management (note: GitHub has its own rate limits; budget for them too) |
| Engineer (build-time) | **Claude Fable 5**, run in Claude Code via the Databricks API (that billing covers only Fable 5's own work) |
| Orchestrator (run-time) | A lighter model (e.g. **Nemotron 3 Ultra**) on a **free or low-cost provider**, behind a **BYOK** abstraction — any model/provider swappable without code changes |
| Credentials I provide | **Jules API key**, **GitHub access token**, **orchestrator model API key** — design the secrets workspace (§15) around receiving these at setup |
| Jules MCP servers | **Stitch**, **Context7**, **Render** (the available set is fixed from my side) |
| Host hardware | **Mac Mini**, 24/7, with required repackaging for **Fedora** (§16) |

## 5. Core capabilities

### 5.1 Orchestrating Jules
- Author prompts; accept, reject, or refine proposed **plans**.
- Answer **clarification requests** automatically from memory (§8).
- Run **many sessions in parallel**; coordinate **sub-agent delegation**.
- Detect stalls, failures, and derailments; recover by restarting,
  re-prompting, or abandoning for a different approach.
- Choose the right **Jules mode / configuration** per task.

### 5.2 GitHub management
- Own the full GitHub side: review PRs, **merge** good ones, or
  **comment back to Jules** requesting changes.
- Manage issues, labels, branches, and the review loop across all
  active sub-agents.
- **Idempotent by construction:** a retried operation never
  double-merges, double-comments, or duplicates a task. Exactly-once
  semantics for anything irreversible.

### 5.3 Resilience & uptime — the watchdog hierarchy
Supervision must be **tiered**, because every watcher can die:
1. **OS supervisor** (launchd / systemd) restarts the orchestrator
   process on crash and on boot.
2. **Internal watchdog** monitors sessions, sub-tasks, and the event
   loop; revives or replaces stuck components.
3. **External dead-man's switch:** the system pings an external
   heartbeat service on a schedule; if pings stop, *I* get notified
   from outside the host — because a dead machine cannot report its own
   death.
- Gracefully respects rolling Jules limits *and* GitHub/API limits so
  throughput stays high without hitting walls.

### 5.4 The custom agent harness
- A **heavily customized harness** — built on existing foundations
  where sensible, tailored deeply.
- Makes the "right next move" **intuitive for the model** in any Jules
  scenario, so a lighter model drives it reliably.
- Encodes *how Jules actually behaves* (§7) so the model never
  rediscovers it.
- **Tuned to the orchestrator model's style** (authoritative section
  for this): lighter models follow explicit instructions well but
  rarely ask for clarification. So — **explicit acceptance criteria**,
  **fixed expected output formats**, **proactively injected context**;
  direct, structured prompting over open-ended "figure it out." If a
  different runtime model is swapped in, the prompting layer adapts.
- **Prompts are engineering artifacts:** versioned, tested (prompt
  regression suite against recorded scenarios), and changed via PR
  (§8) — never edited invisibly in place.

### 5.5 The autonomy ladder
Not a binary switch. The system operates at one of several rungs, per
repo and globally, moving down automatically when confidence drops and
back up when stability returns:
1. **Full auto** — plan, execute, merge, report in digest.
2. **Auto + notify** — merges autonomously but pings me per merge.
3. **Supervised** — does everything except merge; queues approvals.
4. **Propose-only** — plans and estimates, executes nothing.
5. **Paused / safe mode** — heartbeat and gateway only; triggered by
   the panic-stop, repeated anomalies, or credential problems.
Every automatic rung change is logged with its reason and reversible
from the gateway.

### 5.6 Quality-of-life features
- **Dry-run mode:** rehearse a task end-to-end — decomposition,
  session plan, predicted quota cost — without spending anything. Also
  the safe way to test harness changes.
- **Daily digest:** one morning message — shipped, in-flight, blocked,
  anomalies, quota and spend posture.
- **"Explain yourself":** ask about any past action from the gateway
  and get the decision trace behind it (§13).
- **Repo onboarding wizard:** point it at a new repo and it audits the
  codebase, drafts `AGENTS.md`, configures the Jules environment
  snapshot, proposes quality gates and a risk tier, and asks me only
  the questions it can't answer itself.
- **Task templates / skills:** reusable recipes for recurring work
  (dependency bumps, bug triage, refactors, release notes) that encode
  the best-known prompt pattern for that job — and improve over time
  via §8.
- **Priority lanes with preemption:** an urgent request from me jumps
  the queue; background maintenance yields quota gracefully.

## 6. Self-verification & drift detection

The system must notice when *its own ground truth* shifts:
- **Daily canary task:** one small, known-good Jules task (budgeted
  from the 100/day) whose expected shape is well understood. Deviations
  in Jules' responses, timing, or formats are detected here first —
  before they break real work.
- **Synthetic health checks** on every integration (GitHub, model
  provider, MCPs) on a schedule, with results feeding the autonomy
  ladder (§5.5).
- **Chaos hooks (build-time):** Fable 5 should make key failure modes
  injectable (provider outage, malformed Jules response, mid-merge
  crash) so recovery paths are *tested*, not hoped for.

## 7. Jules mastery — the behavioral dossier

The system must deeply "know" Jules. **Fable 5: research Jules
thoroughly at build time** and encode results as a living knowledge
base. Starting points known today — verify all:

- **Session lifecycle:** prompt → plan proposal → approval → execution
  in a cloud VM → diff/PR output, with **activities** exposed via API
  for monitoring and mid-session interaction.
- **`AGENTS.md`:** repo-root standing instructions — the orchestrator's
  primary channel for pushing durable repo-specific guidance into Jules.
- **Native repo memory:** Jules learns per-repo corrections across
  sessions. *Leverage and feed it* (issue corrections deliberately);
  never shadow it.
- **Environment setup scripts & snapshots:** managed per repo so
  sessions start fast and tests run inside Jules' VM.
- **Per-repo secrets:** prefer Jules' storage for anything Jules-side.
- **Modes, full API surface, CLI**, and the MCPs (Stitch for UI,
  Context7 for docs, Render for deploy/test) — know when each is right.
- **Failure patterns:** when to course-correct in-session vs. abandon
  and re-approach. Catalogue quirks over time via §6 and §8.

## 8. Memory & recursive self-improvement

Two memory layers with a hard rule — **Jules owns repo-level memory;
the orchestrator owns everything above it** — and one unifying
mechanism for improvement.

**Jules layer (managed, not replaced):** the orchestrator actively
maintains each repo's `AGENTS.md`, environment snapshot, and (via
deliberate corrections) Jules' native memory. Repo-specific knowledge
lives *inside Jules' own systems*.

**Orchestrator layer (cross-repo):**
- **Task ledger** — everything in flight and completed, durable across
  restarts (§14).
- **Jules behavioral playbook** — learned quirks, working prompt
  patterns, failure signatures, recovery tactics (the living §7).
- **User profile** — my preferences: review strictness, notification
  taste, style, standing rules.
- **Project knowledge** — cross-repo context that fits no single
  `AGENTS.md`.

**The improvement mechanism — behavior-as-PRs.** The orchestrator's
prompts, policies, playbook, templates, and configuration live in a
**git repository of their own**. Self-improvement means the
orchestrator **opens pull requests against its own behavior repo**,
flowing through the same review pipeline it uses for my code:
- **Low-stakes changes** (playbook entries, prompt wording, template
  tweaks) auto-merge after passing the prompt regression suite (§5.4).
- **Behavioral changes** (policies, autonomy rules, gate criteria)
  require my approval — a one-tap review from the gateway with a plain-
  language summary of what changes and why.
- Every change is **diffable, auditable, and revertible**. A bad
  self-improvement is a `git revert`, not an archaeology project.
- A **weekly retrospective** (from §13 analytics) proposes the next
  batch of improvements, with evidence.

This is supervised recursion by design: the system improves itself
through reviewed, reversible changes — never by silently rewriting its
own behavior.

## 9. Local execution & verification

- A sandboxed workspace on the host to clone branches, build, run test
  suites, and smoke-test changes when Jules' VM or Render MCP isn't
  sufficient.
- Results feed the quality gates (§18) — automated verification is a
  **core gate for autonomous merges**.
- Cleanly isolated (own directories, own credential scope) so a
  misbehaving run can't touch the rest of the machine. Mechanism is
  Fable 5's choice, with §16 portability in mind.

## 10. Models: engineer vs. orchestrator

- **Engineer (build-time): Claude Fable 5** — designs and writes the
  system.
- **Orchestrator (run-time): a lighter model** (e.g. Nemotron 3 Ultra)
  on a **free or low-cost provider** by default; the harness (§5.4)
  carries it.
- **BYOK everywhere**; **fallback models** required so a provider limit
  or outage degrades to a lower autonomy rung (§5.5) instead of
  stopping the system.
- **Rate limiting as a toggleable feature:** with a free default
  provider, hard budget enforcement is secondary — but include a
  per-provider rate-limit / spend-cap module I can switch on for paid
  keys. Track usage either way for visibility.

## 11. Exploratory integrations

Optional, never on the critical path, behind clean adapters whose
absence the system shrugs off:
- **NotebookLM via `notebooklm-py`** — project management, memory
  augmentation, asset development, audio/video overviews, deep
  research. Caveat: unofficial, automation-based, can break whenever
  Google changes the product — design accordingly.
- **Anything else Fable 5 finds valuable** — same rule.

## 12. Control interface (the gateway)

A purpose-built **app / dashboard** as my remote control — conceptually
like the Codex or Claude mobile experiences.

- Primarily a **messaging gateway**; *the intelligence lives on the
  host, not the app.*
- Converse, submit tasks, switch sessions/projects, review work,
  approve behavior-PRs (§8), and adjust the autonomy ladder — from my
  phone.
- **All-seeing:** sessions, GitHub status, logs, metrics, analytics,
  decision traces.
- **Remote reconfiguration** without lockout risk: configuration
  changes are validated before apply, and a known-good fallback config
  always exists.
- **UX quality bar:** mobile-first; one glance answers "is everything
  okay and what's in flight?"; common actions (approve, pause, reply)
  in ≤2 taps; progressive disclosure for depth. Functional and clean
  beats beautiful and late — but "functional" includes *pleasant*.
- A thin layer on an existing transport (even Telegram) is fine if it
  gives the cleanest experience.
- **Security is first-class:** the gateway controls a machine holding
  credentials and merge rights. Strong authentication, no
  unauthenticated control endpoints exposed to the internet, session
  revocation, and a lost phone cannot compromise the host. Proven
  patterns; Fable 5 designs the specifics.

## 13. Analytics, observability & explainability

- The system **passively logs everything**; analysis happens
  separately/programmatically — the orchestrator agent is not the
  analyst.
- Goal: find **friction points** and continuously anneal the workflow,
  feeding the §8 improvement loop with evidence.
- Fable 5 designs the full catalogue: data points, importance,
  meaning, collection method, plus metrics, charts, reports, alerts.
- **Decision & audit logging:** every consequential action records
  *why* — inputs considered, options weighed, rule or judgment applied.
  This powers "explain yourself" (§5.6) and makes behavior debuggable.
- **Retention policy:** decision traces and the task ledger are kept
  long-term; bulky raw logs roll off on a sensible schedule.

## 14. Durable state, backup & recovery

- Persist request ↔ Jules session ↔ GitHub PR mappings; resume
  in-flight work cleanly after restart — a reboot never orphans work.
- **State is backed up** (simple, boring, automatic) and **restorable
  with one documented command** — including onto a fresh machine.
- **Portability proof:** restoring state onto the Fedora target is an
  actual tested procedure (§16), not an aspiration.

## 15. Secrets & credentials

- A **locally managed, orchestrator-manageable workspace** for the keys
  I provide and anything added later.
- **Prefer Jules' per-repo secret storage** for anything Jules-side.
- **Least privilege everywhere:** components get only what they need;
  the gateway triggers actions but never reads raw keys; logs and
  analytics never contain secret values.
- Robust and boring by design — proven patterns over clever ones.

## 16. Deployment, runtime & portability

- Runs **24/7** as a managed background service. **Primary host:** Mac
  Mini via `launchd`; **required repackage target:** Fedora via
  `systemd`, with equivalent always-on + auto-restart behavior.
- OS-specific pieces abstracted behind a thin compatibility layer: a
  new OS is a repackage, not a rewrite. No macOS-only dependencies in
  the core.
- Auto-recovery on both platforms; fully phone-controllable regardless
  of host.

## 17. Engineering standards (the bar for the system itself)

The orchestrator must be built to **at least the standard it enforces
on my code**:
- Its own repo has CI, a real test suite (including the chaos hooks of
  §6 and the prompt regression suite of §5.4), linting, and typed code
  where the stack supports it.
- **Documentation ships with it:** an architecture overview, a
  plain-language operator runbook (how to install, restore, panic-stop,
  migrate hosts — written for a non-developer: me), and lightweight
  decision records for major design choices so future Fable 5 sessions
  can pick up context fast.
- **Eventual dogfooding:** once stable, the orchestrator's own repo
  becomes a repo it manages — with the behavior-PR gates of §8 and a
  conservative autonomy rung, so the system maintains itself under the
  same discipline as everything else.

## 18. Open design challenges (delegated to Claude Fable 5)

Deliberately under-specified — design robust, imaginative, elegant
solutions:
- **Task model & lifecycle** — what a "task" is, decomposition into
  Jules sessions, prioritization, queueing, and what "done" means.
- **Quality gates before merge** — tests, build, lint; automated
  verification (Render MCP and/or §9) as a core gate, with gate
  strictness scaled by repo risk tier (§20).
- **Scheduling the scarce resource** — allocating ~100 tasks/day and
  concurrent sessions across priority lanes (§5.6), with backoff,
  queueing, and the canary budget (§6).
- **Sub-agent conflict resolution** — autonomous unless it genuinely
  warrants me.
- **Parallel-work coordination** — overlapping edits, real merge
  conflicts, duplicate-task prevention, moving base branches
  (auto-rebase).
- **Notification routing** — "only alert me when it matters."
- **Collaboration / feedback loops** — seamless movement along the
  autonomy ladder mid-task.
- **Production deployment** — autonomous by default for a low-stakes
  hobby setup; fully configurable and adaptive.
- **Multi-tool connectivity** — a clean integration hub for plugging in
  services (APIs, messaging, storage), designed entirely by Fable 5.
- **Safety boundaries & emergency stop** — what is *never* done
  autonomously (protected branches, history rewrites, secret rotation,
  new spend, destructive ops), plus a single panic-stop reachable from
  the gateway that drops the system to safe mode (§5.5).

…and anything else you can think of. Entirely up to you.

## 19. Build phases (suggested order, not binding)

Each phase ends with a working, usable system. A running Phase 1 this
month beats a half-finished Phase 4.

- **Phase 0 — Walking skeleton.** One message in → one Jules session →
  one PR → one notification back. Prove the spine.
- **Phase 1 — Core loop.** Parallel sessions, plan handling,
  clarification answering, PR review + quality gates, watchdog tiers,
  durable state + backup, quota handling, secrets workspace, autonomy
  ladder (basic rungs).
- **Phase 2 — Harness intelligence.** Analytics + explainability,
  two-layer memory, behavioral playbook, behavior-as-PRs improvement
  loop, notification routing, local verification, canary + drift
  detection, daily digest.
- **Phase 3 — Polish & ecosystem.** Gateway UX polish, repo onboarding
  wizard, task templates, dry-run mode, NotebookLM and other optional
  adapters, multi-tool hub, dogfooding.

## 20. Non-goals / explicit anti-scope

- Not prescribing architecture, data model, or tech stack.
- **Not** multi-user, **not** a SaaS product — this serves one person.
- **No** Windows support. **No** model training or fine-tuning —
  adaptation happens at the prompt/harness/playbook level only.
- **No** pixel-perfect UI in v1.
- **No** parallel reimplementation of anything Jules does natively —
  integrate, don't duplicate.
- Named tools (Jules API, GitHub API, Mac Mini, Fedora) are the real
  baseline; everything else is open to a better idea.

## 21. Open inputs from me (ask early, don't guess)

- **First target repo(s):** *(I'll specify at kickoff.)*
- **Notification channel:** *(push / Telegram / other — I'll confirm.)*
- **Per-repo risk tiers:** which repos get which default autonomy rung.
- **Credentials:** Jules API key, GitHub token, orchestrator model key
  — provided at setup.
- **External heartbeat service** (§5.3): I'll pick or approve one.

<aside>
✨

**Standing invitation to the architect:** I don't always know exactly
what I want. If you (Claude Fable 5) see a more elegant, smarter, or
more capable design at any point — including features or structures I
never mentioned — build that instead. This spec is the floor, not the
ceiling.

**This document grew out of a rambling session with Gemini Live,
refined over several iterations. Nothing is set in stone. If the
wording ever sounds like an order, it isn't — it's a direction and a
dream, and you can almost certainly design it better than I imagined.**

</aside>