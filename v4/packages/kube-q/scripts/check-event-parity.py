#!/usr/bin/env python3
"""
scripts/check-event-parity.py — CI guard for TS/Python event type parity.

Verifies that every event type and every data field defined in
kube_q/core/events.py is also present in web/lib/eventTypes.ts.

Exits non-zero (and prints a diff) if shapes diverge.

Usage:
    python scripts/check-event-parity.py
    # or from CI: python scripts/check-event-parity.py && echo "OK"
"""

import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).parent.parent

PY_EVENTS  = ROOT / "kube_q" / "core" / "events.py"
TS_EVENTS  = ROOT / "web" / "lib" / "eventTypes.ts"


# ── Python schema extraction ──────────────────────────────────────────────────

@dataclass
class PyModel:
    name: str
    fields: dict[str, str] = field(default_factory=dict)  # name → annotation


def extract_py_models(path: Path) -> dict[str, PyModel]:
    """Return every Pydantic BaseModel class found in path."""
    tree = ast.parse(path.read_text())
    models: dict[str, PyModel] = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        # Only BaseModel subclasses (direct or via alias)
        bases = [ast.unparse(b) for b in node.bases]
        if not any("BaseModel" in b or "_EventBase" in b for b in bases):
            continue

        model = PyModel(name=node.name)
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                fname = item.target.id
                fanno = ast.unparse(item.annotation)
                model.fields[fname] = fanno
        models[node.name] = model

    return models


# ── TypeScript interface extraction ───────────────────────────────────────────

@dataclass
class TsInterface:
    name: str
    fields: set[str] = field(default_factory=set)


def extract_ts_interfaces(path: Path) -> dict[str, TsInterface]:
    """Crude but sufficient TS interface parser (regex-based)."""
    text = path.read_text()
    interfaces: dict[str, TsInterface] = {}

    # Match: export interface Foo { ... }
    for m in re.finditer(r"export interface (\w+)[^{]*\{([^}]*)\}", text, re.DOTALL):
        iname = m.group(1)
        body  = m.group(2)
        iface = TsInterface(name=iname)
        for line in body.splitlines():
            line = line.strip().rstrip(";")
            if not line or line.startswith("//"):
                continue
            # field?: type  or  field: type
            fm = re.match(r"(\w+)\??:", line)
            if fm:
                iface.fields.add(fm.group(1))
        interfaces[iname] = iface

    return interfaces


# ── Mapping: Python model name → TS interface name ───────────────────────────

PY_TO_TS: dict[str, str] = {
    "StatusData":      "StatusData",
    "TokenData":       "TokenData",
    "ToolCallData":    "ToolCallData",
    "ToolResultData":  "ToolResultData",
    "HitlRequestData": "HitlRequestData",
    "UsageData":       "UsageData",
    "FinalData":       "FinalData",
    "ErrorData":       "ErrorData",
    "_EventBase":      "EventEnvelope",
    "StatusEvent":     "StatusEvent",
    "TokenEvent":      "TokenEvent",
    "ToolCallEvent":   "ToolCallEvent",
    "ToolResultEvent": "ToolResultEvent",
    "HitlRequestEvent":"HitlRequestEvent",
    "UsageEvent":      "UsageEvent",
    "FinalEvent":      "FinalEvent",
    "ErrorEvent":      "ErrorEvent",
}

# Fields that are Python-only (not part of the wire protocol) — skip in parity check
SKIP_PY_FIELDS: set[str] = set()


# ── Check ─────────────────────────────────────────────────────────────────────

def main() -> int:
    py_models  = extract_py_models(PY_EVENTS)
    ts_ifaces  = extract_ts_interfaces(TS_EVENTS)
    errors: list[str] = []

    for py_name, ts_name in PY_TO_TS.items():
        py_model = py_models.get(py_name)
        ts_iface = ts_ifaces.get(ts_name)

        if py_model is None:
            errors.append(f"Python model '{py_name}' not found in {PY_EVENTS}")
            continue
        if ts_iface is None:
            errors.append(f"TypeScript interface '{ts_name}' not found in {TS_EVENTS}")
            continue

        py_fields = {f for f in py_model.fields if f not in SKIP_PY_FIELDS}
        ts_fields = ts_iface.fields

        missing_in_ts = py_fields - ts_fields
        if missing_in_ts:
            errors.append(
                f"{py_name} → {ts_name}: Python fields not in TS: {sorted(missing_in_ts)}"
            )

    if errors:
        print("❌  Event type parity check FAILED:\n")
        for e in errors:
            print(f"  • {e}")
        print(f"\n  Python source: {PY_EVENTS}")
        print(f"  TS source:     {TS_EVENTS}")
        print("\n  Update web/lib/eventTypes.ts to match kube_q/core/events.py.")
        return 1

    py_count = len(PY_TO_TS)
    print(f"✓  Event parity OK — {py_count} model(s) match between Python and TypeScript.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
