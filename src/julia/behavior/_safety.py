'''Compat shim for behavior-as-PRs safety categoriser.

Vision §8 + §15 promise one shared safety surface between the
orchestrator runtime (``julia.behavior.editor``) and the offline
opener (``behaviors/scripts/self_improve.py``). The canonical source
of truth is ``behaviors/scripts/_safety.py``; this module is a
thin reconciler that:

1. Tries to import from ``behaviors/scripts/_safety.py`` relative to
   the project root (``../behaviors/scripts/_safety.py`` from
   ``src/julia/behavior/``).
2. Falls back to the vendored definitions below if that import fails
   (a clean install with no ``behaviors`` checkout still works;
   symmetric with how ``cli.py`` already falls back when no editor
   is wired).
3. Re-exports the names via ``__getattr__`` so the runtime sees a
   single, stable module surface regardless of which binding wins
   at import. This shape dodges the mypy-vs-dynamic-import fight:
   everything that needs the runtime values asks the module for
   them by name; the names are not statically type-checked but
   tests pin behaviour.

A regression test (``behaviors/tests/test_safety.py``) imports this
shim and ``behaviors/scripts/self_improve.py`` and asserts the
DENYLIST, LOCKED_FILES, LOW_STAKES_DIRS, BEHAVIOURAL_DIRS, Category
enum, and BehaviorDenied exception resolve to the same Python
objects on each side. Drift fails the build.
'''

from __future__ import annotations

import enum
import os
import re
import sys
from typing import Any


def _try_load_canonical():
    candidates: list[str] = []
    env_path = os.environ.get('JULIA_BEHAVIORS_PATH')
    if env_path:
        candidates.append(env_path)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(
        os.path.normpath(
            os.path.join(here, '..', '..', '..', 'behaviors', 'scripts')
        )
    )
    for path in candidates:
        if not os.path.isdir(path):
            continue
        sys.path.insert(0, path)
        try:
            module = __import__('_safety')
        except ImportError:
            continue
        if hasattr(module, 'categorise') and hasattr(module, 'DENYLIST'):
            return module
    return None


# Single resolution at module load. Both branches populate the same
# in-memory dict, and ``__getattr__`` reads from it. There is no
# module-level re-binding, no mypy-visible shadow names, no
# ``from _safety import ...`` so we don't trigger mypy's
# "duplicate definition" diagnostics.
_bindings: dict[str, Any] = {}
_canonical = _try_load_canonical()
if _canonical is not None:
    _bindings['_canonical'] = _canonical
    _bindings['Category'] = _canonical.Category
    _bindings['BehaviorDenied'] = _canonical.BehaviorDenied
    _bindings['DENYLIST'] = _canonical.DENYLIST
    _bindings['LOCKED_FILES'] = _canonical.LOCKED_FILES
    _bindings['LOW_STAKES_DIRS'] = _canonical.LOW_STAKES_DIRS
    _bindings['BEHAVIOURAL_DIRS'] = _canonical.BEHAVIOURAL_DIRS
    _bindings['categorise'] = _canonical.categorise
else:
    # --------------------------------- fallback (vendored) -----------------
    # Keep this in sync with ``behaviors/scripts/_safety.py``. The drift
    # guard test in ``behaviors/tests/test_safety.py`` catches any
    # divergence the moment a ``behaviors`` checkout is present.
    LOW_STAKES_DIRS = ('playbook/', 'prompts/')
    BEHAVIOURAL_DIRS = ('policies/',)
    LOCKED_FILES = frozenset({'policies/safety.md'})
    DENYLIST = re.compile(
        r'(secret|password|credential|api[_-]?key|token|\.env$|\.key$)',
        re.IGNORECASE,
    )

    class Category(str, enum.Enum):
        LOW_STAKES = 'low-stakes'
        BEHAVIOURAL = 'behavioural'
        LOCKED = 'locked'

    class BehaviorDenied(RuntimeError):
        '''Raised when the categoriser refuses a behavior PR.'''

    def categorise(file: str) -> Category:
        if file in LOCKED_FILES:
            raise BehaviorDenied(
                f"refusing to open a PR against locked file {file!r}"
            )
        if DENYLIST.search(file):
            raise BehaviorDenied(
                f"refusing to open a PR: {file!r} matches the safety denylist"
            )
        if any(file.startswith(p) for p in BEHAVIOURAL_DIRS):
            return Category.BEHAVIOURAL
        if any(file.startswith(p) for p in LOW_STAKES_DIRS):
            return Category.LOW_STAKES
        raise BehaviorDenied(
            f"refusing to open a PR against {file!r}: not under a tracked directory"
        )

    _bindings['Category'] = Category
    _bindings['BehaviorDenied'] = BehaviorDenied
    _bindings['DENYLIST'] = DENYLIST
    _bindings['LOCKED_FILES'] = LOCKED_FILES
    _bindings['LOW_STAKES_DIRS'] = LOW_STAKES_DIRS
    _bindings['BEHAVIOURAL_DIRS'] = BEHAVIOURAL_DIRS
    _bindings['categorise'] = categorise


def __getattr__(name: str) -> Any:
    try:
        return _bindings[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return list(_bindings)


__all__ = [
    'Category',
    'BehaviorDenied',
    'DENYLIST',
    'LOCKED_FILES',
    'LOW_STAKES_DIRS',
    'BEHAVIOURAL_DIRS',
    'categorise',
]
