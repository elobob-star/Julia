'''The core loop (vision sections 2 and 5).

Gateway message -> task -> Jules session (plan review, clarification
answering, execution) -> pull request -> quality gates -> merge or
approval queue -> notification. Every consequential step records a
decision trace, so /explain can answer why anything happened.
'''

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .autonomy import AutonomyLadder, Rung
from .config import Settings
from .gateway.base import Gateway, Incoming
from .gh.client import GitHubAPI
from .jules import dossier
from .jules.client import JulesAPI
from .llm.provider import ChatModel
from .models import Task, TaskState, new_id
from .quota import QuotaGuard
from .state import Store
from .watchdog import Watchdog

log = logging.getLogger('julia')

HELP = (
    'Commands: /status, /digest, /approve <task-id>, /explain <task-id>, '
    '/playbook [task-id], /improve <file>:<category> <new-content>, '
    '/rung <0-4> (0 safe, 1 propose-only, 2 supervised, 3 auto+notify, '
    '4 full auto), /panic, /help. Anything else becomes a development task.'
)

# Behaviour editor wiring (vision section 8). Optional at construction so
# the Phase 1 spine continues to work without a behaviours checkout.
from .behavior.editor import BehaviorEditor, PlaybookEntry  # noqa: E402  -- grouped near collaborator imports


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        jules: JulesAPI,
        github: GitHubAPI,
        model: ChatModel,
        gateway: Gateway,
        behavior: 'BehaviorEditor | None' = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.jules = jules
        self.github = github
        self.model = model
        self.gateway = gateway
        self.behavior = behavior
        self.ladder = AutonomyLadder(store)
        self.quota = QuotaGuard(store, settings.jules_daily_quota, settings.jules_canary_budget)
        self.watchdog = Watchdog(settings, gateway)
        self._runners: dict[str, asyncio.Task[None]] = {}

    # Lifecycle ----------------------------------------------------------
    async def run(self) -> None:
        '''Main entry: resume in-flight work, then serve the gateway forever.'''
        self.store.record_decision('orchestrator', 'started', 'process start or restart')
        await self._resume()
        async with asyncio.TaskGroup() as group:
            group.create_task(self.watchdog.run())
            group.create_task(self._daily_loop())
            group.create_task(self._serve())

    async def _resume(self) -> None:
        '''A reboot never orphans work (vision section 14).'''
        for task in self.store.list_tasks(
            TaskState.PLANNING, TaskState.EXECUTING, TaskState.REVIEWING
        ):
            self.store.record_decision(
                'orchestrator', 'resumed', 'picked up in-flight task after restart', task.id
            )
            self._spawn(task)

    async def _serve(self) -> None:
        async for message in self.gateway.incoming():
            self.watchdog.beat('gateway')
            try:
                await self.handle_message(message)
            except Exception:
                log.exception('failed handling gateway message')

    async def _daily_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.digest_interval_s)
            await self.run_canary()
            await self.gateway.send(self.digest())

    # Intake ---------------------------------------------------------------
    async def handle_message(self, message: Incoming) -> None:
        text = message.text.strip()
        if not text:
            return
        if text.startswith('/'):
            await self._command(text)
            return
        if not self.ladder.allows_execution():
            rung = self.ladder.current()
            self.store.record_decision(
                'orchestrator', 'task_refused', f'rung {rung.name} does not allow execution'
            )
            await self.gateway.send(
                f'Holding that: autonomy rung is {rung.name}. Raise it with /rung 2 or higher.'
            )
            return
        repo = self.settings.default_repo or (
            'example/sandbox' if self.settings.dry_run else None
        )
        if repo is None:
            await self.gateway.send('No default repository configured (set JULIA_DEFAULT_REPO).')
            return
        task = Task(id=new_id(), prompt=text, repo=repo)
        if not self.quota.try_acquire(f'task:{task.id}'):
            self.store.record_decision('orchestrator', 'task_refused', 'quota exhausted', task.id)
            await self.gateway.send(
                'Jules quota is exhausted for the rolling 24h window; nothing was started.'
            )
            return
        self.store.save_task(task)
        self.store.record_decision(
            'orchestrator', 'task_created', f'owner request: {text[:120]}', task.id
        )
        await self.gateway.send(f'Task {task.id} started on {repo}.')
        self._spawn(task)

    def _spawn(self, task: Task) -> None:
        runner = asyncio.create_task(self._run_task(task))
        self._runners[task.id] = runner
        # Drop the reference once the runner settles so the registry never
        # holds finished tasks. Using a done-callback (rather than the
        # runner's own finally block) means a caller can snapshot and await
        # the live runners without racing the cleanup.
        def _discard(_: asyncio.Task[None], tid: str = task.id) -> None:
            self._runners.pop(tid, None)

        runner.add_done_callback(_discard)

    async def await_runners(self) -> None:
        '''Await all in-flight task runners (used by tests and shutdown).

        Snapshots the registry first so concurrent completion callbacks
        mutating ``_runners`` cannot change the set being awaited.
        '''
        while runners := list(self._runners.values()):
            await asyncio.gather(*runners, return_exceptions=True)

    # Session driving --------------------------------------------------------
    async def _record(self, kind: str, task: Task, gist: str) -> None:
        """Append a playbook entry when a behaviour editor is wired in.

        Backwards compat: ``self.behavior is None`` short-circuits the
        whole call. The test suite, dry-run mode, and any deployment
        without ``--behaviors PATH`` continue to work without changes.
        """
        if self.behavior is None:
            return
        entry = PlaybookEntry(
            kind=kind,
            repo=task.repo,
            task_id=task.id,
            gist=gist,
        )
        await self.behavior.record_playbook_entry(entry)

    async def _run_task(self, task: Task) -> None:
        try:
            await self._drive_session(task)
        except Exception as exc:
            log.exception('task %s failed', task.id)
            task.state = TaskState.FAILED
            task.error = str(exc)
            self.store.save_task(task)
            self.store.record_decision('orchestrator', 'task_failed', str(exc), task.id)
            self.ladder.record_anomaly(f'task {task.id} failed: {exc}', task.repo)
            await self.gateway.send(f'Task {task.id} failed: {exc}')
        finally:
            self.watchdog.clear(f'task:{task.id}')
            # Registry cleanup is handled by the done-callback in _spawn.

    async def _drive_session(self, task: Task) -> None:
        if task.session_id is None:
            task.session_id = await self.jules.create_session(task.prompt, task.repo)
            task.state = TaskState.PLANNING
            self.store.save_task(task)
            self.store.record_decision(
                'orchestrator', 'session_created', f'Jules session {task.session_id}', task.id
            )
        session_id = task.session_id
        handled: set[str] = set()
        last_progress = time.monotonic()
        while True:
            self.watchdog.beat(f'task:{task.id}')
            activities = await self.jules.list_activities(session_id)
            for activity in activities:
                key = dossier.activity_key(activity)
                if key in handled:
                    continue
                handled.add(key)
                last_progress = time.monotonic()
                kind = dossier.classify_activity(activity)
                if kind == 'failed':
                    raise RuntimeError(f'Jules reported failure: {activity}')
                if kind == 'completed':
                    await self._on_completed(task, activity)
                    return
                if kind == 'plan':
                    await self._on_plan(task, activity)
                elif kind == 'question':
                    await self._on_question(task, activity)
            if time.monotonic() - last_progress > self.settings.stall_timeout_s:
                raise RuntimeError('session stalled: no new activity within the stall window')
            # max(..., 0) keeps a real (possibly zero) delay, while the
            # explicit sleep always yields control back to the event loop
            # so co-running runners and awaiters make progress even when
            # the poll interval is configured to zero.
            await asyncio.sleep(max(self.settings.poll_interval_s, 0))

    async def _on_plan(self, task: Task, activity: dict[str, Any]) -> None:
        plan = dossier.extract_plan_text(activity)
        verdict = await self.model.complete(
            dossier.PLAN_REVIEW_SYSTEM_PROMPT,
            f'Task: {task.prompt}\nProposed plan: {plan}',
        )
        if verdict.strip().upper().startswith('REVISE'):
            note = verdict.split(':', 1)[-1].strip() or 'please propose a smaller, safer plan'
            await self.jules.send_message(task.session_id or '', f'Please revise the plan: {note}')
            self.store.record_decision(
                'orchestrator',
                'plan_revision_requested',
                f'{note} | plan: {plan[:160]}',
                task.id,
                meta={'kind': 'plan', 'verdict': 'REVISE', 'note': note[:160]},
            )
            await self._record('plan', task, gist=f'Plan revised: {note}')
            return
        await self.jules.approve_plan(task.session_id or '')
        task.state = TaskState.EXECUTING
        self.store.save_task(task)
        self.store.record_decision(
            'orchestrator',
            'plan_approved',
            f'model verdict {verdict[:40]}; plan: {plan[:160]}',
            task.id,
            meta={'kind': 'plan', 'verdict': 'APPROVE', 'plan_chars': len(plan)},
        )
        await self._record('plan', task, gist='Plan approved on first pass.')

    async def _on_question(self, task: Task, activity: dict[str, Any]) -> None:
        question = str(activity.get('question') or activity.get('text') or '')
        answer = await self.model.complete(
            dossier.CLARIFICATION_SYSTEM_PROMPT,
            f'Repository: {task.repo}\nTask: {task.prompt}\nJules asks: {question}',
        )
        await self.jules.send_message(task.session_id or '', answer)
        self.store.record_decision(
            'orchestrator',
            'clarification_answered',
            f'q: {question[:120]} | a: {answer[:120]}',
            task.id,
            meta={'kind': 'question', 'q_chars': len(question), 'a_chars': len(answer)},
        )
        await self._record(
            'question', task, gist=f'Clarification Q answered (q={len(question)} chars).'
        )

    async def _on_completed(self, task: Task, activity: dict[str, Any]) -> None:
        pr_url = dossier.extract_pr_url(activity)
        if not pr_url:
            # Live wire does not always carry a pullRequestUrl on
            # sessionCompleted (verified 2026-06-16 -- a single-line
            # CANARY.md task completed successfully but never reached
            # the PR stage). We do not auto-apply patches because
            # vision section 18 lists "history rewrites / destructive
            # ops / new spend" as never-automated; opening a PR
            # from a patch on someone else's behalf bridges into
            # that territory. The right move is to surface the patch
            # to the owner -- file as QUEUED_NEEDS_REVIEW (a new
            # state) so they can decide.
            git_patch = dossier.extract_git_patch(activity)
            if git_patch:
                self.store.record_decision(
                    'orchestrator',
                    'patch_unapplied',
                    f'jules completed without opening a PR; patch bytes={len(git_patch)}; awaiting owner decision',
                    task.id,
                    meta={'kind': 'completion', 'patch_bytes': len(git_patch)},
                )
                task.state = TaskState.AWAITING_APPROVAL
                task.error = 'jules did not open a PR; owner to apply patch or rerun'
                task.pr_url = ''  # explicit
                self.store.save_task(task)
                await self.gateway.send(
                    f'Task {task.id} completed but Jules did not open a PR. '
                    f'Patch captured ({len(git_patch)} bytes). Decision recorded; '
                    f'apply manually or rerun.'
                )
                await self._record('info', task, gist='Jules returned a patch, not a PR. Awaiting owner.')
                return
        if not pr_url:
            raise RuntimeError('session completed without producing a pull request')
        task.pr_url = pr_url
        task.state = TaskState.REVIEWING
        self.store.save_task(task)
        if not await self.github.pr_checks_passed(pr_url):
            await self.github.comment(
                pr_url, 'Julia: automated quality gates failed; please fix the failing checks.'
            )
            self.store.record_decision(
                'orchestrator',
                'gates_failed',
                pr_url,
                task.id,
                meta={'kind': 'completion', 'pr_url': pr_url, 'gates': 'failed'},
            )
            task.state = TaskState.FAILED
            task.error = 'quality gates failed'
            self.store.save_task(task)
            self.ladder.record_anomaly('quality gates failed', task.repo)
            await self.gateway.send(f'Task {task.id}: quality gates failed on {pr_url}.')
            await self._record('failure', task, gist=f'Quality gates failed on {pr_url}.')
            return
        if self.ladder.allows_merge(task.repo):
            self.store.record_decision(
                'orchestrator',
                'merged',
                'gates passed and autonomy rung permits merging',
                task.id,
                meta={'kind': 'completion', 'pr_url': pr_url, 'rung': int(self.ladder.current(task.repo))},
            )
            await self.github.merge_pr(pr_url)
            task.state = TaskState.MERGED
            self.store.save_task(task)
            await self.gateway.send(f'Shipped: task {task.id} merged ({pr_url}).')
            await self._record('completion', task, gist=f'Merged {pr_url}.')
        else:
            task.state = TaskState.AWAITING_APPROVAL
            self.store.save_task(task)
            self.store.record_decision(
                'orchestrator',
                'queued_for_approval',
                'autonomy rung requires human approval to merge',
                task.id,
                meta={'kind': 'completion', 'pr_url': pr_url, 'rung': int(self.ladder.current(task.repo))},
            )
            await self.gateway.send(
                f'Task {task.id} ready for review: {pr_url} - approve with /approve {task.id}'
            )
            await self._record('completion', task, gist=f'Queued for approval: {pr_url}.')

    # Gateway commands ----------------------------------------------------
    async def _command(self, text: str) -> None:
        parts = text.split()
        command, args = parts[0].lower(), parts[1:]
        if command == '/help':
            await self.gateway.send(HELP)
        elif command == '/status':
            await self.gateway.send(self._status_text())
        elif command == '/digest':
            await self.gateway.send(self.digest())
        elif command == '/panic':
            self.ladder.panic()
            await self.gateway.send(
                'Panic-stop engaged: SAFE_MODE. Nothing executes or merges until you /rung up.'
            )
        elif command == '/rung' and args:
            try:
                rung = Rung(int(args[0]))
            except ValueError:
                await self.gateway.send('Rung must be a number from 0 to 4.')
                return
            self.ladder.set_rung(rung, 'set from gateway')
            await self.gateway.send(f'Autonomy rung set to {rung.name}.')
        elif command == '/approve' and args:
            await self._approve(args[0])
        elif command == '/explain' and args:
            decisions = self.store.decisions_for(args[0])
            lines = [
                f'{at} {actor}/{action}: {reason}' for at, actor, action, reason, _meta in decisions
            ]
            await self.gateway.send('\n'.join(lines) or 'No decisions recorded for that task.')
        elif command == '/improve' and args:
            await self._improve(args)
        elif command == '/playbook':
            await self.gateway.send(await self._playbook_summary(args[0] if args else None))
        else:
            await self.gateway.send(f'Unknown command. {HELP}')

    async def _improve(self, args: list[str]) -> None:
        '''``/improve <file>:<low-stakes|behavioural> <new-content>``.

        Routes a behaviour change through ``BehaviorEditor``. ``None``
        editor returns a courteous refusal; locked paths return a
        hard denial (vision section 18). Every ``/improve`` creates a
        ``Task`` of kind ``behavior_pr`` so ``/explain``, the daily
        digest, and a future ``/approve-behavior`` can reference the
        PR by stable task id.
        '''
        if self.behavior is None:
            await self.gateway.send(
                "No behavior wiring configured; run with --behaviors PATH. (Phase 1 mode.)"
            )
            return
        if not args or ':' not in args[0]:
            await self.gateway.send(
                'Usage: /improve <file>:<low-stakes|behavioural> <new-content>'
            )
            return
        target_file, _, category_label = args[0].partition(':')
        # If the owner sent no inline content, leave the file's current
        # text unchanged (this is a touch-up, not a wholesale rewrite).
        # The editor will still record the rationale and category.
        new_content = ' '.join(args[1:]).strip() if len(args) > 1 else ''
        # Step 4: persist a Task up front so the decision trace and
        # ``/explain`` reference a stable id regardless of how the
        # editor's HTTP call ends. The Task is created with state
        # AWAITING_APPROVAL since both low-stakes and behavioural
        # require either auto-merge (Step 5) or owner approval.
        from .behavior.editor import BehaviorDenied  # local import; categoriser raises this
        task = Task(
            id=new_id(),
            prompt=target_file,
            repo='behaviors',
            kind='behavior_pr',
            state=TaskState.AWAITING_APPROVAL,
        )
        self.store.save_task(task)
        self.store.record_decision(
            'owner', 'improve_requested',
            f'proposed {category_label} change to {target_file}',
            task.id, meta={'kind': category_label, 'file': target_file},
        )
        try:
            if category_label == 'low-stakes':
                editor_token = await self.behavior.propose_low_stakes_change(
                    target_file, new_content,
                    rationale=f'proposed via gateway on {target_file}',
                )
                self.store.record_decision(
                    'orchestrator', 'editor_returned', 'low-stakes PR opened', task.id,
                )
                await self.gateway.send(
                    f'Low-stakes change opened for task {task.id}: {target_file} '
                    f'(PR: {editor_token})'
                )
            elif category_label == 'behavioural':
                editor_token = await self.behavior.propose_behavioral_change(
                    target_file, new_content,
                    rationale=f'proposed via gateway on {target_file}',
                )
                self.store.record_decision(
                    'orchestrator', 'editor_returned',
                    'behavioural PR opened, awaiting manual review', task.id,
                )
                await self.gateway.send(
                    f'Behavioural change opened for task {task.id}: {target_file} '
                    f'- awaiting manual review. (PR: {editor_token})'
                )
            else:
                task.state = TaskState.FAILED
                task.error = f'unknown category {category_label!r}'
                self.store.save_task(task)
                self.store.record_decision(
                    'orchestrator', 'improve_rejected',
                    f'unknown category {category_label!r}', task.id,
                )
                await self.gateway.send(
                    f'Unknown category {category_label!r}; expected low-stakes or behavioural.'
                )
                return
        except BehaviorDenied as exc:
            task.state = TaskState.FAILED
            task.error = str(exc)
            self.store.save_task(task)
            self.store.record_decision(
                'orchestrator', 'improve_refused', str(exc), task.id,
                meta={'refusal_stage': 'category'},
            )
            await self.gateway.send(
                f'/improve refused: {exc} (task {task.id} marked failed).'
            )
            return
        except Exception as exc:
            task.state = TaskState.FAILED
            task.error = repr(exc)
            self.store.save_task(task)
            self.store.record_decision(
                'orchestrator', 'improve_failed', repr(exc), task.id,
            )
            await self.gateway.send(
                f'/improve failed: {exc!r} (task {task.id} marked failed).'
            )
            return
        # Persist the editor's token so ``/approve-behavior`` (Step 5)
        # can find this task by URL, and so the daily digest can
        # surface it. ``editor_token`` is the editor's native return
        # value: html_url on GitHub, SHA on local, fake-prefix on dry.
        task.source_url = editor_token
        self.store.save_task(task)

    async def _playbook_summary(self, task_id: str | None) -> str:
        '''Filter decision traces to those whose meta carries a ``kind``.

        Returns the entries that the orchestrator would have written
        into the behavioural playbook. Without an editor attached, this
        endpoint still surfaces the related decisions — it just has no
        external side effects to roll back.
        '''
        if task_id is not None:
            rows = list(self.store.decisions_for(task_id))
        else:
            rows = []
            for task in self.store.list_tasks():
                rows.extend(self.store.decisions_for(task.id))
        rows = [r for r in rows if r[4] and 'kind' in r[4]]
        if not rows:
            return 'No playbook entries recorded yet (run with --behaviors PATH).'
        return '\n'.join(
            f'{at} {actor}/{action} kind={meta["kind"]} - {reason}'  # type: ignore[index]
            for at, actor, action, reason, meta in rows
        )

    async def _approve(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        if task is None or task.state is not TaskState.AWAITING_APPROVAL or not task.pr_url:
            await self.gateway.send(f'Nothing awaiting approval under id {task_id}.')
            return
        await self.github.merge_pr(task.pr_url)
        task.state = TaskState.MERGED
        self.store.save_task(task)
        self.store.record_decision('owner', 'approved_merge', 'manual approval from gateway', task.id)
        await self.gateway.send(f'Task {task.id} merged: {task.pr_url}')

    # Reporting -------------------------------------------------------------
    def _status_text(self) -> str:
        counts: dict[str, int] = {}
        for task in self.store.list_tasks():
            counts[task.state.value] = counts.get(task.state.value, 0) + 1
        summary = ', '.join(f'{k}: {v}' for k, v in sorted(counts.items())) or 'no tasks yet'
        rung = self.ladder.current()
        remaining = self.quota.remaining()
        return f'Rung {rung.name} | quota left (24h) {remaining} | {summary}'

    def digest(self) -> str:
        '''One-glance morning message (vision section 5.6).'''
        since = datetime.now(timezone.utc) - timedelta(days=1)
        recent = [t for t in self.store.list_tasks() if t.updated_at >= since]
        shipped = [t for t in recent if t.state is TaskState.MERGED]
        blocked = [
            t for t in recent if t.state in (TaskState.FAILED, TaskState.AWAITING_APPROVAL)
        ]
        in_flight = self.store.list_tasks(
            TaskState.PLANNING, TaskState.EXECUTING, TaskState.REVIEWING
        )
        rung = self.ladder.current()
        lines = [
            f'Daily digest - rung {rung.name}, quota left '
            f'{self.quota.remaining()}/{self.settings.jules_daily_quota}.',
            f'Shipped: {len(shipped)}'
            + ('; '.join([''] + [t.prompt[:48] for t in shipped]) if shipped else ''),
            f'In flight: {len(in_flight)}',
            f'Needs attention: {len(blocked)}'
            + (
                '; '.join([''] + [f'{t.id} ({t.state.value})' for t in blocked])
                if blocked
                else ''
            ),
        ]
        return '\n'.join(lines)

    # Canary (vision section 6) ---------------------------------------------
    async def run_canary(self) -> None:
        '''Probe Jules with a tiny known-good task and check response shapes.'''
        repo = self.settings.canary_repo or (
            'example/sandbox' if self.settings.dry_run else None
        )
        if repo is None:
            return
        if not self.quota.try_acquire('canary', canary=True):
            return
        try:
            session_id = await self.jules.create_session(dossier.CANARY_PROMPT, repo)
            await self.jules.approve_plan(session_id)
            activities = await self.jules.list_activities(session_id)
            kinds = {dossier.classify_activity(a) for a in activities}
            if kinds and kinds != {'progress'}:
                self.store.record_decision('canary', 'healthy', f'kinds={sorted(kinds)}')
            else:
                self.ladder.record_anomaly('canary drift: no recognizable activity shapes')
                self.store.record_decision('canary', 'drift_detected', f'kinds={sorted(kinds)}')
                await self.gateway.send(
                    'Canary: Jules response shapes look unfamiliar - possible drift. '
                    'Review playbook/jules-playbook.md and the dossier.'
                )
        except Exception as exc:
            self.ladder.record_anomaly(f'canary failed: {exc}')
            self.store.record_decision('canary', 'failed', str(exc))
