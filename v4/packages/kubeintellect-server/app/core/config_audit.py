"""Guard configuration that parses cleanly and protects nothing.

Every security-relevant setting in this project is a comma-separated string, and every parser
for one **silently discards** what it cannot use. A discarded entry is not a syntax error and
not a log line: it is an operator who believes a namespace is protected, or a per-namespace
autonomy ceiling is in force, when nothing of the sort is configured.

Measured 2026-08-20 with ``KUBECTL_BLOCKED_NAMESPACES="Kube-System"`` — one capital letter:

* ``kubectl get pods -A`` returned the two ``kube-system`` rows the filter exists to remove;
* ``kubectl delete deployment coredns -n kube-system`` was **allowed**, where it is normally a
  ``[Protected]`` refusal at every role;
* the Loki/Prometheus namespace guard passed ``{namespace="kube-system"}`` straight through;
* :func:`app.autonomy.ladder.level_for_namespace` returned ``A1`` instead of the pinned ``A0``.

Case is now folded in :attr:`Settings.kubectl_blocked_namespaces`, which fixes that class
outright. What cannot be fixed by folding — a glob, a slash, a stray character — is reported
here instead of vanishing. This is the same idea as ``set_but_unwired_flags``
(:mod:`app.core.version`), one level down: a switch that does nothing must say so.

Nothing here refuses to start the server. An operator's typo should not take a cluster's
agent offline; it should be impossible to miss.

**This reporter had the blind spot it exists to remove.** Until 2026-08-20
:func:`autonomy_override_problems` validated the *level* of an override and never the namespace
it names, so an entry that parses but can never match was reported as fine. Measured with
``AUTONOMY_NAMESPACE_LEVELS="prod-*=A0"`` and ``AUTONOMY_LEVEL=A3`` — a natural thing to write,
because the sibling ``AUTONOMY_A3_ALLOWLIST`` *does* take globs and the documentation says so:

* :func:`unenforceable_guard_config` returned ``[]`` — nothing to see;
* ``GET /v1/v5/status`` reported ``unenforceable_guard_config: []`` and ``kq v5-status`` agreed;
* :func:`app.autonomy.ladder.level_for_namespace` returned **A3** for ``prod-web``, the
  permissive default the override existed to tighten — this failure mode is fail-**open**;
* and with ``AUTONOMY_A3_ALLOWLIST="CrashLoopBackOff/prod-*"``, which honours the glob,
  :func:`app.autonomy.ladder.a3_allowed` returned **True**: the watchtower would auto-fix in
  exactly the namespaces the operator believed were pinned to A0.

A glob, a slash and an embedded space were all silent. :func:`a3_allowlist_problems` had the
same class of gap (empty playbook, empty pattern, a second ``/``), failing closed rather than
open. Both now check the whole entry, not the half that was easy to validate.
"""
from __future__ import annotations

import re

from app.core.config import settings

# RFC 1123 label — what the Kubernetes API server accepts as a namespace name.
_RFC1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_AUTONOMY_LEVELS = ("A0", "A1", "A2", "A3")


def _legal_namespace(name: str) -> bool:
    return bool(name) and len(name) <= 63 and bool(_RFC1123_LABEL.match(name))


def blocked_namespace_problems() -> list[str]:
    """Entries of `KUBECTL_BLOCKED_NAMESPACES` that cannot ever match a namespace."""
    problems: list[str] = []
    for raw in settings.KUBECTL_BLOCKED_NAMESPACES.split(","):
        entry = raw.strip()
        if not entry:
            continue
        folded = entry.lower()
        if _legal_namespace(folded):
            continue
        hint = ""
        if "*" in entry or "?" in entry:
            hint = (" Globs are supported by AUTONOMY_A3_ALLOWLIST but NOT here — "
                    "list each namespace explicitly.")
        problems.append(
            f"KUBECTL_BLOCKED_NAMESPACES entry {entry!r} is not a legal Kubernetes namespace "
            f"name, so it protects nothing.{hint}")
    return problems


