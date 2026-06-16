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
from typing import Any

DEFAULT_BASE_URL = 'https://jules.googleapis.com/v1alpha'
API_KEY_HEADER = 'X-Goog-Api-Key'

# Activity type strings observed across Jules API revisions. Several
# spellings are kept because Jules has shipped both snake_case and
# camelCase shapes; classify_activity tolerates either.
ACTIVITY_PLAN = ('plan_generated', 'planGenerated')
ACTIVITY_QUESTION = ('agent_question', 'userInputRequired', 'awaiting_user_feedback')
ACTIVITY_PROGRESS = ('progress_update', 'progressUpdated')
ACTIVITY_COMPLETED = ('session_completed', 'sessionCompleted')
ACTIVITY_FAILED = ('session_failed', 'sessionFailed')

SESSION_TERMINAL_STATES = ('COMPLETED', 'FAILED')


def classify_activity(activity: dict[str, Any]) -> str:
    '''Map a raw Jules activity to one of: failed, completed, plan, question, progress.'''
    kind = str(activity.get('type') or activity.get('kind') or '')
    keys = activity.keys()
    if kind in ACTIVITY_FAILED or 'sessionFailed' in keys:
        return 'failed'
    if kind in ACTIVITY_COMPLETED or 'sessionCompleted' in keys or 'pullRequestUrl' in keys:
        return 'completed'
    if kind in ACTIVITY_PLAN or 'planGenerated' in keys or 'plan' in keys:
        return 'plan'
    if kind in ACTIVITY_QUESTION or 'question' in keys:
        return 'question'
    return 'progress'


def activity_key(activity: dict[str, Any]) -> str:
    '''A stable identity for an activity, used to process each one exactly once.

    Jules does not guarantee that the activities list is append-only or that a
    given activity keeps the same position across polls (a transient question
    can be replaced by a completion at the same index). Deduplicating by list
    position is therefore unsafe; we key on the activity's own identifier when
    present and fall back to its classification plus salient payload so a new
    activity is never mistaken for one already handled.
    '''
    for id_field in ('id', 'name', 'activityId'):
        value = activity.get(id_field)
        if value:
            return f'{id_field}:{value}'
    kind = classify_activity(activity)
    payload = (
        activity.get('pullRequestUrl')
        or activity.get('pr_url')
        or activity.get('plan')
        or activity.get('question')
        or activity.get('text')
        or ''
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
