"""`ALLOWED_ORIGINS` was the one comma-separated guard setting the guard audit did not audit.

`app/core/config_audit.py` opens by stating that *every* security-relevant setting in this
project is a comma-separated string whose parser silently discards what it cannot use, and that
"a switch that does nothing must say so". It then checked four of the five. The fifth was CORS,
and it is the only one whose failure runs in **both** directions.

Measured 2026-08-24 against a real `CORSMiddleware`, which compares origins as exact strings:

    ALLOWED_ORIGINS='http://localhost:3080, http://app.example.com'
        request from http://app.example.com  -> access-control-allow-origin: (absent)
    ALLOWED_ORIGINS='http://app.example.com/'
        request from http://app.example.com  -> access-control-allow-origin: (absent)
    ALLOWED_ORIGINS='*'
        request from https://attacker.example
            -> access-control-allow-origin: https://attacker.example
               access-control-allow-credentials: true

…and `unenforceable_guard_config()` returned `[]` — "the config is enforceable" — for all three.

The wildcard is the one that matters. `app/main.py` sets `allow_credentials=True`
unconditionally, and Starlette then echoes the *calling* origin instead of emitting `*`, so the
browser rule that credentialed requests are refused against a wildcard is never reached. `*`
here does not mean "anonymous read-only access"; it means any site a logged-in operator visits
may call this API with their session.

Two different remedies, on purpose. Whitespace is repaired at the source — stripping can only
ever allow origins the operator explicitly wrote. A trailing slash, a missing scheme and a bare
`*` are reported and left alone: repairing those would mean inventing an origin nobody typed, or
silently overriding a deliberate (if dangerous) choice.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.core import config_audit
from app.core.config import settings


@pytest.fixture(autouse=True)
def _restore_origins():
    before = settings.ALLOWED_ORIGINS
    yield
    settings.ALLOWED_ORIGINS = before


def _app(origins: list[str]) -> TestClient:
    """The middleware stack exactly as `app/main.py` builds it."""
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware, allow_origins=origins,
        allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
    )

    @app.get("/v1/ping")
    def ping():  # noqa: ANN202
        return {"ok": True}

    return TestClient(app)


def _allow_origin_for(configured: str, calling: str) -> str | None:
    settings.ALLOWED_ORIGINS = configured
    response = _app(settings.allowed_origins).get(
        "/v1/ping", headers={"Origin": calling}
    )
    return response.headers.get("access-control-allow-origin")


# ── 1. the whitespace case, repaired at the source ────────────────────────────────────────────


class TestAListWrittenTheNaturalWayWorks:
    def test_an_origin_after_a_comma_space_is_allowed(self):
        """The defect, in one test: this used to come back with no header at all."""
        assert _allow_origin_for(
            "http://localhost:3080, http://app.example.com", "http://app.example.com"
        ) == "http://app.example.com"

    def test_the_first_origin_still_works(self):
        assert _allow_origin_for(
            "http://localhost:3080, http://app.example.com", "http://localhost:3080"
        ) == "http://localhost:3080"

    def test_an_unlisted_origin_is_still_refused(self):
        """Vacuity guard: stripping must not have turned this into an allow-all."""
        assert _allow_origin_for(
            "http://localhost:3080, http://app.example.com", "https://attacker.example"
        ) is None

    def test_empty_entries_are_dropped_rather_than_becoming_an_origin(self):
        settings.ALLOWED_ORIGINS = "http://localhost:3080,,  ,"
        assert settings.allowed_origins == ["http://localhost:3080"]

    def test_stripping_never_widens_the_set(self):
        settings.ALLOWED_ORIGINS = " http://a.example , http://b.example "
        assert settings.allowed_origins == ["http://a.example", "http://b.example"]


# ── 2. the wildcard, reported not rewritten ───────────────────────────────────────────────────


class TestTheWildcardIsReported:
    def test_star_with_credentials_echoes_the_caller(self):
        """Pinning the behaviour that makes the warning worth printing. If Starlette ever stops
        echoing, this test fails and the wording in `cors_origin_problems` must be revisited."""
        settings.ALLOWED_ORIGINS = "*"
        response = _app(settings.allowed_origins).get(
            "/v1/ping", headers={"Origin": "https://attacker.example"}
        )
        assert response.headers.get("access-control-allow-origin") == "https://attacker.example"
        assert response.headers.get("access-control-allow-credentials") == "true"

    def test_the_audit_names_the_credential_consequence(self):
        settings.ALLOWED_ORIGINS = "*"
        problems = config_audit.cors_origin_problems()
        assert len(problems) == 1
        assert "allow_credentials=True" in problems[0]
        assert "not anonymous read-only access" in problems[0]

    def test_the_wildcard_is_not_silently_removed(self):
        """Reported, never rewritten — the rest of this audit module has the same posture, and
        an operator who meant it must not find the setting quietly overridden."""
        settings.ALLOWED_ORIGINS = "*"
        assert settings.allowed_origins == ["*"]


# ── 3. the entries that can never match ───────────────────────────────────────────────────────


class TestUnmatchableEntriesAreReported:
    def test_a_trailing_slash_is_reported_with_the_correction(self):
        settings.ALLOWED_ORIGINS = "http://app.example.com/"
        problems = config_audit.cors_origin_problems()
        assert len(problems) == 1
        assert "'http://app.example.com'" in problems[0]

    def test_a_trailing_slash_really_does_not_match(self):
        """The premise of the warning above, measured rather than asserted."""
        assert _allow_origin_for("http://app.example.com/", "http://app.example.com") is None

    def test_a_missing_scheme_is_reported(self):
        settings.ALLOWED_ORIGINS = "localhost:3080"
        assert "has no scheme" in config_audit.cors_origin_problems()[0]

    def test_a_missing_scheme_really_does_not_match(self):
        assert _allow_origin_for("localhost:3080", "http://localhost:3080") is None

    def test_a_value_that_yields_nothing_is_reported(self):
        settings.ALLOWED_ORIGINS = " , "
        assert "no usable origin" in config_audit.cors_origin_problems()[0]

    def test_an_unset_value_is_not_a_problem(self):
        """Empty is a legitimate "no browser clients" deployment, not a typo."""
        settings.ALLOWED_ORIGINS = ""
        assert config_audit.cors_origin_problems() == []

    def test_a_correct_list_reports_nothing(self):
        settings.ALLOWED_ORIGINS = "http://localhost:3080,https://ki.example.com"
        assert config_audit.cors_origin_problems() == []


# ── 4. it reaches the surfaces the other four guards reach ────────────────────────────────────


class TestItIsPartOfTheGuardAudit:
    def test_the_wildcard_shows_up_in_unenforceable_guard_config(self):
        """`unenforceable_guard_config()` is what `/v1/v5/status`, `kq v5-status` and the
        startup log read. A check nobody calls is the same as no check."""
        settings.ALLOWED_ORIGINS = "*"
        assert any("ALLOWED_ORIGINS" in p for p in config_audit.unenforceable_guard_config())

    def test_a_clean_config_leaves_the_report_empty(self):
        settings.ALLOWED_ORIGINS = "http://localhost:3080"
        assert not [p for p in config_audit.unenforceable_guard_config()
                    if "ALLOWED_ORIGINS" in p]

    def test_the_startup_logger_reports_it_as_an_error(self, mocker):
        settings.ALLOWED_ORIGINS = "*"
        logger = mocker.Mock()
        mocker.patch("app.utils.logger.get_logger", lambda _name: logger)
        problems = config_audit.log_guard_config_problems()
        assert any("ALLOWED_ORIGINS" in p for p in problems)
        assert any("ALLOWED_ORIGINS" in c.args[0] for c in logger.error.call_args_list)


# ── 5. the server really is built from the parsed property ────────────────────────────────────


class TestMainUsesTheParsedProperty:
    def test_the_raw_split_is_gone_from_main(self):
        """A guard on the seam: `main.py` builds its middleware at import time, so no test can
        rebuild it with a different setting. What is checkable is that it no longer splits the
        raw string itself — which is where the whitespace entry came from."""
        from pathlib import Path
        main = Path(__file__).resolve().parents[1] / (
            "packages/kubeintellect-server/app/main.py")
        source = main.read_text()
        assert 'ALLOWED_ORIGINS.split(",")' not in source
        assert "allow_origins=settings.allowed_origins," in source