# kubectl accepts a resource as `name`, `name.version.group` (`secrets.v1.`) and with a
# trailing dot; nothing else is a resource token.
_RESOURCE_TOKEN = re.compile(r"^[a-z0-9][a-z0-9.-]*$")


def blocked_resource_problems() -> list[str]:
    """Entries of `KUBECTL_BLOCKED_RESOURCES` that cannot ever match a resource type."""
    problems: list[str] = []
    for raw in settings.KUBECTL_BLOCKED_RESOURCES.split(","):
        entry = raw.strip()
        if not entry:
            continue
        if _RESOURCE_TOKEN.match(entry.lower()):
            continue
        hint = ""
        if "*" in entry or "?" in entry:
            hint = " Globs are not supported here — list each resource type explicitly."
        problems.append(
            f"KUBECTL_BLOCKED_RESOURCES entry {entry!r} is not a resource type kubectl would "
            f"accept, so it blocks nothing.{hint}")
    return problems


def autonomy_override_problems() -> list[str]:
    """Entries of `AUTONOMY_NAMESPACE_LEVELS` that are dropped by the parser.

    Dropping one fails **open**: the namespace falls back to `AUTONOMY_LEVEL`, which is the
    permissive default the override existed to tighten.
    """
    problems: list[str] = []
    for raw in settings.AUTONOMY_NAMESPACE_LEVELS.split(","):
        entry = raw.strip()
        if not entry:
            continue
        if "=" not in entry:
            problems.append(
                f"AUTONOMY_NAMESPACE_LEVELS entry {entry!r} has no '=' and is ignored — "
                f"that namespace keeps the default level {settings.AUTONOMY_LEVEL}.")
            continue
        ns, level = entry.split("=", 1)
        if level.strip() not in _AUTONOMY_LEVELS:
            problems.append(
                f"AUTONOMY_NAMESPACE_LEVELS entry {entry!r} names level {level.strip()!r}, "
                f"which is not one of {'/'.join(_AUTONOMY_LEVELS)}, and is ignored — "
                f"namespace {ns.strip()!r} keeps the default level {settings.AUTONOMY_LEVEL}.")
            continue
        key = ns.strip().lower()
        # The empty key is left alone on purpose: `level_for_namespace("")` is the cluster-scoped
        # lookup, so `=A0` genuinely pins cluster-scoped objects. Undocumented, but not inert.
        if key and not _legal_namespace(key):
            hint = ""
            if "*" in key or "?" in key:
                hint = (" Globs are supported by AUTONOMY_A3_ALLOWLIST but NOT here — the lookup "
                        "is an exact match, so list each namespace explicitly.")
            problems.append(
                f"AUTONOMY_NAMESPACE_LEVELS entry {entry!r} names {ns.strip()!r}, which is not a "
                f"legal Kubernetes namespace name, so it can never match — every namespace it was "
                f"meant to pin keeps the default level {settings.AUTONOMY_LEVEL}.{hint}")
    return problems


def a3_allowlist_problems() -> list[str]:
    """Entries of `AUTONOMY_A3_ALLOWLIST` that are dropped by the parser.

    Dropping one fails **closed** — the pair is simply never auto-fixed — so this is a
    correctness report, not a security hole. It is here because an operator reading
    "auto-fix is enabled for X" from their own config deserves to know it is not.
    """
    problems: list[str] = []
    for raw in settings.AUTONOMY_A3_ALLOWLIST.split(","):
        entry = raw.strip()
        if not entry:
            continue
        if "/" not in entry:
            problems.append(
                f"AUTONOMY_A3_ALLOWLIST entry {entry!r} is not in playbook/namespace form and "
                "is ignored — that pair will never be auto-fixed.")
            continue
        playbook, pattern = (part.strip() for part in entry.split("/", 1))
        if not playbook:
            problems.append(
                f"AUTONOMY_A3_ALLOWLIST entry {entry!r} names no playbook, and the comparison is "
                "exact, so it matches no finding — that pair will never be auto-fixed.")
        elif not pattern:
            problems.append(
                f"AUTONOMY_A3_ALLOWLIST entry {entry!r} names no namespace pattern, which matches "
                "nothing — that pair will never be auto-fixed.")
        elif "/" in pattern:
            problems.append(
                f"AUTONOMY_A3_ALLOWLIST entry {entry!r} has more than one '/', so the pattern is "
                f"{pattern!r}; a namespace name cannot contain '/', so it matches nothing — that "
                "pair will never be auto-fixed.")
    return problems


