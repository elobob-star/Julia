'''Jules behavioral dossier (vision section 7).

Everything Julia believes about how Jules behaves lives HERE and only
here, because Jules evolves quickly and assumptions must be cheap to
correct (vision section 8): when drift is detected, the fix is a small
reviewable change to this module plus playbook/jules-playbook.md.

VERIFY-AT-BUILD: endpoint shapes and activity types below were compiled
from the v3.0 vision dossier and public Jules API documentation as of
2026-06. Re-verify against current docs before trusting in production;
the daily canary task (vision section 6) exists to catch silent drift.
'''

from __future__ import annotations

from pathlib import Path

DEFAULT_BASE_URL = 'https://jules.googleapis.com/v1alpha'
API_KEY_HEADER = 'X-Goog-Api-Key'

# Activity type strings observed across Jules API revisions. Several
# spellings are kept because Jules has shipped both snake_case and
# camelCase shapes; classify_activity tolerates either.
# Activity event-shaped payloads (vision section 7, verified 2026-06-16).
# The live wire format is *presence-of-key*, not a ``type`` discriminator:
PLAN_EVENT = 'planGenerated'
APPROVED_EVENT = 'planApproved'
QUESTION_EVENT = 'agentMessaged'
PROGRESS_EVENT = 'progressUpdated'
COMPLETED_EVENT = 'sessionCompleted'
FAILED_EVENT = 'sessionFailed'

# Older snake_case tags are kept as fallbacks so the FakeJulesClient
# used in tests and --dry-run continues to drive the spine without
# fixture rewrites.
ACTIVITY_PLAN = ('plan_generated', PLAN_EVENT)
ACTIVITY_QUESTION = ('agent_question', 'userInputRequired', 'awaiting_user_feedback', QUESTION_EVENT)
ACTIVITY_PROGRESS = ('progress_update', 'progressUpdated', PROGRESS_EVENT)
ACTIVITY_COMPLETED = ('session_completed', 'sessionCompleted', COMPLETED_EVENT)
ACTIVITY_FAILED = ('session_failed', 'sessionFailed', FAILED_EVENT)

SESSION_TERMINAL_STATES = ('COMPLETED', 'FAILED')


def classify_activity(activity):
    """Map a raw Jules activity to one of: failed, completed, plan, question, progress.

    The live wire (verified 2026-06-16) uses event-shaped payloads
    rather than a ``type`` field. The discriminator is the *presence
    of one of the ``*Event`` keys above*. Older snake_case ``type``
    strings are tolerated so FakeJulesClient fixtures continue to
    drive the spine in --dry-run and tests.
    """
    keys = activity.keys()
    kind = str(activity.get('type') or activity.get('kind') or '')
    if COMPLETED_EVENT in keys or activity.get('pullRequestUrl') or kind in ACTIVITY_COMPLETED:
        return 'completed'
    if FAILED_EVENT in keys or kind in ACTIVITY_FAILED:
        return 'failed'
    if APPROVED_EVENT in keys:
        return 'progress'  # the gate opened; nothing for the orchestrator.
    if QUESTION_EVENT in keys or kind in ACTIVITY_QUESTION or 'question' in keys:
        return 'question'
    if PLAN_EVENT in keys or kind in ACTIVITY_PLAN or 'plan' in keys:
        return 'plan'
    if PROGRESS_EVENT in keys or kind in ACTIVITY_PROGRESS:
        return 'progress'
    return 'progress'


def extract_plan_text(activity):
    """Return the user-visible plan text for an activity.

    Live plans are stepped (``plan.steps[i].title``); older shapes
    were free text in ``plan``/``description``. Tolerate both shapes
    so tests/--dry-run and the live path both produce a string.

    Live shape observed 2026-06-16: the plan lives under
    ``activity['planGenerated']['plan']``. Older shape: directly at
    ``activity['plan']``. Both are looked up here.
    """
    plan = activity.get('plan')
    if not isinstance(plan, dict):
        pg = activity.get('planGenerated')
        if isinstance(pg, dict):
            plan = pg.get('plan')
    if isinstance(plan, dict):
        steps = plan.get('steps')
        if isinstance(steps, list) and steps:
            titles = [
                str(s.get('title') or s.get('description') or '').strip()
                for s in steps
            ]
            return '\n'.join(f'{i+1}. {t}' for i, t in enumerate(titles) if t)
        return str(plan.get('description') or plan.get('text') or '')
    return str(plan or activity.get('description') or activity.get('text') or '')


