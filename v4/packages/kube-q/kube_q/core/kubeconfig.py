"""
kubeconfig.py — Read kubectl context names from the local kubeconfig.

Uses ``kubectl`` if available (no YAML dependency). Falls back to parsing
~/.kube/config directly with a minimal YAML scan.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

_logger = logging.getLogger(__name__)


def _from_kubectl() -> list[str]:
    if shutil.which("kubectl") is None:
        return []
    try:
        out = subprocess.check_output(
            ["kubectl", "config", "get-contexts", "-o", "name"],
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return [ln.strip() for ln in out.decode("utf-8").splitlines() if ln.strip()]
    except (subprocess.SubprocessError, OSError) as exc:
        _logger.debug("kubectl get-contexts failed: %s", exc)
        return []


def _from_kubeconfig_file() -> list[str]:
    """Minimal parse of ~/.kube/config (or $KUBECONFIG). Extracts ``contexts[].name``."""
    kubeconfig = os.environ.get("KUBECONFIG", "").split(":")[0] or str(
        Path.home() / ".kube" / "config"
    )
    path = Path(kubeconfig)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    # Minimal YAML scan of the `contexts:` block. Not a full YAML parser, but it must handle the
    # layout kubectl itself writes:
    #
    #     contexts:
    #     - context:
    #         cluster: kind-kubeintellect
    #       name: kind-kubeintellect      <- the dash is on `context:`, not on `name:`
    #
    # This used to match only ``- name:``, the *other* valid ordering, so against a kubeconfig
    # written by kubectl it returned no contexts at all — and this is the branch that runs when
    # kubectl is absent, which is exactly when it is the only source there is. Key off the
    # position of the item's keys instead of which key happens to carry the dash.
    names: list[str] = []
    in_contexts = False
    base_indent = 0
    seq_indent: int | None = None
    item_key_indent: int | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)

        if stripped.startswith("contexts:"):
            in_contexts = True
            base_indent = indent
            seq_indent = None
            item_key_indent = None
            continue
        if not in_contexts:
            continue
        if indent <= base_indent and not stripped.startswith("-"):
            break  # a sibling key of `contexts:` — the block is over

        if stripped.startswith("-"):
            if seq_indent is None:
                seq_indent = indent
            if indent != seq_indent:
                # A nested sequence — `extensions:` entries live inside a context and carry a
                # `name:` of their own (`context_info`). They are not contexts.
                continue
            # A new entry. Its keys live one dash-width in, whichever one the dash carries.
            item_key_indent = indent + 2
            if stripped == "-":
                continue
            stripped = stripped[2:].lstrip()
            indent = item_key_indent

        if indent == item_key_indent and stripped.startswith("name:"):
            name = stripped.split(":", 1)[1].strip().strip("\"'")
            if name:
                names.append(name)
    return names


def list_contexts() -> list[str]:
    """Return kubectl context names, empty list if none found.

    Tries ``kubectl config get-contexts -o name`` first, then falls back to a
    minimal parse of the kubeconfig file.
    """
    names = _from_kubectl()
    if names:
        return names
    return _from_kubeconfig_file()
