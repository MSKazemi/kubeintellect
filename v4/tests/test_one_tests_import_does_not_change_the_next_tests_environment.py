"""A module that calls `load_dotenv()` at import time changes every later test's environment.

`evaluation/runner.py` does exactly that, so importing `test_evaluation_runner.py` loaded the
developer's repo-root `.env` into `os.environ` for the rest of the pytest process. Measured:
`USE_SQLITE` went `None -> 'false'` at that import and stayed there, and
`test_a_missing_project_env_is_not_an_error` failed in the full suite while passing alone — the
suite's result depended on the contents of an untracked local file, and was green on CI (no `.env`)
and red on the machine that has one.

`conftest.py` clears five auth variables at collection time, which is *before* that import, so it
cannot undo it. `load_dotenv()` does not override a variable that is already set, which is why
those five survived and `USE_SQLITE` did not: the blast radius is exactly the variables conftest
does not name. The autouse `_isolate_environment` fixture closes the class rather than the instance.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _conftest():
    spec = importlib.util.spec_from_file_location("_v4_conftest_probe", HERE / "conftest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_isolation_fixture_restores_added_removed_and_changed_variables() -> None:
    fixture = _conftest()._isolate_environment
    # pytest wraps a fixture function; the underlying generator is what actually restores.
    gen = getattr(fixture, "__wrapped__", fixture)()
    next(gen)                                   # setup: snapshot

    os.environ["KI_TEST_ADDED"] = "x"
    os.environ["PATH"] = "/definitely-not-the-real-path"
    removed_key, removed_val = "KI_TEST_REMOVED", os.environ.get("KI_TEST_REMOVED")
    os.environ[removed_key] = "present"

    try:
        next(gen)                               # teardown: restore
    except StopIteration:
        pass

    assert "KI_TEST_ADDED" not in os.environ, "a variable a test added survived it"
    assert os.environ["PATH"] != "/definitely-not-the-real-path", "a changed variable was not restored"
    assert os.environ.get(removed_key) == removed_val


def test_the_suite_is_not_at_the_mercy_of_a_local_dotenv() -> None:
    """End-to-end: the two tests that collided, run together, in that order, in a fresh process.

    Asserting the fixture's mechanics is not enough — the leak is an ordering effect, and only
    running the importer before the asserter proves it is contained.
    """
    root = HERE.parents[1]
    env_file = root / ".env"
    if not env_file.is_file():
        # Nothing to leak here; the failure this guards reproduces only where a .env exists.
        import pytest
        pytest.skip("no repo-root .env on this machine — the leak cannot be reproduced")

    # From the repo root: the top-level `evaluation` package the importer needs is only
    # importable from there, which is also how the suite is actually run.
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly",
         "v4/tests/test_evaluation_runner.py",
         "v4/tests/test_every_command_reads_the_same_config.py"],
        cwd=root, capture_output=True, text=True, timeout=600,
    )
    assert p.returncode == 0, p.stdout[-4000:] + p.stderr[-2000:]
