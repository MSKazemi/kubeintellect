"""Check a manifest against the cluster's own OpenAPI v3 schema before it is sent.

Why this exists. Measured 2026-11-14 (`papers/v4-operator-r1/A3-LOOP-INVESTIGATION.md` §2d): an unattended session
re-typed a Job manifest with `- echo 'ERROR: nightly export failed'; exit 1` unquoted. YAML parses the `: ` as a
mapping, so `command[2]` was an object, and the cluster answered client-side `kubectl apply` — even with
`--dry-run=server` and `--validate=strict` — with "The request is invalid: … unrecognized type: string", which names
no field. The session resent it 73 times. A type check of the parsed manifest against the cluster's schema names the
field, the cause and the fix before anything is sent: `spec.template.spec.containers[0].command[2] is a mapping …`.

What it checks: every field's JSON type against the schema (strings, integers, numbers, booleans, objects, arrays,
`oneOf` alternatives such as Quantity and IntOrString), and field names against the schema's properties (a misspelt
field), with the closest valid name. Maps (`additionalProperties`) are checked value by value.

What it does not do: decide that a manifest is valid. It only reports what it can prove wrong. Anything it cannot
check — no schema for the kind (a CRD it cannot find), the schema endpoint unreachable, an unparsable document —
yields no findings, and the server's own validation still runs. It never turns "could not check" into "valid".

The schema comes from the cluster (`kubectl get --raw /openapi/v3/<group-version>`), so it matches the server's
version; it is cached per kubeconfig and group-version. YAML is read with PyYAML's YAML 1.1 rules, which is how
kubectl reads it too (`yes`/`no` are booleans, an unquoted `8080` is an integer); a YAML timestamp is treated as the
string it was written as, because kubectl keeps it as text.
"""

from __future__ import annotations

import datetime as _dt
import difflib
import json
import os
import subprocess
import threading
import time

import yaml

_MAX_FINDINGS = 8
_FAILED_FETCH_TTL_SECONDS = 300.0

_cache: dict[tuple[str, str], tuple[float, dict | None]] = {}
_lock = threading.Lock()


def clear_cache() -> None:
    """Test helper."""
    with _lock:
        _cache.clear()


def _gv_path(api_version: str) -> str:
    return f"api/{api_version}" if "/" not in api_version else f"apis/{api_version}"


def _fetch_openapi(gv_path: str, kubeconfig: str) -> dict | None:
    """The cluster's OpenAPI v3 document for one group-version, or None when it cannot be read."""
    try:
        proc = subprocess.run(
            ["kubectl", "get", "--raw", f"/openapi/v3/{gv_path}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            shell=False,
            env={**os.environ, "KUBECONFIG": kubeconfig},
        )
        if proc.returncode != 0:
            return None
        doc = json.loads(proc.stdout)
        return doc if isinstance(doc, dict) and "components" in doc else None
    except Exception:  # noqa: BLE001 - "could not check" is never an error the write should fail on
        return None


def _schema_doc(api_version: str, kubeconfig: str) -> dict | None:
    key = (kubeconfig, _gv_path(api_version))
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and (
            hit[1] is not None or now - hit[0] < _FAILED_FETCH_TTL_SECONDS
        ):
            return hit[1]
    doc = _fetch_openapi(key[1], kubeconfig)
    with _lock:
        _cache[key] = (now, doc)
    return doc


def _kind_schema(doc: dict, api_version: str, kind: str) -> dict | None:
    group, _, version = api_version.rpartition("/")
    for schema in (doc.get("components") or {}).get("schemas", {}).values():
        for gvk in schema.get("x-kubernetes-group-version-kind") or []:
            if (
                gvk.get("group", "") == group
                and gvk.get("version") == version
                and gvk.get("kind") == kind
            ):
                return schema
    return None


def _resolve(schema: dict, schemas: dict, depth: int = 0) -> dict:
    """Follow `$ref` and single-element `allOf` wrappers to the schema they stand for."""
    while depth < 20:
        if "$ref" in schema:
            schema = schemas.get(schema["$ref"].rsplit("/", 1)[-1], {})
        elif (
            "allOf" in schema
            and len(schema["allOf"]) == 1
            and "type" not in schema
            and "properties" not in schema
        ):
            schema = schema["allOf"][0]
        else:
            return schema
        depth += 1
    return schema


def _type_name(value: object) -> str:
    if isinstance(value, dict):
        return "a mapping"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int):
        return "an integer"
    if isinstance(value, float):
        return "a number"
    return "a string"


