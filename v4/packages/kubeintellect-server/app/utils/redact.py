"""Secret/PII redaction for stored reflexion outcomes.

K8s manifests and kubectl commands routinely contain credentials, tokens,
internal hostnames, and bearer tokens. Anything that lands in the database
must pass through this redactor. The goal is defensive: we'd rather lose
useful context than leak a credential into a learned pattern.

Heuristic-based — full enumeration of secret formats is impossible. This
catches the common cases. Reviewers should still grep stored data for
patterns we missed.

**Why this is line-*aware* and not line-*wise* (fixed 2026-08-20).** The first
version classified each line independently and dropped any line containing a
keyword. YAML — the format almost everything here is written in — puts the name
of a thing and its value on *different lines*, so that rule removed the label
and kept the credential. Measured against a plain Deployment::

    - name: DB_PASSWORD        ->  # <redacted-line>          (dropped)
      value: hunter2-prod-db   ->    value: hunter2-prod-db   (KEPT)

The stored record was worse than an unredacted one: the secret survived and the
only occurrence of the word "password" was gone, so the review procedure this
module's own docstring prescribes -- *grep stored data for patterns we missed* --
returned nothing. A ``tls.key: |`` block scalar leaked its whole PEM body for the
same reason, plus a second one: ``tls.key`` contains no keyword, and
``-----BEGIN RSA PRIVATE KEY-----`` does not match ``private_key``.

So the rules are now, in order of preference:

1. **Keep the key, redact the value.** A key name is not a credential and is what
   makes the stored record auditable. Dropping the line is the last resort, kept
   only for free text that has no ``key: value`` shape.
2. **Values may live on later lines.** A secret key introducing a block scalar
   (``tls.key: |``) and the ``name:``/``value:`` pair Kubernetes uses for env vars
   are tracked across lines.
3. **Some keys are secrets by convention with no keyword in them** -- ``tls.key``,
   ``.dockerconfigjson``, ``id_rsa``.
4. **PEM armour is redacted wherever it appears**, regardless of the key above it.

Stated limits -- these are *not* covered, deliberately and testably:

- A value with no secret-looking key anywhere near it (``foo: hunter2``) is kept.
  Nothing in the text marks it as a credential.
- ``_TOKEN_RE`` still does not span ``+`` and ``/``, so a base64 blob embedded
  mid-line survives unless the whole line is base64. Widening it would redact
  filesystem paths, which diagnostics need.
- This guards what is **stored**. It is not applied to the prompt sent to the
  model provider.
"""
from __future__ import annotations

import re

# Tokens that mark a name as credential-bearing (case-insensitive). Matched
# against the *key* of a `key: value` line, and as a fallback against a whole
# line that has no such shape.
_SECRET_LINE_KEYWORDS = (
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "token",
    "bearer",
    "authorization",
    "credentials",
    "client_secret",
    "private_key",
    "privatekey",
    "ssh_key",
    "passphrase",
)

# Keys that carry no keyword but are credentials by convention -- the Secret
# `data:` names Kubernetes and its ecosystem use.
_SECRET_KEY_NAMES = frozenset({
    "tls.key", "ca.key", "server.key", "client.key", "key.pem",
    ".dockerconfigjson", ".dockercfg", ".netrc", ".pgpass",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "htpasswd", "keystore", "truststore", "keystore.jks",
})

# Internal hostnames in URLs are replaced with a sentinel. Keeps the
# protocol+path so the structural shape is preserved for diffing.
_URL_RE = re.compile(r"\b(https?)://[a-zA-Z0-9.\-_]+(?::\d+)?", re.IGNORECASE)

# Common bearer/JWT/long-base64 token shapes -- replaced inline.
_TOKEN_RE = re.compile(
    r"\b(?:eyJ[a-zA-Z0-9_\-\.]{20,}|[a-zA-Z0-9_\-]{32,})\b"
)

# Field-style assignments inside strings: `password: foo`, `token=foo`.
_KV_RE = re.compile(
    r"(?i)\b(password|passwd|secret|api[_-]?key|token|bearer|authorization|client[_-]?secret|private[_-]?key)"
    r"\s*[:=]\s*\S+",
)

# `  - name: FOO` / `  key: value` / `  tls.key: |` — and the same lines as kubectl writes
# them with `-o json`: `  "name": "FOO",`. The key quote is captured and back-referenced so an
# opening quote requires a closing one, and `_unwrap_value` gives the value's own punctuation
# back to the emitter, so a redacted JSON line stays a JSON line.
_LINE_RE = re.compile(r"^(\s*)(-\s+)?(\"?)([A-Za-z0-9_.\-/]+)\3\s*:\s*(.*?)\s*$")

