"""A schema migration that failed must not report success.

`psql -f file.sql` prints each error, carries on to the next statement, and **exits 0**. That is
psql's documented default, and it made every path that applies our schema report success on a
migration that did not happen. Measured 2026-08-20 against `postgres:16-alpine` — the exact image
the Helm Job uses — running the real `schema.sql` as a role without `CREATE` on `public`, which is
the ordinary shape of a managed instance (RDS, ApsaraDB, Cloud SQL):

    70 statements failed · 0 of 18 tables created · psql exit code 0

Kubernetes reads that exit code, so the Job was marked **Succeeded** and `helm upgrade` reported
**deployed**. Nothing downstream contradicts it: `/readyz` deliberately does not probe Postgres
(one blip must not restart the pod), and every memory/recorder write is fire-and-forget by design,
so a missing table degrades the product silently. The only trace was a warning line in the server
log — `flight_recorder: 'decision_log' table missing` — that no operator is watching.

`--single-transaction` is the second half: without it a partial failure leaves a half-applied
schema, which is harder to reason about than either outcome. The schema is safe to run inside one
transaction because it contains no `CREATE INDEX CONCURRENTLY`, and safe to re-run because every
statement is `IF NOT EXISTS` — both asserted below, since either could regress and silently
invalidate the fix.

These tests are static assertions over the shipped artifacts. The exit-code behaviour itself needs
a live Postgres and is not re-run here; the measurement above is recorded in `.note.done.md`.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCHEMA = _ROOT / "packages" / "kubeintellect-server" / "app" / "db" / "schema.sql"
_CONFIGMAP = _ROOT / "deploy" / "helm" / "kubeintellect" / "templates" / "configmap-schema.yaml"
_JOB = _ROOT / "deploy" / "helm" / "kubeintellect" / "templates" / "job-db-init.yaml"
_MAKEFILE = _ROOT / "Makefile"
_DOCS = _ROOT / "docs"

#: A psql call that reads a file — the form that silently continues past an error.
_PSQL_FILE_CALL = re.compile(r"psql\b[^\n|]*?(?:-f\s|<\s*\S+\.sql)")


def _shipped_docs() -> list[Path]:
    """The docs this repository carries — not whatever markdown this disk happens to hold.

    Parametrizing over ``rglob`` made the size of the suite depend on the working directory:
    this tree carries `docs/evaluation.md`, which `.git/info/exclude` keeps out of the public
    repository, so the suite collected one more test here than in any clone. The doc-claims
    gate then wrote *that* number into AGENTS.md and failed for everyone else.

    Falls back to every file on disk when the tracked set cannot be determined (no git, an
    unpacked sdist, an empty index) — never to the empty list, which would check nothing.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--", "docs"],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        tracked = sorted(_ROOT / name for name in proc.stdout.split("\0") if name.endswith(".md"))
    except (OSError, subprocess.TimeoutExpired):
        tracked = []
    return tracked or sorted(_DOCS.rglob("*.md"))


def _guarded(call: str) -> bool:
    return "ON_ERROR_STOP=1" in call


class TestEveryShippedPathStopsOnError:
    def test_the_helm_job_stops_on_error(self):
        cmd = next(ln for ln in _JOB.read_text(encoding="utf-8").splitlines() if "psql" in ln and "-f " in ln)
        assert "ON_ERROR_STOP=1" in cmd, (
            "the db-init Job would be marked Succeeded by Kubernetes on a migration that failed "
            f"every statement: {cmd.strip()}"
        )
        assert "--single-transaction" in cmd, f"a failure could half-apply the schema: {cmd.strip()}"

    def test_make_db_init_stops_on_error(self):
        # Comment lines are stripped first. The first draft of this test read the whole recipe and
        # passed on the `@#` comment that *mentions* ON_ERROR_STOP — the same "a comment faked a
        # consumer" trap this audit has hit before. Assert on the command, never on prose.
        body = _MAKEFILE.read_text(encoding="utf-8").split("\ndb-init:", 1)[1].split("\n\n", 1)[0]
        command = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith(("@#", "#")))
        assert "psql" in command, command
        assert "ON_ERROR_STOP=1" in command and "--single-transaction" in command, command

    @pytest.mark.parametrize("doc", _shipped_docs())
    def test_no_documented_psql_command_swallows_errors(self, doc):
        """A command in the docs is a command an operator will run during an incident."""
        unguarded = [c.strip() for c in _PSQL_FILE_CALL.findall(doc.read_text(encoding="utf-8")) if not _guarded(c)]
        assert not unguarded, (
            f"{doc.relative_to(_ROOT)} documents a psql invocation that exits 0 on failure: "
            f"{unguarded}"
        )