def _cause(value: object, want: str) -> str:
    """The likely YAML reason a value has the wrong type, when there is a well-known one."""
    if want == "string":
        if isinstance(value, dict) and len(value) == 1:
            return "a YAML value containing ': ' is read as a mapping — quote the whole value"
        if isinstance(value, bool):
            return "YAML reads yes/no/on/off/true/false as a boolean — quote it"
        if isinstance(value, (int, float)):
            return "YAML reads an unquoted number as a number — quote it"
    return f"the schema wants a value of type {want}"


def _matches(value: object, typ: str) -> bool:
    if typ == "string":
        return isinstance(value, (str, _dt.date, _dt.datetime))
    if typ == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if typ == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if typ == "boolean":
        return isinstance(value, bool)
    if typ == "object":
        return isinstance(value, dict)
    if typ == "array":
        return isinstance(value, list)
    return True


def _walk(
    value: object, schema: dict, schemas: dict, path: str, out: list[str]
) -> None:
    if len(out) >= _MAX_FINDINGS or value is None:
        return
    schema = _resolve(schema, schemas)
    if schema.get("x-kubernetes-preserve-unknown-fields"):
        return
    alternatives = schema.get("oneOf") or schema.get("anyOf")
    if alternatives:
        resolved = [_resolve(a, schemas) for a in alternatives]
        raw_types = [a.get("type") for a in resolved]
        # Only judge when every alternative names a type; an alternative without one could accept anything.
        if not all(isinstance(t, str) for t in raw_types):
            return
        types = [str(t) for t in raw_types]
        if not any(_matches(value, t) for t in types):
            out.append(
                f"{path or '<root>'} is {_type_name(value)}; the schema wants one of {', '.join(types)}"
            )
        return
    typ = schema.get("type") or ("object" if "properties" in schema else None)
    if typ is None:
        return
    if not _matches(value, typ):
        want = {
            "string": "a string",
            "integer": "an integer",
            "number": "a number",
            "boolean": "a boolean",
            "object": "a mapping",
            "array": "a list",
        }.get(typ, typ)
        out.append(
            f"{path or '<root>'} is {_type_name(value)}, but the schema wants {want}: {_cause(value, typ)}"
            f" (value: {json.dumps(value, default=str)[:80]})"
        )
        return
    if typ == "array" and isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(value):
                _walk(item, items, schemas, f"{path}[{i}]", out)
    elif typ == "object" and isinstance(value, dict):
        props = schema.get("properties")
        extra = schema.get("additionalProperties")
        for key, sub in value.items():
            child = f"{path}.{key}" if path else str(key)
            if props and key in props:
                _walk(sub, props[key], schemas, child, out)
            elif isinstance(extra, dict):
                _walk(sub, extra, schemas, child, out)
            elif props and extra is not True:
                near = difflib.get_close_matches(str(key), list(props), n=1)
                out.append(
                    f"{child} is not a field of this object"
                    + (f" — did you mean '{near[0]}'?" if near else "")
                )
            if len(out) >= _MAX_FINDINGS:
                return


def lint(stdin: str, kubeconfig: str) -> list[str]:
    """Findings for every document in `stdin` that can be checked; [] when nothing could be proved wrong."""
    try:
        docs = [d for d in yaml.safe_load_all(stdin) if isinstance(d, dict)]
    except yaml.YAMLError:
        return []  # the YAML pre-validation in run_kubectl reports unparsable input
    findings: list[str] = []
    for i, doc in enumerate(docs):
        api_version, kind = doc.get("apiVersion"), doc.get("kind")
        if not (isinstance(api_version, str) and isinstance(kind, str)):
            continue
        schema_doc = _schema_doc(api_version, kubeconfig)
        if schema_doc is None:
            continue
        kind_schema = _kind_schema(schema_doc, api_version, kind)
        if kind_schema is None:
            continue
        found: list[str] = []
        _walk(doc, kind_schema, schema_doc["components"]["schemas"], "", found)
        prefix = f"document {i + 1} ({kind}): " if len(docs) > 1 else ""
        findings.extend(prefix + f for f in found)
        if len(findings) >= _MAX_FINDINGS:
            break
    return findings[:_MAX_FINDINGS]
