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


# ── Stream health ─────────────────────────────────────────────────────────────
# "The sensorium is active" used to mean only "a DetectorEngine object exists".
# It said nothing about whether anything was being watched. Measured 2026-08-20:
# with kubectl absent, both watch tasks hit FileNotFoundError and **return
# permanently**, and `GET /v1/findings` still answered
# {"sensorium": "active", "detectors": 20, "findings": []} — so `kq findings`
# printed the green line "No findings · 20 detectors watching" while nothing was
# watching at all. An RBAC denial produces the same silence: kubectl exits
# non-zero, the loop retries every 60s forever, and (with stderr sent to
# DEVNULL) the reason was discarded.
#
# This registry is the difference between "quiet" and "deaf".
class StreamHealth:
    """Live state of one `kubectl --watch` stream."""

    __slots__ = ("name", "connected", "connected_at", "last_error",
                 "closed_at", "consecutive_failures", "stopped")

    def __init__(self, name: str) -> None:
        self.name = name
        self.connected = False
        self.connected_at: float | None = None
        self.last_error: str | None = None
        self.closed_at: float | None = None
        self.consecutive_failures = 0
        self.stopped = False          # gave up permanently — will never reconnect

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "connected": self.connected,
            "connected_at": self.connected_at,
            "stopped": self.stopped,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
        }


_streams: dict[str, StreamHealth] = {}


def stream_health() -> list[dict]:
    """Health of every watch stream. Empty when the sensorium was never started."""
    return [s.as_dict() for s in _streams.values()]


def any_stream_connected() -> bool:
    return any(s.connected for s in _streams.values())


def reset_stream_health() -> None:
    _streams.clear()

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
            # The apiserver's own identity for this exact object version. Carried so a
            # graph edge derived from this observation can cite what it was derived FROM
            # (kg.observation_ref). Observations are an in-memory stream — there is no
            # observations table — so a synthetic observation id would point at nothing;
            # uid + resourceVersion is a handle that can be checked against the cluster.
            "uid": meta.get("uid", ""),
            "resource_version": meta.get("resourceVersion", ""),
        },
    )


# Reasons the staleness filter could not run, warned once each per watch epoch. Without this
# the filter fails open in silence: an event whose age is unknown skips the check entirely, and
# skipping the check is exactly the condition the check exists to catch.
_staleness_unchecked: set[str] = set()


def _warn_staleness_unchecked(reason: str) -> None:
    if reason in _staleness_unchecked:
        return
    _staleness_unchecked.add(reason)
    logger.warning(
        "k8s_watcher: %s — the event-staleness filter cannot run for these events, so replayed "
        "history may fire detectors as if it were happening now. Further occurrences of this "
        "reason are not repeated until the next (re)connect.",
        reason,
    )


def _event_timestamp(obj: dict) -> float | None:
    """Best-effort parse of an Event's last activity time (unix seconds).

    `None` means "we do not know how old this is", which the caller must not treat as "fresh"
    without saying so — see `_event_observation`.
    """
    raw = obj.get("lastTimestamp") or obj.get("eventTime") or obj.get(
        "metadata", {}
    ).get("creationTimestamp")
    if not raw:
        _warn_staleness_unchecked("an Event carried no lastTimestamp, eventTime or creationTimestamp")
        return None
    try:
        from datetime import UTC, datetime
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        _warn_staleness_unchecked(f"an Event timestamp could not be parsed (e.g. {str(raw)[:40]!r})")
        return None
    if parsed.tzinfo is None:
        # Kubernetes emits RFC3339 in UTC. `.timestamp()` on a naive datetime interprets it as
        # LOCAL time, which shifts the age by the host's UTC offset — two hours on this machine,
        # measured 2026-08-24. West of UTC that makes replayed history look current; east of it,
        # a current event looks stale and is dropped, which is a missed detection.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _event_observation(doc: dict, cluster_id: str) -> Observation | None:
    obj = doc.get("object", doc)
    if obj.get("kind") != "Event":
        return None
    # The watch replays recent event history on (re)connect — without this
    # filter, bootstrap-era warnings fire detectors minutes after the fact.
    # Fail OPEN on an unknown age: dropping an event we merely cannot date would turn a
    # timestamp quirk into a missed incident, which is the worse error for a watchtower. But
    # `_event_timestamp` says so out loud, so "the filter is off" is never silent.
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
    import time

    name = " ".join(args[:3])
    health = _streams.setdefault(name, StreamHealth(name))
    backoff = _BACKOFF_INITIAL
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "kubectl",
                *args,
                stdout=asyncio.subprocess.PIPE,
                # Captured, not discarded: an RBAC denial or an expired credential
                # is the whole explanation for why no findings ever appear.
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info(f"sensorium_watch_started args={name}")
            health.connected = True
            health.connected_at = time.time()
            health.last_error = None
            # Drained concurrently: reading stdout while stderr fills its pipe
            # buffer would block the child.
            errbuf: list[str] = []
            err_task = asyncio.create_task(_drain_stderr(proc, errbuf))
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
                health.consecutive_failures = 0
            await proc.wait()
            err_task.cancel()
            health.connected = False
            health.closed_at = time.time()
            reason = "".join(errbuf).strip().splitlines()
            health.last_error = (reason[-1][:300] if reason
                                 else f"stream closed (rc={proc.returncode})")
            health.consecutive_failures += 1
            logger.warning(
                f"sensorium_watch_closed args={name} rc={proc.returncode} "
                f"reason={health.last_error!r}")
        except asyncio.CancelledError:
            health.connected = False
            raise
        except FileNotFoundError:
            # Permanent: this loop returns, so the stream never reconnects. It
            # must not keep reading as "active" for the rest of the process life.
            health.connected = False
            health.stopped = True
            health.last_error = "kubectl not found on the server"
            logger.warning("sensorium: kubectl not found — watcher disabled")
            return
        except Exception as exc:
            health.connected = False
            health.consecutive_failures += 1
            health.last_error = f"{type(exc).__name__}: {exc}"[:300]
            logger.warning(f"sensorium_watch_error: {exc}")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, _BACKOFF_MAX)


