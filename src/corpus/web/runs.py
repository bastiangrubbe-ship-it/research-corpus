"""Run manager: executes ingestion in a background thread, bridges its events onto
a queue an async SSE endpoint can drain.

Ingestion is synchronous (blocking network calls, a synchronous SQLAlchemy session)
and FastAPI's request handlers are async, so a run cannot simply happen inline in a
request. A background thread per run plus a thread-safe queue is the simplest correct
bridge — `queue.Queue.get` blocks the calling thread, so the async side wraps it in
`asyncio.to_thread` rather than polling.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from typing import Literal

import structlog

from corpus.ingest.pipelines import IngestEvent
from corpus.ingest.runner import load_seeds, run_ingestion

log = structlog.get_logger(__name__)

RunStatus = Literal["running", "done", "failed"]

# A sentinel rather than None: an event stream that legitimately emits no events
# before completion must still be distinguishable from "the queue itself is closed."
_DONE = object()


@dataclass
class Run:
    id: str
    status: RunStatus = "running"
    error: str | None = None
    credits_spent: int = 0
    credits_budget: int = 0
    _queue: queue.Queue = field(default_factory=queue.Queue)


class RunManager:
    """Holds at most a handful of runs in memory. This is a single-operator local
    dashboard, not a multi-tenant job queue — runs are not persisted across a
    process restart, which is the correct tradeoff for what this actually is.
    """

    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}
        self._lock = threading.Lock()

    def start(self, *, phase: str | None, handle: str | None, limit: int | None) -> str:
        seeds = load_seeds(phase, handle)
        if not seeds:
            raise ValueError(f"no seed rows for phase={phase!r} handle={handle!r}")

        run_id = str(uuid.uuid4())
        run = Run(id=run_id)
        with self._lock:
            self._runs[run_id] = run

        thread = threading.Thread(
            target=self._execute,
            args=(run, seeds, limit),
            name=f"ingest-run-{run_id[:8]}",
            daemon=True,
        )
        thread.start()
        return run_id

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(run_id)

    def _execute(self, run: Run, seeds: list[dict], limit: int | None) -> None:
        def emit(event: IngestEvent) -> None:
            run._queue.put(event)

        try:
            result = run_ingestion(seeds, limit=limit, on_event=emit)
            run.credits_spent = result.credits_spent
            run.credits_budget = result.credits_budget
            run.status = "done"
        except Exception as exc:  # noqa: BLE001 - surfaced to the dashboard, not swallowed
            log.exception("run_failed", run_id=run.id)
            run.status = "failed"
            run.error = str(exc)
        finally:
            run._queue.put(_DONE)

    async def stream(self, run_id: str) -> AsyncIterator[dict]:
        """Async generator of SSE envelopes. `sse-starlette` matches each yielded
        dict's keys against `ServerSentEvent`'s own constructor (`data`, `event`,
        `id`...) — the actual progress payload has to be nested under `data` as a
        JSON string, not yielded as a bare dict of its own fields.
        """
        run = self.get(run_id)
        if run is None:
            yield {"data": json.dumps({"kind": "error", "detail": f"unknown run {run_id}"})}
            return

        while True:
            item = await asyncio.to_thread(run._queue.get)
            if item is _DONE:
                payload = {
                    "kind": "run_complete",
                    "status": run.status,
                    "error": run.error,
                    "credits_spent": run.credits_spent,
                    "credits_budget": run.credits_budget,
                }
                yield {"data": json.dumps(payload)}
                return
            yield {"data": json.dumps(asdict(item))}


# One instance for the process — runs are an in-memory concept scoped to this
# server's lifetime, not something that needs a database row.
manager = RunManager()