# `"hunter2",` -> ('"', 'hunter2', '",').  `3,` -> ('', '3', ',').  `hunter2` -> ('', 'hunter2', '')
_QUOTED_VALUE_RE = re.compile(r'^"(.*)"(,?)$')

# A JSON value that opens a container: the material, if any, is on the lines below.
_CONTAINER_OPENERS = ("{", "[")


def _unwrap_value(raw: str) -> tuple[str, str, str]:
    """Split a captured value into `(open, inner, close)` so a redacted line keeps its shape.

    Classification runs on `inner` — the value without JSON quoting — and the emitter puts the
    punctuation back. Without this, `redact_secrets` was a **YAML** redactor that reported
    itself applied to any text: `kubectl get pod -o json` writes the k8s env idiom as
    `"name": "DB_PASSWORD",` / `"value": "hunter2-prod-db"`, which `_LINE_RE` did not match at
    all, so both lines fell through to free text and the credential was stored verbatim. The
    identical object in YAML was redacted correctly. Whether the secret was caught depended on
    the `-o` flag the caller happened to pass.
    """
    match = _QUOTED_VALUE_RE.match(raw)
    if match:
        return '"', match.group(1), '"' + match.group(2)
    if raw.endswith(","):
        return "", raw[:-1], ","
    return "", raw, ""

# PEM/OpenSSH armour. The body between BEGIN and END is dropped wholesale.
_ARMOR_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]+-----")
_ARMOR_END = re.compile(r"-----END [A-Z0-9 ]+-----")

# A whole line that is nothing but base64 -- key material rendered by a manifest
# renderer. Anchored so that filesystem paths (which carry `.`, `-`, `~`) miss.
_BASE64_LINE_RE = re.compile(r"^\s*[A-Za-z0-9+/]{40,}={0,2}\s*$")

# Block-scalar indicators: `key: |`, `key: >-`, `key: |+2`.
_BLOCK_SCALAR_RE = re.compile(r"^[|>][+\-]?\d*$")

_REDACTED = "<redacted>"
_REDACTED_BLOCK = "<redacted-block>"
_REDACTED_PEM = "<redacted-pem-block>"
_REDACTED_TOKEN = "<redacted-token>"
_REDACTED_HOST = "<redacted-host>"
_REDACTED_LINE = "# <redacted-line>"

# **Every marker this module can leave behind.** Anything that reports on a redaction has to
# count these, and `kubectl_tool._capture_note` used to carry its own hand-copied subset — it
# knew three of the six, so a capture whose only redaction was a PEM private key or a Secret's
# `data:` block was described to the operator as, in full, "redacted". The most secret-dense
# object there is produced the least informative note. A test asserts this tuple against the
# literals in this file's own source, so a new marker cannot be added without joining it.
REDACTION_MARKERS: tuple[str, ...] = (
    _REDACTED_LINE, _REDACTED_PEM, _REDACTED_BLOCK, _REDACTED_TOKEN, _REDACTED_HOST, _REDACTED,
)

# A dropped line leaves no trace of the value; the others stand in for one. Kept apart because
# an operator reading a rollback note needs to know which of the two happened.
_DROP_MARKERS: tuple[str, ...] = (_REDACTED_LINE,)


def count_redactions(text: str) -> tuple[int, int]:
    """`(lines dropped, values replaced)` in an already-redacted string.

    The markers are disjoint as substrings — `<redacted-token>` does not contain
    `<redacted>`, `<redacted-pem-block>` does not contain `<redacted-block>` — so counting
    each independently does not double-count. A test pins that.
    """
    dropped = sum(text.count(m) for m in _DROP_MARKERS)
    replaced = sum(text.count(m) for m in REDACTION_MARKERS if m not in _DROP_MARKERS)
    return dropped, replaced


def _is_secret_name(token: str) -> bool:
    """True if this key (or env-var name) denotes a credential."""
    low = token.strip().strip("\"'").lower()
    if low in _SECRET_KEY_NAMES:
        return True
    return any(kw in low for kw in _SECRET_LINE_KEYWORDS)


def _scrub_inline(line: str) -> str:
    line = _URL_RE.sub(lambda m: f"{m.group(1)}://<redacted-host>", line)
    return _TOKEN_RE.sub("<redacted-token>", line)


