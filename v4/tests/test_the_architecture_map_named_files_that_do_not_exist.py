"""The code map on the architecture page pointed at modules that were not there.

`docs/architecture.md` carries the map a contributor or integrator reads to find their way around
the server package, plus a request-flow diagram. Both had drifted. Measured 2026-08-20:

    code map, 34 `.py` entries          3 named a module that exists nowhere under the package
      endpoints/stream.py               — no such file; nothing serves that path
      endpoints/memory.py               — no such file; pinned context is `preferences.py`
      db/memory.py                      — no such file; the store lives under `app/memory/`

    endpoint annotations                1 named a route the server does not expose
      GET /v1/chat/stream/{session_id}  — cited twice (diagram + map), 404 on a real server

The truth is that `POST /v1/chat/completions` **is** the stream — it returns a
`StreamingResponse` with `media_type="text/event-stream"` — and the separate SSE route is
`GET /v1/events/replay/{session_id}`, served by `events.py`. An integrator following the page
would have opened a connection to a path that does not exist and read it as a server problem.

Nothing here is a security defect and nothing was broken in the code. It is the same class as
pass 105 one page over: a document confidently describing a structure that is not there, on the
page whose whole purpose is to be trusted instead of the source.

This file is the gate. It is deliberately written against *whatever the page says today* rather
than against a fixed list, so it keeps working as the tree changes: rename a module and the map
must follow, add a route and the docs may cite it, cite one that does not exist and this fails.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_DOCS = _ROOT / "docs"
_SERVER = _ROOT / "packages" / "kubeintellect-server"
_ARCH = _DOCS / "architecture.md"

#: Floors for the non-vacuity checks. A regex that stops matching must fail, not pass silently —
#: the lesson of pass 102, where an assertion held because the set it compared was empty.
_MIN_MAP_MODULES = 25
_MIN_DOC_PAGES = 20
_MIN_DOC_PATHS = 10


def _real_paths() -> set[str]:
    from app.main import app
    return {_norm(p) for p in app.openapi()["paths"]}


def _real_methods() -> dict[str, set[str]]:
    from app.main import app
    spec = app.openapi()["paths"]
    return {_norm(p): {m.upper() for m in spec[p]} for p in spec}


def _norm(path: str) -> str:
    """Parameter *names* are the doc's business; the shape is the contract."""
    return re.sub(r"\{[^}]*\}", "{}", path.rstrip("/"))


def _code_map() -> str:
    blocks = re.findall(r"```[a-z]*\n(.*?)```", _ARCH.read_text(), re.S)
    maps = [b for b in blocks if "core/" in b and "config.py" in b]
    assert len(maps) == 1, f"expected exactly one code map in architecture.md, found {len(maps)}"
    return maps[0]


def _map_modules() -> list[tuple[str, str]]:
    out = []
    for line in _code_map().splitlines():
        m = re.search(r"[├└]──\s+([A-Za-z0-9_./-]+\.py)", line)
        if m:
            out.append((m.group(1), line.strip()))
    return out


def _doc_pages() -> list[Path]:
    return sorted(_DOCS.rglob("*.md"))


def _cited_paths() -> dict[str, set[str]]:
    """Every `/v1/...` path mentioned anywhere in the docs → the pages that mention it."""
    cited: dict[str, set[str]] = {}
    for page in _doc_pages():
        for m in re.finditer(r"(?<![\w/])/v1/[A-Za-z0-9_\-{}/]+", page.read_text()):
            cited.setdefault(_norm(m.group(0).rstrip(".,;:)`")), set()).add(page.name)
    return cited


