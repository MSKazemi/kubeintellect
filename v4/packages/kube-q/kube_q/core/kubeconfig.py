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

    # Minimal YAML scan: look for a `contexts:` block and extract `- name: X` entries
    # until indentation decreases. Not a full YAML parser, but handles the common case.
    names: list[str] = []
    in_contexts = False
    base_indent: int | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped.startswith("contexts:"):
            in_contexts = True
            base_indent = indent
            continue
        if (
            in_contexts
            and base_indent is not None
            and indent <= base_indent
            and not stripped.startswith("-")
        ):
            # Left the contexts block
            break
        if in_contexts and stripped.startswith("- name:"):
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