async def _drain_stderr(proc, buf: list[str]) -> None:
    """Keep stderr empty so the child never blocks; retain the tail for the reason."""
    if proc.stderr is None:
        return
    try:
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            buf.append(chunk.decode(errors="replace"))
            del buf[:-8]           # bounded — only the tail is ever useful
    except asyncio.CancelledError:
        return
    except Exception:
        return


# ── Backpressure ──────────────────────────────────────────────────────────────────────────────
# `_watch_loop` used to call `sink(obs)` inline for every observation, which means the ONLY thing
# limiting how fast the detector engine and the memory writer are driven was the event loop. At a
# few hundred pods that is fine. At the pod count implied by a data centre of thousands of nodes,
# `get pods -A --watch` is a firehose — and every relist on reconnect replays the entire cluster —
# so an inline sink turns a burst into unbounded memory growth inside a container with a 1 GiB
# limit. The pod is OOMKilled, restarts, relists the whole cluster again, and the loop repeats.
#
# A bounded queue does NOT make the firehose smaller. What it does is convert an invisible OOM
# into a counted, visible loss: past the limit the OLDEST observation is dropped and `shed_total`
# increments. Oldest-first is deliberate — a pod's status is a LEVEL, not an edge, so the newest
# observation of a pod supersedes the stale one, and shedding the newest would keep the queue full
# of history while discarding the current state of the cluster.
#
# This is the cheap half of the fix. The real answer is a shared informer with server-side field
# selection instead of a `kubectl` subprocess; see design/enterprise-readiness.md (A5).
_shed_total = 0
_queue_high_water = 0


def queue_stats() -> dict:
    """Observation-queue state. `shed_total > 0` means the sensorium is dropping perception."""
    return {"shed_total": _shed_total, "high_water": _queue_high_water}


def reset_queue_stats() -> None:
    global _shed_total, _queue_high_water
    _shed_total = _queue_high_water = 0


def _enqueue(queue: "asyncio.Queue", obs: Observation) -> None:
    """Non-blocking put that sheds the oldest rather than blocking the watch stream.

    Blocking here would apply backpressure to `kubectl` itself: the pipe fills, the child stops
    writing, and the API server eventually closes the watch — which surfaces as a reconnect storm
    and a full relist, i.e. MORE load exactly when the system is already behind.
    """
    global _shed_total, _queue_high_water
    while True:
        try:
            queue.put_nowait(obs)
            _queue_high_water = max(_queue_high_water, queue.qsize())
            return
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                _shed_total += 1
                if _shed_total == 1 or _shed_total % 1000 == 0:
                    logger.warning(
                        f"sensorium: observation queue full — shed {_shed_total} observation(s). "
                        f"Detection is now lossy; see queue_stats() and design A5."
                    )
            except asyncio.QueueEmpty:  # pragma: no cover — drained concurrently
                pass


async def _drain_queue(queue: "asyncio.Queue", sink: Callable[[Observation], object]) -> None:
    """Single consumer: the engine and memory writer are driven from here, not from the stream."""
    while True:
        obs = await queue.get()
        try:
            sink(obs)
        except Exception as exc:
            # One bad observation must never kill perception for the whole process.
            logger.warning(f"sensorium: sink failed for one observation: {exc}")
        finally:
            queue.task_done()


async def start_watchers(
    cluster_id: str, sink: Callable[[Observation], object]
) -> list[asyncio.Task]:
    """Start pod + event watch tasks feeding *sink*, through a bounded queue.

    The returned list includes the consumer task, so the caller's existing shutdown path cancels
    it along with the watchers and nothing is left draining a queue nobody feeds.
    """
    global _watch_epoch
    import time
    _watch_epoch = time.time()
    # A reconnect is a new replay window, so the once-per-reason warnings arm again.
    _staleness_unchecked.clear()
    _streams.clear()
    reset_queue_stats()
    loop = asyncio.get_running_loop()

    from app.core.config import settings

    queue: asyncio.Queue = asyncio.Queue(maxsize=settings.SENSORIUM_QUEUE_MAXSIZE)

    def queued_sink(obs: Observation) -> None:
        _enqueue(queue, obs)

    tasks = [
        loop.create_task(_watch_loop(args, normalise, cluster_id, queued_sink))
        for args, normalise in _WATCHES
    ]
    tasks.append(loop.create_task(_drain_queue(queue, sink)))
    return tasks
