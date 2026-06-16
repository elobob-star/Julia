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
    'Commands: /status, /digest, /approve <task-id>, /approve-behavior <pr-url>, '
    '/explain <task-id>, /playbook [task-id], '
    '/improve <file>:<category> <new-content>, '
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
            group.create_task(self._pr_watcher_loop())
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
            # the PR stage). Two paths:
            #
            #   - NO_PATCH path: surface to the owner that nothing
            #     arrived and let them rerun.
            #   - PATCH path: if a git_patch artifact arrived AND
            #     the autonomy ladder is at SUPERVISED or higher,
            #     publish the patch through ``publish_jules_outputs``
            #     so an actual GitHub PR exists for the rest of the
            #     spine (_poll_behavior_prs / /approve).
            #
            # Below SUPERVISED the patch is a real surface area but
            # not yet a public record; the right posture is to leave
            # the task AWAITING_APPROVAL so the owner can decide
            # whether to publish, edit, or skip. (Vision §18 lists
            # 'history rewrites / destructive ops / new spend' as
            # never-automated; opening a fresh PR is neither, but the
            # rung control keeps that call with the operator until
            # they've opted in.)
            git_patch = dossier.extract_git_patch(activity)
            if git_patch:
                if self.ladder.allows_publish(task.repo):
                    try:
                        base_sha = await self.github.get_default_branch_sha(task.repo)
                        title = f"julia: {task.prompt[:60] or 'patch from Jules'}"
                        body = (
                            f"Opened by Julia orchestrator after Jules returned "
                            f"a {len(git_patch)}-byte patch without a PR URL.\n\n"
                            f"Original prompt: {task.prompt}\n"
                            f"Task id: {task.id}\n"
                        )
                        new_pr_url = await self.github.publish_jules_outputs(
                            repo=task.repo,
                            base_sha=base_sha,
                            patch_text=git_patch,
                            title=title,
                            body=body,
                        )
                        task.pr_url = new_pr_url
                        task.state = TaskState.REVIEWING
                        self.store.save_task(task)
                        self.store.record_decision(
                            'orchestrator',
                            'patch_published_as_pr',
                            f'opened PR from {len(git_patch)}-byte Jules patch',
                            task.id,
                            meta={'kind': 'completion', 'pr_url': new_pr_url},
                        )
                        await self.gateway.send(
                            f'Task {task.id}: Jules returned a patch; '
                            f'opened PR {new_pr_url}. PR watcher will pick up '
                            f'checks from here.'
                        )
                        await self._record(
                            'info', task,
                            gist=f'Jules returned a patch; opened {new_pr_url}.',
                        )
                        # Fall through to the normal REVIEWING path
                        # below so gates, anomaly bookkeeping, and the
                        # autonomy-rung merge decision all run on the
                        # PR we just opened.
                        pr_url = new_pr_url
                    except Exception as exc:
                        # Translator raised (or GitHub rejected). Keep
                        # the patch in the ledger via a decision trace
                        # and surface to the owner; do not silently
                        # drop the work.
                        task.state = TaskState.AWAITING_APPROVAL
                        task.error = (
                            f'patch translation failed: {exc!r}; '
                            f'owner to apply manually'
                        )
                        self.store.save_task(task)
                        self.store.record_decision(
                            'orchestrator',
                            'patch_publish_failed',
                            repr(exc), task.id,
                            meta={'kind': 'completion', 'patch_bytes': len(git_patch)},
                        )
                        await self.gateway.send(
                            f'Task {task.id}: Jules returned a patch but '
                            f'translation failed ({exc!r}). Awaiting manual '
                            f'apply or /approve-behavior.'
                        )
                        await self._record(
                            'failure', task,
                            gist=f'Could not open PR from Jules patch ({exc!r}).',
                        )
                        return
                else:
                    # Below SUPERVISED the publisher isn't authorised;
                    # record the patch but don't apply it. Rung <= 1
                    # means "operator wants to see the change before
                    # anything happens" -- so we defer.
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
                        f'Patch captured ({len(git_patch)} bytes). '
                        f'Rung below SUPERVISED; awaiting owner decision.'
                    )
                    await self._record(
                        'info', task,
                        gist='Jules returned a patch, not a PR. Awaiting owner.',
                    )
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
        elif command == '/approve-behavior' and args:
            await self._approve_behavior(args[0])
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

    async def _approve_behavior(self, pr_token: str) -> None:
        '''``/approve-behavior <pr-token>``.

        The token is whatever the editor returned when ``/improve``
        opened the PR — an html_url for GitHub, a SHA for the local
        editor, a ``fake-...:file`` prefix for the fake editor.
        Resolved through ``Task.source_url``. Behavioural and
        low-stakes PRs both go through the same approval route now
        that the auto-merge watcher (Step 5) is on; the low-stakes
        ones will usually have already merged by the time the owner
        sees them. This handler is for the cases the watcher
        hesitated on.
        '''
        task = next(
            (
                t for t in self.store.list_tasks()
                if getattr(t, 'kind', 'dev') == 'behavior_pr'
                and t.source_url == pr_token
            ),
            None,
        )
        if task is None:
            await self.gateway.send(
                f'No /improve task with token {pr_token}. '
                f'Pass the html_url printed at /improve time.'
            )
            return
        if task.state not in (TaskState.AWAITING_APPROVAL, TaskState.QUEUED):
            await self.gateway.send(
                f'Task {task.id} is in state {task.state.value}; '
                f'nothing for /approve-behavior to merge.'
            )
            return
        try:
            merged = await self.github.merge_pr(pr_token)
        except Exception as exc:
            task.state = TaskState.FAILED
            task.error = repr(exc)
            self.store.save_task(task)
            self.store.record_decision(
                'orchestrator', 'approve_behavior_failed',
                repr(exc), task.id,
            )
            await self.gateway.send(
                f'/approve-behavior failed for task {task.id}: {exc!r}'
            )
            return
        task.state = TaskState.MERGED if merged else task.state
        self.store.save_task(task)
        self.store.record_decision(
            'owner', 'approved_behavior_merge',
            'manual approval from /approve-behavior',
            task.id,
            meta={'already_merged': not merged},
        )
        await self.gateway.send(
            f'Behaviour PR for task {task.id} '
            f'{"already merged" if not merged else "merged"}: {pr_token}'
        )

    # Step 5: live PR watcher — auto-merges low-stakes PRs whose CI is
    # green, surfaces failed checks, and never touches a behavioural
    # PR without owner approval. One asyncio.Task per Orchestrator.run
    # call; failure in this loop never bubbles out (the orchestrator
    # stays alive).
    async def _pr_watcher_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.settings.poll_prs_interval_s)
                await self._poll_behavior_prs()
            except Exception:
                log.exception('pr watcher cycle failed; will retry next interval')

    async def _poll_behavior_prs(self) -> None:
        # Behaviour PR tasks that need watching: state is one or
        # AWAITING_APPROVAL (waiting for CI or owner), and source_url
        # is a real GitHub html_url (not a fake-prefix or local SHA,
        # which the editor can't auto-poll).
        candidates = [
            t for t in self.store.list_tasks(TaskState.AWAITING_APPROVAL)
            if getattr(t, 'kind', 'dev') == 'behavior_pr'
            and t.source_url
            and t.source_url.startswith('http')
        ]
        for task in candidates:
            url = task.source_url
            assert url is not None  # narrowed by the candidates filter
            try:
                passed = await self.github.pr_checks_passed(url)
            except Exception:
                # 404 / 422 / network — GitHub isn't ready with the
                # PR yet. Back off and try again next cycle.
                self.watchdog.beat(f'pr:{task.id}')
                continue
            if not passed:
                # A check failed or is still running; surface the
                # failure once via the gateway and stay in
                # AWAITING_APPROVAL.
                self.store.record_decision(
                    'orchestrator', 'pr_checks_failed',
                    'CI gate not green; awaiting owner',
                    task.id,
                )
                await self.gateway.send(
                    f'Task {task.id}: PR CI not green. '
                    f'Investigate at {task.source_url}'
                )
                continue
            # CI is green. Branch on category derived from the
            # target file path stored in task.prompt.
            is_low_stakes = (
                task.prompt.startswith('prompts/')
                or task.prompt.startswith('playbook/')
            )
            if is_low_stakes:
                try:
                    await self.github.merge_pr(url)
                except Exception as exc:
                    self.store.record_decision(
                        'orchestrator', 'auto_merge_failed',
                        repr(exc), task.id,
                    )
                    await self.gateway.send(
                        f'PR for task {task.id} is green but merge failed: '
                        f'{exc!r}. Awaiting manual /approve-behavior.'
                    )
                    continue
                task.state = TaskState.MERGED
                self.store.save_task(task)
                self.store.record_decision(
                    'orchestrator', 'auto_merged',
                    'low-stakes PR auto-merged after green CI',
                    task.id,
                )
                await self.gateway.send(
                    f'Task {task.id} auto-merged: {task.source_url}'
                )
            else:
                # Behavioural: never auto-merge.
                self.store.record_decision(
                    'orchestrator', 'pr_ready_for_owner',
                    'CI green; awaiting /approve-behavior', task.id,
                )
                self.watchdog.beat(f'pr:{task.id}')

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
