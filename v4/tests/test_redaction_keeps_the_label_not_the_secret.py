"""The redactor must not delete the evidence and keep the credential.

Pass 83 of the standing audit (T38). `redact_secrets` is the single funnel every
stored artefact passes through — rollback captures (`kubectl_tool._capture_rollback_point`),
mutation captures (`coordinator`), episode summaries, preferences, flight-recorder
payload fields. It classified each line on its own, and dropped any line containing a
keyword.

YAML puts the name of a thing and its value on different lines. Measured against a
plain Deployment, the old redactor produced:

    - name: DB_PASSWORD        ->  # <redacted-line>          (dropped)
      value: hunter2-prod-db   ->    value: hunter2-prod-db   (KEPT)

The credential survived and the only occurrence of the word "password" did not — so the
review procedure the module's own docstring prescribes ("grep stored data for patterns
we missed") returned nothing. The stored record was *worse* than an unredacted one,
because it also looked clean.

These tests assert the inverted rule: **keep the key, drop the value.**
"""
from __future__ import annotations

import pytest

from app.utils.redact import redact_secrets

DEPLOYMENT_WITH_ENV = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  template:
    spec:
      containers:
      - name: api
        env:
        - name: DB_PASSWORD
          value: hunter2-prod-db
        - name: STRIPE_API_KEY
          value: sk_live_51H8xQ2eZvKY
        - name: LOG_LEVEL
          value: debug
"""

TLS_SECRET = """apiVersion: v1
kind: Secret
metadata:
  name: tls
stringData:
  tls.key: |
    -----BEGIN RSA PRIVATE KEY-----
    MIIEow+IBAAK/CAQEAtLK3Qb1a
    ZmluZ2Vy/cHJpbnQ+aGVsbG8K
    -----END RSA PRIVATE KEY-----
