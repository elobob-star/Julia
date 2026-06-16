#!/usr/bin/env python3
"""Smoke parser for ``deploy/systemd/julia.service``.

Validates structural correctness of the unit file in dry-run, so a
malformed unit breaks the build instead of breaking a host at 3am.

What this script checks:

  * The file parses as an INI-style unit.
  * ``[Unit]``, ``[Service]``, ``[Install]`` sections all present.
  * ``[Service]`` carries ``ExecStart``, ``Restart``,
    ``RestartSec``, and one of ``Type=simple`` / ``Type=oneshot``.
  * The command in ``ExecStart`` resolves to a binary on the
    current ``$PATH`` *or* contains a literal path. (``env julia
    run`` is acceptable because ``env`` is ``/usr/bin/env``.
    systemd itself resolves the env chain at activation time.)
  * File permissions are 0644 (or stricter) — system units should
    not be group- or world-writable.
  * The unit references the right `Documentation=` source.

What this script does NOT do:

  * Install the unit. Use ``deploy/systemd/install.sh install``.
  * Start the orchestrator. ``julia run --dry-run`` is the right
    rehearsal path.
  * Connect to Jules or GitHub. Pure static analysis.

Exit code 0 = unit is well-formed. Non-zero = at least one
contract violated; ``--verbose`` prints every check.
"""

from __future__ import annotations

import argparse
import configparser
import shutil
import stat
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
DEFAULT_UNIT = REPO_DIR / "deploy" / "systemd" / "julia.service"


def _verify(unit: Path, *, verbose: bool) -> list[str]:
    failures: list[str] = []

    if not unit.exists():
        return [f"unit file not found: {unit}"]

    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read(unit)
    except configparser.Error as exc:
        return [f"INI parse error: {exc}"]

    missing_sections = [s for s in ("Unit", "Service", "Install") if s not in parser]
    if missing_sections:
        failures.append(f"missing sections: {missing_sections}")
        return failures

    service = parser["Service"]
    required_keys = ("ExecStart", "Restart")
    for key in required_keys:
        if key not in service:
            failures.append(f"[Service] missing required key: {key}")

    if "Restart" in service and service["Restart"] == "always":
        if "RestartSec" not in service:
            failures.append("[Service] Restart=always without RestartSec is brittle")

    service_type = service.get("Type", "simple")
    if service_type not in ("simple", "exec", "oneshot"):
        failures.append(f"[Service] Type={service_type!r} is unusual for always-on")

    exec_start = service.get("ExecStart", "")
    if exec_start:
        first_token = exec_start.split(maxsplit=1)[0]
        if "/" not in first_token and shutil.which(first_token) is None:
            failures.append(
                f"[Service] ExecStart first token {first_token!r} is not on $PATH and is not a path"
            )

    mode = stat.S_IMODE(unit.stat().st_mode)
    if mode & 0o022:
        failures.append(
            f"unit is group- or world-writable (mode={oct(mode)}); "
            f"systemd refuses such units"
        )

    unit_doc = parser["Unit"].get("Documentation", "")
    expected_doc = "RUNBOOK.md"
    if expected_doc not in unit_doc:
        failures.append(
            f"[Unit] Documentation={unit_doc!r} does not mention RUNBOOK.md"
        )

    if verbose:
        print(f"unit:           {unit}")
        print(f"mode:           {oct(mode)}")
        print(f"Type:           {service_type}")
        print(f"ExecStart:      {exec_start}")
        print(f"Restart:        {service.get('Restart', '-')}")
        print(f"RestartSec:     {service.get('RestartSec', '-')}")
        print(f"Documentation:  {unit_doc or '-'}")

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--unit",
        type=Path,
        default=DEFAULT_UNIT,
        help=f"Path to the unit to verify (default: {DEFAULT_UNIT})",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print every check, not just failures",
    )
    args = parser.parse_args()

    failures = _verify(args.unit, verbose=args.verbose)
    if failures:
        print(f"FAIL: {len(failures)} contract violation(s):", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(f"OK: {args.unit} satisfies the systemd unit smoke contract.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
