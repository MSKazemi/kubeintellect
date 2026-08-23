"""#168 — a non-UTF-8 byte from kubectl/helm/git must not take the caller down.

Two halves, and the second is what keeps the first honest:

  * the runtime contract — a child that writes an undecodable byte is survivable
    with `errors="replace"` and fatal without it, run for real against a real
    child process rather than asserted from the signature;
  * the policy — the call sites that read free-form CLUSTER output actually carry
    `errors="replace"`. `scripts/check-text-encoding.py` gates `encoding=`, which
    is a rule a checker can state. Which sites deserve `errors=` is a judgement,
    so it is pinned here by name instead: dropping it from `run_kubectl` would
    otherwise be an invisible, green regression back to #168.

The byte is 0x80 — a UTF-8 continuation byte with no lead byte, invalid in any
position. `kubectl logs` returns whatever the container wrote to stdout, so this
is a container writing latin-1, not a hypothetical.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

_SERVER = Path(__file__).resolve().parent.parent / "packages" / "kubeintellect-server"

# A child that writes one raw undecodable byte between two readable lines.
_EMIT_BAD_BYTE = (
    "import sys; "
    "sys.stdout.buffer.write(b'NAME  READY\\nweb-\\x80-0  1/1\\n'); "
    "sys.stdout.buffer.flush()"
)


def test_replace_survives_an_undecodable_byte():
    proc = subprocess.run(
        [sys.executable, "-c", _EMIT_BAD_BYTE],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert proc.returncode == 0
    # Nothing else is lost: both lines survive and only the bad byte is marked.
    assert "NAME  READY" in proc.stdout
    assert "1/1" in proc.stdout
    assert "�" in proc.stdout, "the replacement must be visible, not silent"


def test_strict_would_have_failed_the_whole_read():
    """Not vacuous: without errors="replace" the same child kills the caller."""
    with pytest.raises(UnicodeDecodeError):
        subprocess.run(
            [sys.executable, "-c", _EMIT_BAD_BYTE],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )


@pytest.mark.parametrize(
    ("relpath", "func", "why"),
    [
        ("app/tools/kubectl_tool.py", "run_kubectl", "kubectl logs/describe/get -o yaml"),
        ("app/tools/kubectl_tool.py", "_capture_rollback_point", "pre-state YAML snapshot"),
        ("app/tools/helm_tool.py", "run_helm", "helm get values/manifest"),
        ("app/agent/nodes/context_fetcher.py", "_run_kubectl_snapshot", "pod list + events"),
    ],
)
def test_cluster_output_readers_keep_errors_replace(relpath: str, func: str, why: str):
    path = _SERVER / relpath
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    calls = [
        node
        for parent in ast.walk(tree)
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)) and parent.name == func
        for node in ast.walk(parent)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("run", "Popen", "check_output")
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]
    assert calls, f"{relpath}:{func} no longer runs a subprocess — re-pin this test"
    for call in calls:
        kwargs = {k.arg for k in call.keywords if k.arg}
        assert "encoding" in kwargs, f"{relpath}:{call.lineno} ({why}) lost encoding="
        assert "errors" in kwargs, (
            f'{relpath}:{call.lineno} ({why}) lost errors="replace" — one bad byte '
            "would take the whole read down again (#168)"
        )
