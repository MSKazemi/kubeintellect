"""Schema versioning — make a stale or downgraded database loud instead of silent (A11).

`schema.sql` is applied by hand (`kubeintellect db-init`) and is written to be idempotent:
`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE … ADD COLUMN IF NOT EXISTS`. Re-running it is safe,
which is genuinely useful — and it is also why, until 2026-08-28, **nothing recorded that it had
ever been run.** The database held no version, no fingerprint and no application timestamp, so
these three states were indistinguishable from one another and from a healthy install:

* the operator upgraded the image and forgot `db-init` — the new columns do not exist;
* the operator rolled the Deployment *back* while the database kept the newer schema;
* the schema is exactly right.

The first two do not fail loudly. Every memory, recorder and audit write in this system is
fire-and-forget by design (a memory failure must never break a user response), so a missing
column becomes a logged warning inside a swallowed exception: memory silently stops recording,
`/healthz` keeps saying `enabled: true`, and the first symptom is an empty table weeks later —
the failure mode `memory_status()` and `audit_status()` were each built to end, arriving through
a door nobody had covered.

So this module adds the missing fact, not a migration engine. `schema_migrations` records which
version was applied, its fingerprint and when; `check_schema()` runs once when the pool opens and
classifies the result; `schema_status()` puts it on `/healthz` beside the other subsystems.

⚠️ **What this is NOT, and the dated reason A11 is not green.** 2026-08-28: there is still no
migration *tool* here — no ordered revisions, no down-migrations, and no support for a change
that is not additive. Idempotent `IF NOT EXISTS` DDL cannot express a column rename, a type
change or a backfill, and re-running it against a database that needs one is a silent no-op. That
needs Alembic (or equivalent) with a baseline revision stamped from this fingerprint; what is
here is the detector that tells an operator a migration is *needed*, which is the half that was
missing entirely.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from app.utils.logger import get_logger

logger = get_logger(__name__)

#: Bumped by hand when `schema.sql` changes. This integer is what a database records, and what
#: makes a *downgrade* (a database newer than the running binary) distinguishable from drift.
#: It also appears as a literal in `schema.sql`'s own stamping INSERT — a test asserts they agree.
SCHEMA_VERSION = 2

#: The fingerprint of `schema.sql` at :data:`SCHEMA_VERSION`. This is the **build-time** half of
#: A11 and the part that actually enforces the discipline: change one line of DDL without bumping
#: the version and updating this pin, and the suite fails. Without it, `SCHEMA_VERSION` is a
#: number somebody has to remember to increment, which is not a migration policy.
PINNED_FINGERPRINT = "85dd2e78e2c324e23c0754660dc9ed2aa8c20852e8769b42991e89891ae09ec4"

#: Set once at startup by :func:`check_schema`. Read by `/healthz` — never a live query, because
#: `/healthz` is liveness and a probe that touches Postgres turns one blip into a restart loop.
_state: dict[str, Any] = {
    "state": "unknown",
    "expected_version": SCHEMA_VERSION,
    "applied_version": None,
    "matches": None,
    "reason": "not checked yet",
}

_COMMENT = re.compile(r"--[^\n]*")


def _ddl_only(sql: str) -> str:
    """Strip comments and blank lines so a reworded comment is not reported as schema drift."""
    stripped = _COMMENT.sub("", sql)
    return "\n".join(ln.strip() for ln in stripped.splitlines() if ln.strip())


def schema_sql() -> str:
    """The shipped `schema.sql`, read the same way `kubeintellect db-init` reads it."""
    import importlib.resources as pkg_resources
    return pkg_resources.files("app.db").joinpath("schema.sql").read_text(encoding="utf-8")


def schema_fingerprint(sql: str | None = None) -> str:
    """SHA-256 over the DDL of `schema.sql`, comments and whitespace excluded."""
    return hashlib.sha256(_ddl_only(sql if sql is not None else schema_sql()).encode()).hexdigest()


RECORD_SQL = """
    INSERT INTO schema_migrations (version, fingerprint, applied_by)
    VALUES (%s, %s, %s)
    ON CONFLICT (version) DO UPDATE SET
        fingerprint = EXCLUDED.fingerprint,
        applied_at  = now(),
        applied_by  = EXCLUDED.applied_by
