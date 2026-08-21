"""K8s watcher — streams pod/event changes via `kubectl --watch` subprocesses.

Uses kubectl rather than a Kubernetes client library deliberately: the V2
deployment already ships kubectl in the image with watch RBAC, the tool layer
is kubectl-based, and subprocess streaming avoids a new heavyweight
dependency. kubectl emits a stream of concatenated JSON documents; we parse
incrementally with JSONDecoder.raw_decode.

Each watcher reconnects with exponential backoff (watches drop when the
API server closes the connection or resourceVersion expires).
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from app.sensorium.observations import Observation, pod_display_status
from app.utils.logger import get_logger

logger = get_logger(__name__)

_BACKOFF_INITIAL = 2.0
_BACKOFF_MAX = 60.0
_READ_CHUNK = 65536

# Events older than watcher start (minus grace) are history replays, not news.
_EVENT_STALENESS_GRACE = 30.0
_watch_epoch = 0.0


class _JsonStream:
    """Incremental parser for a stream of concatenated JSON documents."""

    def __init__(self) -> None:
        self._buffer = ""
        self._decoder = json.JSONDecoder()

    def feed(self, chunk: str) -> list[dict]:
        self._buffer += chunk
        docs: list[dict] = []
        while True:
            stripped = self._buffer.lstrip()
            if not stripped:
                self._buffer = ""
                break
            try:
                doc, end = self._decoder.raw_decode(stripped)
            except json.JSONDecodeError:
                self._buffer = stripped
                break  # incomplete document — wait for more bytes
            docs.append(doc)
            self._buffer = stripped[end:]
        return docs


def _pod_observation(doc: dict, cluster_id: str) -> Observation | None:
    obj = doc.get("object", doc)  # --output-watch-events wraps in {type, object}
    if obj.get("kind") != "Pod":
        return None
    meta = obj.get("metadata", {})
    owner = ""
    for ref in meta.get("ownerReferences") or []:
        if ref.get("controller"):
            owner = f"{ref.get('kind', '')}/{ref.get('name', '')}"
            break
    return Observation(
        kind="pod_status",
        cluster_id=cluster_id,
        namespace=meta.get("namespace", ""),
        name=meta.get("name", ""),
        fields={
            "status": pod_display_status(obj),
            "watch_type": doc.get("type", ""),
            "node": obj.get("spec", {}).get("nodeName", ""),
            "owner": owner,
        },
    )


def _event_timestamp(obj: dict) -> float | None:
    """Best-effort parse of an Event's last activity time (unix seconds)."""
    raw = obj.get("lastTimestamp") or obj.get("eventTime") or obj.get(
        "metadata", {}
    ).get("creationTimestamp")
    if not raw:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(raw)).timestamp()
    except ValueError:
        return None


def _event_observation(doc: dict, cluster_id: str) -> Observation | None:
    obj = doc.get("object", doc)
    if obj.get("kind") != "Event":
        return None
    # The watch replays recent event history on (re)connect — without this
    # filter, bootstrap-era warnings fire detectors minutes after the fact.
    ts = _event_timestamp(obj)
    if ts is not None and ts < _watch_epoch - _EVENT_STALENESS_GRACE:
        return None
    involved = obj.get("involvedObject", {})
    return Observation(
        kind="event",
        cluster_id=cluster_id,
        namespace=involved.get("namespace", obj.get("metadata", {}).get("namespace", "")),
        name=involved.get("name", ""),
        fields={
            "reason": obj.get("reason", ""),
            "message": obj.get("message", ""),
            "involved_kind": involved.get("kind", ""),
            "event_type": obj.get("type", ""),
        },
    )


_WATCHES: tuple[tuple[list[str], Callable[[dict, str], Observation | None]], ...] = (
    (
        ["get", "pods", "-A", "--watch", "--output-watch-events=true", "-o", "json"],
        _pod_observation,
    ),
    (
        ["get", "events", "-A", "--watch", "--output-watch-events=true", "-o", "json"],
        _event_observation,
    ),
)


async def _watch_loop(
    args: list[str],
    normalise: Callable[[dict, str], Observation | None],
    cluster_id: str,
    sink: Callable[[Observation], object],
) -> None:
    backoff = _BACKOFF_INITIAL
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "kubectl",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            logger.info(f"sensorium_watch_started args={' '.join(args[:3])}")
            stream = _JsonStream()
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(_READ_CHUNK)
                if not chunk:
                    break
                for doc in stream.feed(chunk.decode(errors="replace")):
                    obs = normalise(doc, cluster_id)
                    if obs is not None:
                        sink(obs)
                backoff = _BACKOFF_INITIAL  # data flowing — reset backoff
            await proc.wait()
            logger.warning(f"sensorium_watch_closed args={' '.join(args[:3])} rc={proc.returncode}")
        except asyncio.CancelledError:
            raise
        except FileNotFoundError:
            logger.warning("sensorium: kubectl not found — watcher disabled")
            return
        except Exception as exc:
            logger.warning(f"sensorium_watch_error: {exc}")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, _BACKOFF_MAX)


async def start_watchers(
    cluster_id: str, sink: Callable[[Observation], object]
) -> list[asyncio.Task]:
    """Start pod + event watch tasks feeding *sink*. Returns the tasks."""
    global _watch_epoch
    import time
    _watch_epoch = time.time()
    loop = asyncio.get_running_loop()
    return [
        loop.create_task(_watch_loop(args, normalise, cluster_id, sink))
        for args, normalise in _WATCHES
    ]
