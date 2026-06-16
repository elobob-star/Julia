"""Behavior-as-PRs substrate (vision section 8).

The orchestrator owns everything above Jules' per-repo memory; this
package is the bridge. Three Protocol methods (``record_playbook_entry``,
``propose_low_stakes_change``, ``propose_behavioral_change``) let the
orchestrator write back to its own behavior repo through reviewable
PRs flowing through the same pipeline Julia uses for user code.

Three implementations exist:

* :class:`FakeBehaviorEditor` — deterministic for tests/dry-run.
* :class:`LocalBehaviorEditor` — commits to a local git checkout
  of the behaviors repo. Used when ``--behaviors PATH`` is set but
  the user is not yet pushing to GitHub.
* :class:`GitHubBehaviorEditor` — opens real PRs through the
  GitHub API. Defer to Phase 3 once the behaviors repo lands on
  ``github.com/<owner>/behaviors``.

A ``DENYLIST`` of paths and patterns is enforced by all three
implementations: ``policies/safety.md`` and anything that looks like
a secret is hard-refused, regardless of rung.
"""