"""


# ── L1/L2: the value on the *next* line ───────────────────────────────────────

class TestTheValueOnTheNextLine:
    def test_the_env_var_value_is_redacted(self):
        out = redact_secrets(DEPLOYMENT_WITH_ENV, max_chars=4000)
        assert "hunter2-prod-db" not in out
        assert "sk_live_51H8xQ2eZvKY" not in out

    def test_the_env_var_name_is_kept(self):
        """The name is the label, not the credential — and it is what grep needs."""
        out = redact_secrets(DEPLOYMENT_WITH_ENV, max_chars=4000)
        assert "DB_PASSWORD" in out
        assert "STRIPE_API_KEY" in out

    def test_grepping_the_store_for_password_still_finds_this_record(self):
        # The old redactor deleted the only line containing the word, so the review
        # procedure the module prescribes came up empty on a record that was leaking.
        out = redact_secrets(DEPLOYMENT_WITH_ENV, max_chars=4000)
        assert [ln for ln in out.splitlines() if "password" in ln.lower()]

    def test_a_harmless_env_var_is_untouched(self):
        out = redact_secrets(DEPLOYMENT_WITH_ENV, max_chars=4000)
        assert "value: debug" in out
        assert "LOG_LEVEL" in out

    def test_the_next_sibling_entry_does_not_inherit_the_redaction(self):
        """Redaction must stop at the end of the secret entry, not run to EOF."""
        out = redact_secrets(
            "        - name: API_TOKEN\n"
            "          value: t0psecret\n"
            "        - name: PORT\n"
            "          value: 8080\n",
            max_chars=4000,
        )
        assert "t0psecret" not in out
        assert "value: 8080" in out

    def test_a_value_key_at_the_same_indent_is_still_caught(self):
        # `password:` and `value:` at equal indent — a mapping, not a list item.
        out = redact_secrets("name: DB_PASSWORD\nvalue: hunter2\n", max_chars=4000)
        assert "hunter2" not in out

    def test_structural_fields_survive(self):
        """`kind: Secret` is a type name. Deleting it is the wrong half to delete."""
        out = redact_secrets(TLS_SECRET, max_chars=4000)
        assert "kind: Secret" in out
        assert "apiVersion: v1" in out


# ── L3: block scalars ─────────────────────────────────────────────────────────

class TestBlockScalarBodies:
    def test_a_secret_block_scalar_body_is_removed(self):
        out = redact_secrets(
            "data:\n  client_secret: |\n    line-one-of-the-secret\n    line-two\n",
            max_chars=4000,
        )
        assert "line-one-of-the-secret" not in out
        assert "line-two" not in out
        assert "client_secret:" in out

    def test_the_block_ends_at_the_dedent(self):
        out = redact_secrets(
            "data:\n  password: |\n    hidden\nmetadata:\n  name: keep-me\n",
            max_chars=4000,
        )
        assert "hidden" not in out
        assert "name: keep-me" in out

    def test_a_harmless_block_scalar_survives(self):
        out = redact_secrets(
            "data:\n  nginx.conf: |\n    server { listen 80; }\n", max_chars=4000)
        assert "listen 80" in out


# ── L4: PEM armour ────────────────────────────────────────────────────────────

class TestPemArmour:
    def test_the_pem_body_never_reaches_the_store(self):
        out = redact_secrets(TLS_SECRET, max_chars=4000)
        assert "MIIEow+IBAAK/CAQEAtLK3Qb1a" not in out
        assert "BEGIN RSA PRIVATE KEY" not in out
        assert "<redacted-pem-block>" in out

    def test_armour_under_a_harmless_key_is_still_redacted(self):
        """`-----BEGIN …-----` is decisive on its own — no keyword needed above it."""
        out = redact_secrets(
            "notes: |\n  -----BEGIN OPENSSH PRIVATE KEY-----\n"
            "  b3BlbnNzaC1rZXktdjEAAAAA\n  -----END OPENSSH PRIVATE KEY-----\n",
            max_chars=4000,
        )
        assert "b3BlbnNzaC1rZXktdjEAAAAA" not in out
        assert "<redacted-pem-block>" in out

    def test_text_after_the_armour_block_resumes_normally(self):
        out = redact_secrets(
            "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
            "status: healthy\n",
            max_chars=4000,
        )
        assert "status: healthy" in out


# ── L5: keys that are secrets by convention ───────────────────────────────────

class TestKeysWithNoKeywordInThem:
    @pytest.mark.parametrize("key", [
        "tls.key", ".dockerconfigjson", "id_rsa", "ca.key", ".netrc", "htpasswd",
    ])
    def test_a_conventional_secret_key_is_redacted(self, key):
        out = redact_secrets(f"data:\n  {key}: c3VwZXItc2VjcmV0\n", max_chars=4000)
        assert "c3VwZXItc2VjcmV0" not in out, f"{key} leaked its value"
        assert f"{key}:" in out

    def test_tls_crt_is_not_a_secret(self):
        """A public certificate is not credential material — over-redacting hurts diagnosis."""
        out = redact_secrets("data:\n  tls.crt: aGVsbG8=\n", max_chars=4000)
        assert "aGVsbG8=" in out


# ── L6: whole-line base64 ─────────────────────────────────────────────────────

class TestWholeLineBase64:
    """The case `_TOKEN_RE` structurally cannot reach.

    `_TOKEN_RE` is `[a-zA-Z0-9_\-]{32,}` — it does not span `+` or `/`, so real base64
    is chopped into sub-32 fragments and survives it entirely. A blob of pure
    `[A-Za-z0-9]` is caught by `_TOKEN_RE` and proves nothing about this layer, so every
    case here carries the `+` and `/` that make it uniquely this layer's work.
    """

    def test_a_base64_value_with_plus_and_slash_is_redacted(self):
        blob = "QUJDREVG+0hJSktMTU5PUFFSU1RVVldY/WVphYmNkZWZnaGlqa2xtbm9w"
        assert "+" in blob and "/" in blob
        out = redact_secrets(f"data:\n  blob: {blob}\n", max_chars=4000)
        assert blob not in out

    def test_a_bare_base64_line_with_no_key_is_redacted(self):
        blob = "b3BlbnNzaC1rZXkt+jEAAAAABG5vbmUAAAAEbm9u/QAAAAAAAAABAAAB"
        out = redact_secrets(f"body:\n{blob}\n", max_chars=4000)
        assert blob not in out
        assert "<redacted-token>" in out

    def test_a_filesystem_path_is_not_mistaken_for_base64(self):
        path = "/var/lib/kubelet/pods/abc-123/volumes/kubernetes.io~secret/tok"
        out = redact_secrets(f"    mounted at {path}\n", max_chars=4000)
        # The line contains "secret", so the old free-text fallback still applies;
        # what must not happen is a silent whole-line base64 match on a path.
        assert "<redacted-token>" not in out


# ── The limits, asserted so they cannot be mistaken for coverage ──────────────

class TestStatedLimits:
    def test_an_unlabelled_value_is_not_detected(self):
        """Nothing in `foo: hunter2` marks it as a credential. Documented limit."""
        out = redact_secrets("foo: hunter2\n", max_chars=4000)
        assert "hunter2" in out

    def test_a_base64_blob_embedded_mid_line_survives(self):
        """`_TOKEN_RE` does not span `+`/`/`; widening it would eat filesystem paths."""
        out = redact_secrets("note: blob=QUJD+REVG/R0hJSktMTU5PUFFSU1RVVldY\n",
                             max_chars=4000)
        assert "QUJD+REVG/R0hJSktMTU5PUFFSU1RVVldY" in out


# ── Unchanged guarantees ──────────────────────────────────────────────────────

class TestTheOldGuaranteesStillHold:
    def test_inline_password_still_redacted(self):
        out = redact_secrets("name: api\npassword: hunter2\nport: 8080")
        assert "hunter2" not in out and "<redacted>" in out

    def test_url_host_still_stripped(self):
        out = redact_secrets("server: https://internal-prod-api.acme.local:8443/api")
        assert "internal-prod-api.acme.local" not in out

    def test_truncation_and_empty_inputs(self):
        big = "\n".join(f"line {i}: ok" for i in range(500))
        assert redact_secrets(big, max_chars=200).endswith("[...]")
        assert redact_secrets("") == "" and redact_secrets(None) == ""