def cors_origin_problems() -> list[str]:
    """Entries of `ALLOWED_ORIGINS` that no browser will ever match — and the one that matches
    far too much.

    This module opens by claiming *every* security-relevant comma-separated setting silently
    discards what it cannot use. It then audited four of the five. `ALLOWED_ORIGINS` was the
    fifth, and it is the only one whose failure mode runs in **both** directions.

    Measured 2026-08-24 against a real `CORSMiddleware`, which compares origins as exact strings:

    * ``"http://localhost:3080, http://app.example.com"`` — a space after the comma — a request
      from `http://app.example.com` came back with no `access-control-allow-origin` header at
      all. Fixed at the source by :attr:`Settings.allowed_origins`, which strips.
    * ``"http://app.example.com/"`` — a trailing slash — same silence. An `Origin` header never
      carries a path, so this can never match. Not repairable by guessing: stripping the slash
      would mean inventing an origin the operator did not write.
    * ``"*"`` — a request from `https://attacker.example` came back with
      ``access-control-allow-origin: https://attacker.example`` **and**
      ``access-control-allow-credentials: true``. This is the reverse of what an operator
      expects: the browser rule that credentialed requests are refused against a wildcard is
      never reached, because Starlette echoes the requesting origin instead of emitting `*`
      when credentials are enabled. `app/main.py` sets ``allow_credentials=True``
      unconditionally, so `*` means *any website a logged-in operator visits may call this API
      with their session*, not *anonymous read-only access*.

    Reported, never refused, and never silently rewritten — same posture as the rest of this
    module. An operator's typo should not take a cluster's agent offline; it should be
    impossible to miss.
    """
    problems: list[str] = []
    raw = settings.ALLOWED_ORIGINS
    for entry in (part.strip() for part in raw.split(",")):
        if not entry:
            continue
        if entry == "*":
            problems.append(
                "ALLOWED_ORIGINS contains '*' while CORS is configured with "
                "allow_credentials=True, so the server echoes the CALLING origin back and "
                "marks it credentialed — any site a logged-in operator visits can call this "
                "API with their session. This is not anonymous read-only access. List the "
                "origins you mean instead.")
            continue
        if "://" not in entry:
            problems.append(
                f"ALLOWED_ORIGINS entry {entry!r} has no scheme; a browser Origin header is "
                f"always 'scheme://host[:port]', so this matches nothing and that origin is "
                f"silently NOT allowed.")
            continue
        if entry.rstrip("/") != entry:
            problems.append(
                f"ALLOWED_ORIGINS entry {entry!r} ends in '/'; an Origin header never carries "
                f"a path, so this matches nothing and that origin is silently NOT allowed. "
                f"Write {entry.rstrip('/')!r}.")
    if raw.strip() and not settings.allowed_origins:
        problems.append(
            f"ALLOWED_ORIGINS is set to {raw!r} but yields no usable origin, so every "
            f"cross-origin browser request is refused.")
    return problems


def unenforceable_guard_config() -> list[str]:
    """Every configured guard entry that has no effect. Empty when the config is enforceable."""
    return (blocked_namespace_problems()
            + blocked_resource_problems()
            + autonomy_override_problems()
            + a3_allowlist_problems()
            + cors_origin_problems())


def log_guard_config_problems() -> list[str]:
    """Report the problems at startup. Returns them, so a caller can surface them too."""
    from app.utils.logger import get_logger

    problems = unenforceable_guard_config()
    logger = get_logger(__name__)
    for problem in problems:
        logger.error(f"guard_config_unenforceable: {problem}")
    return problems