class TestTheAssumptionsBehindTheFix:
    """`--single-transaction` and re-runnability are only safe while these hold."""

    def test_the_schema_has_no_concurrent_index(self):
        assert "CONCURRENTLY" not in _SCHEMA.read_text(encoding="utf-8").upper(), (
            "CREATE INDEX CONCURRENTLY cannot run inside a transaction — adding one silently "
            "breaks --single-transaction in the db-init Job"
        )

    def test_every_create_and_alter_is_idempotent(self):
        """The Job re-runs on every `helm upgrade`; a non-idempotent statement would fail it."""
        offenders = []
        for n, line in enumerate(_SCHEMA.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if re.match(r"^CREATE (TABLE|INDEX)\b", stripped) and "IF NOT EXISTS" not in stripped:
                offenders.append(f"{n}: {stripped[:80]}")
            if re.match(r"^ALTER TABLE\b.*\bADD COLUMN\b", stripped) and "IF NOT EXISTS" not in stripped:
                offenders.append(f"{n}: {stripped[:80]}")
        assert not offenders, "post-upgrade hook re-runs this; these would fail it:\n" + "\n".join(offenders)


class TestTheChartAppliesTheRealSchema:
    """The Helm Job applies the ConfigMap's copy, not `schema.sql` — so the copy must not drift.

    Nothing enforced this: the ConfigMap embeds 456 lines of SQL literally rather than reading the
    file. They are identical today. If they ever diverge, the Job applies a stale-but-valid schema,
    exits 0, and reports Succeeded — the same class of lie this module exists to prevent, just one
    step further back.
    """

    def _configmap_body(self) -> str:
        lines = _CONFIGMAP.read_text(encoding="utf-8").splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "schema.sql: |") + 1
        return "\n".join(ln[4:] if ln.startswith("    ") else ln for ln in lines[start:])

    def test_the_configmap_copy_matches_schema_sql(self):
        expected = _SCHEMA.read_text(encoding="utf-8").rstrip("\n")
        actual = self._configmap_body().rstrip("\n")
        if expected != actual:
            import difflib
            diff = "\n".join(list(difflib.unified_diff(
                expected.splitlines(), actual.splitlines(),
                "schema.sql", "configmap-schema.yaml", lineterm="", n=1))[:40])
            pytest.fail(
                "the Helm chart ships a stale copy of the schema — the db-init Job would apply it "
                f"and report success:\n{diff}"
            )

    def test_the_configmap_body_is_not_empty(self):
        """Guard on the guard: an extraction bug would make the comparison vacuously pass."""
        body = self._configmap_body()
        assert len(body.splitlines()) > 400 and "CREATE TABLE" in body, len(body.splitlines())


class TestTheDocSweepIsTheSameSizeInEveryCheckout:
    """A suite whose size depends on the working directory cannot have a gated count.

    `sorted(_DOCS.rglob("*.md"))` parametrized this sweep over every markdown file on disk,
    so a file the repository does not carry — a private doc, a scratch note — added a test
    case. The doc-claims gate counts collected tests and publishes the number in AGENTS.md;
    the number was therefore a property of the machine, not of the repository.
    """

    def test_the_sweep_is_never_empty(self):
        assert _shipped_docs(), "the psql sweep would have checked no documents at all"

    def test_a_document_the_repository_does_not_carry_is_not_swept(self):
        phantom = _DOCS / "zz_guard_phantom.md"
        phantom.write_text("# not in any clone\n", encoding="utf-8")
        try:
            swept = _shipped_docs()
        finally:
            phantom.unlink(missing_ok=True)
        assert phantom not in swept, (
            "a stray markdown file changed the number of tests this suite collects"
        )

    def test_a_tree_without_git_still_sweeps_every_document(self, monkeypatch):
        """Falling back to nothing would turn the sweep into a gate that checks no files."""
        def _no_git(*args, **kwargs):
            raise OSError("git is not available here")

        monkeypatch.setattr(subprocess, "run", _no_git)
        assert _shipped_docs() == sorted(_DOCS.rglob("*.md"))