class TestEveryModuleTheMapNamesExists:

    def test_the_map_was_actually_found_and_parsed(self):
        """Non-vacuity: if the extraction breaks, every check below would pass on nothing."""
        modules = _map_modules()
        assert len(modules) >= _MIN_MAP_MODULES, (
            f"only {len(modules)} modules parsed out of the code map — the map moved or the "
            "line format changed, and this gate is no longer reading it"
        )

    def test_no_entry_names_a_module_that_does_not_exist(self):
        missing = [
            line for name, line in _map_modules()
            if not list(_SERVER.rglob(name))
        ]
        assert missing == [], (
            "docs/architecture.md maps modules that do not exist:\n  " + "\n  ".join(missing)
        )

    @pytest.mark.parametrize("name,expected", [
        ("events.py", "the SSE replay endpoint that `stream.py` was supposed to be"),
        ("preferences.py", "the pinned-context CRUD that `memory.py` was supposed to be"),
        ("flight_recorder.py", "a headline V4 subsystem the db/ listing omitted"),
    ])
    def test_the_replacements_are_named(self, name, expected):
        assert name in _code_map(), f"the map no longer names {name} — {expected}"

    @pytest.mark.parametrize("phantom", ["stream.py", "db/memory.py"])
    def test_the_phantoms_stay_gone(self, phantom):
        assert phantom not in _code_map()


class TestEveryRouteTheDocsCiteIsReal:

    def test_the_scan_actually_reads_the_docs(self):
        """Non-vacuity again: a bad glob would make the whole class trivially green."""
        pages, cited = _doc_pages(), _cited_paths()
        assert len(pages) >= _MIN_DOC_PAGES, f"only {len(pages)} doc pages found"
        assert len(cited) >= _MIN_DOC_PATHS, f"only {len(cited)} /v1 paths cited across the docs"

    def test_no_page_cites_a_path_the_server_does_not_serve(self):
        real = _real_paths()
        assert real, "no routes collected from the app"
        bad = {p: sorted(pages) for p, pages in _cited_paths().items() if p not in real}
        assert bad == {}, (
            "documented API paths that do not exist:\n  "
            + "\n  ".join(f"{p} — cited in {pages}" for p, pages in sorted(bad.items()))
        )

    def test_no_page_cites_the_removed_stream_route(self):
        """The specific regression: it appeared twice, in a diagram and in the map."""
        for page in _doc_pages():
            assert "chat/stream" not in page.read_text(), f"{page.name} still cites it"

    def test_every_annotated_method_matches_the_real_route(self):
        """`postmortem.py — GET /v1/episodes/{id}/postmortem` claims a verb as well as a path."""
        methods = _real_methods()
        wrong = []
        for verb_group, path in re.findall(
            r"((?:GET|POST|PUT|DELETE)(?:/(?:GET|POST|PUT|DELETE))*)\s+(/[A-Za-z0-9_{}/-]+)",
            _ARCH.read_text(),
        ):
            key = _norm(path)
            if key not in methods:
                continue                      # covered by the path test above
            for verb in verb_group.split("/"):
                if verb not in methods[key]:
                    wrong.append(f"{verb} {path} — real methods {sorted(methods[key])}")
        assert wrong == [], "architecture.md annotates methods the route does not accept:\n  " \
                            + "\n  ".join(wrong)


class TestTheDiagramDescribesHowStreamingActuallyWorks:

    def test_the_page_says_the_post_itself_streams(self):
        text = _ARCH.read_text()
        assert "text/event-stream" in text, (
            "the page should say how the stream is delivered, since the separate GET it used to "
            "name never existed"
        )

    def test_the_chat_endpoint_really_returns_an_event_stream(self):
        """The doc claim, checked against the code rather than against another doc."""
        src = (_SERVER / "app" / "api" / "v1" / "endpoints" / "chat_completions.py").read_text()
        assert 'media_type="text/event-stream"' in src
        assert "StreamingResponse" in src

    def test_the_replay_route_really_is_the_sse_one(self):
        src = (_SERVER / "app" / "api" / "v1" / "endpoints" / "events.py").read_text()
        assert '@router.get("/events/replay/{session_id}")' in src
        assert 'media_type="text/event-stream"' in src