def extract_pr_url(activity):
    """Return the PR URL for a completed activity, or empty string."""
    top = activity.get('pullRequestUrl') or activity.get('pr_url')
    if isinstance(top, str) and top:
        return top
    return ''


def extract_git_patch(activity):
    """Return the unidiff patch from a completed activity, if any.

    When Jules falls short of opening a GitHub PR (single-line or
    trivial tasks), the orchestrator uses this as a fallback to
    branch the change itself. Live artifacts observed 2026-06-16.
    """
    artifacts = activity.get('artifacts')
    if not isinstance(artifacts, list):
        return ''
    for art in artifacts:
        cs = art.get('changeSet') if isinstance(art, dict) else None
        if not isinstance(cs, dict):
            continue
        patch = cs.get('gitPatch') if isinstance(cs.get('gitPatch'), dict) else None
        if patch and isinstance(patch.get('unidiffPatch'), str):
            return patch['unidiffPatch']
    return ''


def activity_key(activity):
    """A stable identity for an activity, used to process each one exactly once."""
    for id_field in ('id', 'name', 'activityId'):
        value = activity.get(id_field)
        if value:
            return f'{id_field}:{value}'
    kind = classify_activity(activity)
    payload = (
        extract_pr_url(activity)
        or extract_plan_text(activity)
        or extract_git_patch(activity)[:80]
        or str(activity.get('text') or '')
    )
    return f'{kind}:{payload}'



# Prompt patterns are engineering artifacts (vision section 5.4): they
# are versioned here and changed only through reviewable commits.
CLARIFICATION_SYSTEM_PROMPT = (
    'You are Julia, an orchestrator answering a coding agent clarification '
    'question on behalf of its owner. Answer in one or two sentences, '
    'decisively. Prefer the simplest reasonable interpretation, the '
    'repository default branch, and minimal well-tested changes. '
    'Never invent credentials, secrets or URLs.'
)

PLAN_REVIEW_SYSTEM_PROMPT = (
    'You are Julia, reviewing a coding agent plan before approval. Reply '
    'APPROVE if the plan is a reasonable, minimal path to the goal, or '
    'REVISE: <one sentence> if it is clearly off-goal or destructive. '
    'Plans touching protected branches, history rewrites, secret values '
    'or deletion of unrelated code must always get REVISE.'
)

# Known-good canary prompt (vision section 6): tiny, deterministic in
# shape, safe to run daily against a sandbox repository.
CANARY_PROMPT = (
    'Canary task: append the current date as a single line to CANARY.md '
    '(create the file if missing). Make no other changes.'
)

# ------------- Phase 2: behavior repo loader (vision section 8) -------------

_PROMPT_FILENAMES = {
    "plan_review": ("prompts/plan_review.md", PLAN_REVIEW_SYSTEM_PROMPT),
    "clarification": ("prompts/clarification.md", CLARIFICATION_SYSTEM_PROMPT),
    "canary": ("prompts/canary.md", CANARY_PROMPT),
}


def load_prompt(name: str, behaviors_root: Path | None) -> str:
    """Return the prompt text for ``name``, preferring the behaviors repo.

    The behavioral playbook lives in version control (vision section 8).
    When a behaviors repo checkout is configured, the orchestrator pulls
    prompt text from ``prompts/<name>.md`` so the running model is talking
    to the latest reviewed wording. When the repo is absent or the file is
    missing, fall back to the embedded default constant.

    The embedded defaults are kept verbatim in this file so legacy
    callers, dry-run mode, and the test suite all continue to work
    without a behaviors checkout. The intent is *additive* loaders,
    not a rewrite of the dossier.
    """
    filename, fallback = _PROMPT_FILENAMES[name]
    if behaviors_root is None:
        return fallback
    candidate = Path(behaviors_root) / filename
    if not candidate.exists():
        return fallback
    text = candidate.read_text()
    # Strip the leading markdown heading so the prompt is just text.
    if "\n" in text:
        parts = text.split("\n", 2)
        return parts[2].strip() if len(parts) >= 3 else text
    return text
