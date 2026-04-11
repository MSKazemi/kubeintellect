# app/utils/code_security.py
"""
Static analysis and integrity utilities for runtime-generated tool code.

Three layers of defence applied before exec():
  1. AST static analysis — reject code containing dangerous imports or calls.
  2. SHA-256 checksum verification — detect PVC tampering between write and load.

These are complementary to the HITL approval gate (the primary control).
"""

import ast
import hashlib
from typing import List, Tuple

from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")

# ---------------------------------------------------------------------------
# Blocked patterns
# ---------------------------------------------------------------------------

# Top-level import names that generated tools must never use.
# Generated tools interact with Kubernetes exclusively through
# app.services.kubernetes_service — they have no legitimate reason to open
# raw sockets, spawn processes, or do unmediated HTTP.
_BLOCKED_IMPORTS: frozenset = frozenset({
    "subprocess",
    "socket",
    "ctypes",
    "requests",
    "httpx",
    "aiohttp",
    "shutil",
    "ftplib",
    "smtplib",
    "telnetlib",
    "pty",
    "pickle",
    "marshal",
    "shelve",
    "multiprocessing",
    "concurrent",  # concurrent.futures could spawn threads that outlive the call
})

# Bare function / builtin call names that are always dangerous in generated code.
_BLOCKED_CALLS: frozenset = frozenset({
    "eval",
    "exec",
    "compile",
    "__import__",
})

# Attribute names on the `os` (or `os.path`) object that allow shell escape
# or filesystem mutation.
_BLOCKED_OS_ATTRS: frozenset = frozenset({
    "system",
    "popen",
    "execv",
    "execve",
    "execvp",
    "execvpe",
    "spawnl",
    "spawnle",
    "spawnlp",
    "spawnlpe",
    "spawnv",
    "spawnve",
    "spawnvp",
    "spawnvpe",
    "fork",
    "forkpty",
    "kill",
    "killpg",
    "remove",
    "unlink",
    "rmdir",
    "removedirs",
    "chmod",
    "chown",
    "chroot",
    "setuid",
    "setgid",
})


# ---------------------------------------------------------------------------
# AST visitors
# ---------------------------------------------------------------------------

class _SecurityVisitor(ast.NodeVisitor):
    """Collect security violations from an AST."""

    def __init__(self) -> None:
        self.violations: List[str] = []

    # --- imports ------------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root in _BLOCKED_IMPORTS:
                self.violations.append(
                    f"line {node.lineno}: blocked import '{alias.name}'"
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        root = module.split(".")[0]
        if root in _BLOCKED_IMPORTS:
            self.violations.append(
                f"line {node.lineno}: blocked import 'from {module} import ...'"
            )
        self.generic_visit(node)

    # --- dangerous calls ----------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        # eval(...), exec(...), compile(...), __import__(...)
        if isinstance(node.func, ast.Name):
            if node.func.id in _BLOCKED_CALLS:
                self.violations.append(
                    f"line {node.lineno}: blocked call '{node.func.id}()'"
                )

        # os.system(...), os.fork(), etc.
        if isinstance(node.func, ast.Attribute):
            if node.func.attr in _BLOCKED_OS_ATTRS:
                # Only flag when the object looks like `os` or `os.path`
                obj = node.func.value
                if isinstance(obj, ast.Name) and obj.id == "os":
                    self.violations.append(
                        f"line {node.lineno}: blocked call 'os.{node.func.attr}()'"
                    )
                elif isinstance(obj, ast.Attribute) and obj.attr == "path":
                    pass  # os.path attrs are fine
                elif isinstance(obj, ast.Name) and obj.id in ("subprocess", "shutil"):
                    self.violations.append(
                        f"line {node.lineno}: blocked call '{obj.id}.{node.func.attr}()'"
                    )

        self.generic_visit(node)

    # --- open() with write modes -------------------------------------------

    def visit_Call_open(self, node: ast.Call) -> None:  # called manually below
        """Flag open() when a writable mode is detectable at AST level."""
        WRITE_MODES = {"w", "a", "x", "wb", "ab", "xb", "w+", "a+", "x+", "r+"}
        if len(node.args) >= 2:
            mode_arg = node.args[1]
            if isinstance(mode_arg, ast.Constant) and mode_arg.value in WRITE_MODES:
                self.violations.append(
                    f"line {node.lineno}: open() called with writable mode '{mode_arg.value}'"
                )
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                if kw.value.value in WRITE_MODES:
                    self.violations.append(
                        f"line {node.lineno}: open() called with writable mode '{kw.value.value}'"
                    )

    def visit_Call(self, node: ast.Call) -> None:  # noqa: F811 (intentional override)
        # Dangerous bare calls
        if isinstance(node.func, ast.Name):
            if node.func.id in _BLOCKED_CALLS:
                self.violations.append(
                    f"line {node.lineno}: blocked call '{node.func.id}()'"
                )
            if node.func.id == "open":
                self.visit_Call_open(node)

        # os.* / subprocess.* / shutil.* attribute calls
        if isinstance(node.func, ast.Attribute):
            if node.func.attr in _BLOCKED_OS_ATTRS:
                obj = node.func.value
                if isinstance(obj, ast.Name) and obj.id in ("os", "subprocess", "shutil"):
                    self.violations.append(
                        f"line {node.lineno}: blocked call "
                        f"'{obj.id}.{node.func.attr}()'"
                    )

        self.generic_visit(node)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_tool_code(code: str) -> Tuple[bool, List[str]]:
    """
    Parse and statically analyse generated tool code.

    Args:
        code: Python source code to analyse.

    Returns:
        (is_safe, violations) where is_safe is True when no violations were
        found and violations is a (possibly empty) list of human-readable
        problem descriptions.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        msg = f"Syntax error in generated code: {exc}"
        logger.warning(msg)
        return False, [msg]

    visitor = _SecurityVisitor()
    visitor.visit(tree)

    if visitor.violations:
        logger.warning(
            "Static analysis found %d violation(s) in generated tool code: %s",
            len(visitor.violations),
            visitor.violations,
        )
        return False, visitor.violations

    return True, []


def compute_code_checksum(code: str) -> str:
    """Return the SHA-256 hex digest of the given source code string."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()