def redact_identifier(name: str) -> str:
    """Redact a **name** — a dict key, a label, an env-var name — not a line of text.

    `redact_secrets` is built for content and applies two rules that are wrong for an
    identifier: it drops a whole free-text line that merely *looks* secret, and it treats
    `key: value` shapes specially. Measured 2026-08-24, `redact_secrets("token")` returns
    `"# <redacted-line>"` and so does `redact_secrets("password")` — so redacting keys with it
    would rename two ordinary, non-secret field names to the *same* string and silently merge
    them into one entry of the record.

    A key name is not a credential; a key that *is* a credential (a bearer token used as a
    map key) is. So this applies only the substitutions that identify secret **material** —
    the `password=…` assignment shape and long token/URL shapes — and never the line rules.
    """
    if not name:
        return name
    return _scrub_inline(_KV_RE.sub(lambda m: f"{m.group(1)}=<redacted>", name))


def redact_secrets(text: str | None, *, max_chars: int | None = 1500) -> str:
    """Return a redacted copy of `text`. Deterministic; safe on all inputs.

    - `key: value` lines whose **key** names a credential keep the key and lose
      the value.
    - A credential key introducing a block scalar, and the `value:` that follows
      a secret-looking `name:`, are redacted **on their own lines**.
    - PEM armour is replaced by a single marker.
    - Long token-shaped strings are replaced with `<redacted-token>`.
    - URLs are stripped of host and port.
    - Free-text lines with no `key: value` shape fall back to the old behaviour:
      redact in place, else drop the line.
    - Output is hard-capped at max_chars with a `[...]` truncation marker.
    """
    if not text:
        return ""

    out_lines: list[str] = []
    in_armor = False
    # Indent of the key whose value is still to come, or None.
    pending_indent: int | None = None

    for line in text.splitlines():
        indent = len(line) - len(line.lstrip())

        # -- PEM armour, wherever it appears -----------------------------------
        if in_armor:
            if _ARMOR_END.search(line):
                in_armor = False
            continue
        if _ARMOR_BEGIN.search(line):
            out_lines.append(f"{' ' * indent}<redacted-pem-block>")
            in_armor = True
            pending_indent = None
            continue

        match = _LINE_RE.match(line)
        quote = match.group(3) if match else ""
        key = match.group(4) if match else ""
        raw = match.group(5) if match else ""
        open_q, value, close_q = _unwrap_value(raw)

        # -- A value still owed to a secret key on an earlier line -------------
        if pending_indent is not None:
            if match and key == "value" and indent >= pending_indent:
                out_lines.append(
                    f"{' ' * indent}{quote}value{quote}: {open_q}{_REDACTED}{close_q}")
                pending_indent = None
                continue
            if indent > pending_indent and line.strip():
                # Block-scalar body. One marker for the whole block.
                if not out_lines or out_lines[-1].strip() != _REDACTED_BLOCK:
                    out_lines.append(f"{' ' * indent}{_REDACTED_BLOCK}")
                continue
            pending_indent = None

        if not match:
            # No `key: value` shape -- free text. Old behaviour, unchanged.
            if any(kw in line.lower() for kw in _SECRET_LINE_KEYWORDS):
                redacted = _KV_RE.sub(lambda m: f"{m.group(1)}: {_REDACTED}", line)
                out_lines.append(redacted if redacted != line else "# <redacted-line>")
                continue
            if _BASE64_LINE_RE.match(line):
                out_lines.append(f"{' ' * indent}<redacted-token>")
                continue
            out_lines.append(_scrub_inline(line))
            continue

        dash = match.group(2) or ""
        prefix = f"{' ' * indent}{dash}{quote}{key}{quote}:"

        # -- The key itself names a credential ---------------------------------
        if _is_secret_name(key):
            if _BLOCK_SCALAR_RE.match(value):
                out_lines.append(f"{prefix} {raw}")
                pending_indent = indent
            elif not value or value in _CONTAINER_OPENERS:
                # A mapping follows (`valueFrom:`/`secretKeyRef:`, or a JSON `{`) --
                # references, not material. Children are judged on their own lines, and the
                # opener is kept so the document does not lose its structure.
                out_lines.append(f"{prefix} {raw}" if raw else prefix)
            else:
                out_lines.append(f"{prefix} {open_q}{_REDACTED}{close_q}")
            continue

        # -- The k8s env idiom: the name is here, the value is next ------------
        # The name is KEPT -- it is the label that makes the record auditable and
        # greppable, and it is not itself a credential.
        if key in ("name", "key") and _is_secret_name(value):
            out_lines.append(f"{prefix} {raw}")
            pending_indent = indent
            continue

        if _BASE64_LINE_RE.match(value):
            out_lines.append(f"{prefix} {open_q}<redacted-token>{close_q}")
            continue

        out_lines.append(_scrub_inline(line))

    result = "\n".join(out_lines)
    if max_chars is not None and len(result) > max_chars:
        result = result[: max_chars - 5] + "[...]"
    return result