"""

_READ_SQL = """
    SELECT version, fingerprint, applied_at FROM schema_migrations
    ORDER BY version DESC LIMIT 1
"""


def record_args(applied_by: str = "db-init") -> tuple[int, str, str]:
    """Parameters for :data:`RECORD_SQL`. Separated so the CLI needs no async machinery."""
    return SCHEMA_VERSION, schema_fingerprint(), applied_by


async def check_schema(pool: Any) -> dict[str, Any]:
    """Classify the database against the shipped schema. Never raises; caches into `_state`.

    Five outcomes, and the two that matter are the ones that used to look identical to health:

    * ``current``     — recorded version and fingerprint both match.
    * ``stale``       — the fingerprint differs: `schema.sql` changed since `db-init` last ran, so
      columns this build writes to may not exist. **The write paths swallow that.**
    * ``ahead``       — the recorded version is *newer* than this binary: the Deployment was rolled
      back while the database was not. The dangerous direction, because the new code's DDL will
      not restore the old code's assumptions.
    * ``unrecorded``  — the ledger is empty or absent: a database created before this ledger
      existed, or one that never had `db-init` run against it at all.
    * ``unknown``     — the check itself could not run. Not a verdict; see `reason`.
    """
    global _state
    expected_fp = schema_fingerprint()
    try:
        row = await pool.fetchrow(_READ_SQL)
    except Exception as exc:
        _state = {"state": "unknown", "expected_version": SCHEMA_VERSION, "applied_version": None,
                  "matches": None, "reason": f"could not read schema_migrations: {exc}"}
        logger.warning(f"schema: version check could not run ({exc})")
        return _state

    if row is None:
        _state = {
            "state": "unrecorded", "expected_version": SCHEMA_VERSION, "applied_version": None,
            "matches": False,
            "reason": ("no row in schema_migrations — this database predates the ledger or has "
                       "never had `kubeintellect db-init` run against it; the schema this build "
                       "writes to is unverified"),
        }
        logger.warning(f"schema: {_state['reason']}")
        return _state

    applied_version = int(row["version"])
    recorded_fp = str(row["fingerprint"] or "")
    # An empty fingerprint means the row was stamped by `schema.sql` itself, piped into psql by
    # the chart's db-init Job, which cannot compute a hash of the file it is running. That is not
    # drift — it is the version-only answer, and the build-time pin is what guards the DDL.
    matches = (recorded_fp == expected_fp) if recorded_fp else (applied_version == SCHEMA_VERSION)
    if applied_version > SCHEMA_VERSION:
        state, reason = "ahead", (
            f"the database is at schema v{applied_version} but this build expects "
            f"v{SCHEMA_VERSION} — the deployment was rolled back and the database was not. "
            f"Running this build's DDL will NOT restore the older shape."
        )
    elif not matches:
        state, reason = "stale", (
            "schema.sql has changed since `kubeintellect db-init` last ran. Columns this build "
            "writes to may not exist, and every memory/recorder/audit write is fire-and-forget — "
            "the failure is logged and swallowed, not raised. Run `kubeintellect db-init`."
        )
    elif not recorded_fp:
        state, reason = "current", (
            "version matches; fingerprint not recorded (applied by `schema.sql` directly, as the "
            "chart's db-init Job does). Drift within a version is caught at build time, not here."
        )
    else:
        state, reason = "current", ""

    _state = {"state": state, "expected_version": SCHEMA_VERSION,
              "applied_version": applied_version, "matches": matches, "reason": reason}
    if state == "current":
        logger.info(f"schema: v{applied_version}, fingerprint matches")
    else:
        logger.warning(f"schema: {state} — {reason}")
    return _state


def schema_status() -> dict[str, Any]:
    """Shape reported on ``/healthz``. ``state == 'current'`` is the only clean answer."""
    return dict(_state)


def reset_for_tests() -> None:
    global _state
    _state = {"state": "unknown", "expected_version": SCHEMA_VERSION, "applied_version": None,
              "matches": None, "reason": "not checked yet"}
