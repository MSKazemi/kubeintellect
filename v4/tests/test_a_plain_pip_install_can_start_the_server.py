"""Gate: `pip install kubeintellect` must produce a server that starts.

Found on 2026-08-29 by installing the freshly published **2.4.0** from PyPI into a clean
virtualenv and running the documented commands. `pip install kubeintellect` succeeded in
23.7 s and `kubeintellect --version` printed `kubeintellect 2.4.0` (exit 0 — the defect
that made 2.2.0 unusable was genuinely fixed). Then `kubeintellect serve` died:

    File ".../app/main.py", line 260, in <module>
        from prometheus_fastapi_instrumentator import Instrumentator
    ModuleNotFoundError: No module named 'prometheus_fastapi_instrumentator'

`METRICS_ENABLED` defaults to **True**, `app/main.py` imports the instrumentator at module
scope under that flag, and the published metadata declared the package only under
`extra == 'metrics'` / `extra == 'all'`. So the primary command of the release could not
run out of the box; it worked in development purely because the dev virtualenv had the
package installed for other reasons.

Two fixes, and this file pins both:

* the distribution now depends on it directly — a default-on feature is not an extra;
* the import is guarded — a control plane must not refuse to boot because a telemetry
  package is missing. Missing metrics is a degradation; a missing server is an outage.

The second is what makes this test possible: it hides the module and asserts `app.main`
still imports, which is exactly what a plain install does.
"""

from __future__ import annotations

import builtins
import importlib
import sys
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PYPROJECT = (
    _REPO_ROOT / "v4" / "packages" / "kubeintellect-server" / "pyproject.toml"
)
_DIST = "prometheus-fastapi-instrumentator"
_MODULE = "prometheus_fastapi_instrumentator"


def _project() -> dict:
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]


class TestTheDefaultOnFeatureIsACoreDependency:
    def test_metrics_is_on_by_default(self):
        """The premise. If metrics ever defaults off, the rule below can be relaxed."""
        from app.core.config import Settings

        assert Settings.model_fields["METRICS_ENABLED"].default is True

    def test_the_instrumentator_is_a_core_dependency(self):
        declared = " ".join(_project()["dependencies"])
        assert _DIST in declared, (
            f"{_DIST} is imported at module scope in app/main.py under a flag that "
            "defaults on, so it cannot live only in an extra — that is what stopped "
            "released 2.4.0 from starting"
        )


class TestTheServerStartsWithoutTheOptionalPackage:
    def test_app_main_imports_when_the_instrumentator_is_absent(self, monkeypatch):
        """Simulate a stripped install: the module is gone, the server still comes up."""
        real_import = builtins.__import__

        def _blocked(name, *args, **kwargs):
            if name == _MODULE or name.startswith(f"{_MODULE}."):
                raise ImportError(f"No module named {name!r}")
            return real_import(name, *args, **kwargs)

        for mod in [m for m in sys.modules if m == _MODULE or m.startswith(f"{_MODULE}.")]:
            monkeypatch.delitem(sys.modules, mod, raising=False)
        monkeypatch.delitem(sys.modules, "app.main", raising=False)
        monkeypatch.setattr(builtins, "__import__", _blocked)

        module = importlib.import_module("app.main")
        assert module.app is not None

    def test_the_guard_is_actually_in_the_source(self):
        """Cheap belt-and-braces: the import above can pass on a cached module."""
        source = (
            _REPO_ROOT
            / "v4"
            / "packages"
            / "kubeintellect-server"
            / "app"
            / "main.py"
        ).read_text(encoding="utf-8")
        head = source[source.index("if settings.METRICS_ENABLED:") :]
        assert "except ImportError" in head[:800], (
            "the module-scope instrumentator import must be guarded"
        )


@pytest.fixture(autouse=True)
def _restore_app_main():
    """Leave app.main importable for whatever runs next in this session."""
    yield
    sys.modules.pop("app.main", None)
    importlib.import_module("app.main")
