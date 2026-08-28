#!/usr/bin/env python3
"""Compare the checks `main` requires against the checks CI actually produces.

`.github/ci.yml` produces 15 named checks; branch protection requires 9; and until 2026-08-28
nothing connected the two lists. A job could be added, renamed, or left out of the required set
and every PR would keep merging green — the one thing a required-check list exists to prevent.

Two modes, and the first always runs:

* **offline** — every check name CI produces must appear in `.github/required-checks.yml`,
  either as required or as deliberately-not-required with a dated reason. Adding a job to CI
  without deciding forces a failure here rather than inheriting silence.
* **live** — with an authenticated `gh`, the recorded list is compared against the actual
  branch-protection setting, including `strict` and `enforce_admins`. Drift in either direction
  is reported: a check required in the settings but absent from CI is just as broken as a CI
  check nobody requires, because a required context that never reports leaves PRs pending
  forever.

This is deliberately NOT a CI job: it needs a token with repo scope, and a workflow that reads
its own branch protection would be a credential added for a report. Run it by hand, or
`make check-required`.

Exit codes: 0 = consistent · 1 = drift · 2 = the record or the workflow could not be read.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
RECORD = ROOT / ".github" / "required-checks.yml"
REPO = "MSKazemi/kubeintellect"


def ci_check_names() -> list[str]:
    """Every name a PR shows, expanding each matrix `include` entry into its own check."""
    doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
    names: list[str] = []
    for job in doc["jobs"].values():
        template = job["name"]
        include = ((job.get("strategy") or {}).get("matrix") or {}).get("include")
        if not include:
            names.append(template)
            continue
        for entry in include:
            name = template
            for key, value in entry.items():
                name = name.replace("${{ matrix.%s }}" % key, str(value))
            names.append(name)
    return names


def live_protection() -> dict | None:
    """The live branch protection, or None when `gh` cannot read it (no token, no network)."""
    try:
        out = subprocess.run(
            ["gh", "api", f"repos/{REPO}/branches/main/protection"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def main() -> int:
    if not RECORD.exists() or not CI.exists():
        print(f"cannot read {RECORD} or {CI}", file=sys.stderr)
        return 2

    record = yaml.safe_load(RECORD.read_text(encoding="utf-8"))
    required = list(record["required"])
    not_required = dict(record["not_required"])
    produced = ci_check_names()

    failures: list[str] = []

    # ── offline: CI and the record must partition each other exactly ──────────────────
    accounted = set(required) | set(not_required)
    for name in sorted(set(produced) - accounted):
        failures.append(
            f"CI produces {name!r} and .github/required-checks.yml does not mention it — "
            f"decide whether main requires it, and write the reason if it does not"
        )
    for name in sorted(accounted - set(produced)):
        failures.append(
            f"{name!r} is recorded but CI no longer produces it — a required context that "
            f"never reports leaves every PR pending forever"
        )
    for name, reason in sorted(not_required.items()):
        if not str(reason).strip().startswith("2026-"):
            failures.append(f"{name!r} is not required and its reason is not dated")

    print(f"CI produces {len(produced)} check(s); the record accounts for {len(accounted)}.")

    # ── live: the record must match the actual setting ────────────────────────────────
    live = live_protection()
    if live is None:
        print(
            "live check SKIPPED — `gh api` could not read the branch protection "
            "(no token, no network, or no admin rights). The offline partition above still ran."
        )
    else:
        contexts = set(live["required_status_checks"]["contexts"])
        for name in sorted(contexts - set(required)):
            failures.append(f"main REQUIRES {name!r} but the record does not list it")
        for name in sorted(set(required) - contexts):
            failures.append(f"the record lists {name!r} as required but main does NOT require it")
        for key, expected in (record.get("settings") or {}).items():
            actual = (
                live["required_status_checks"]["strict"] if key == "strict"
                else live.get(key, {}).get("enabled")
            )
            if actual != expected:
                failures.append(f"branch protection {key}={actual!r}, record says {expected!r}")
        print(f"live: main requires {len(contexts)} check(s).")

    if failures:
        print()
        for line in failures:
            print(f"  ✗ {line}")
        return 1
    print("Required checks match the record.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
