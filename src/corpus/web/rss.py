"""RSS feed routes: preview, add, ingest.

`preview` is a genuine safety step, not ceremony. `feedparser` returns zero entries
on a typo'd or dead URL rather than raising, so without a preview an operator would
add a broken feed, see a run report "0 fetched", and have no way to tell that from a
feed that simply has no new items. Previewing first turns a silent failure into a
visible one.

Adding writes to `seeds/rss_feeds.yaml`, never straight to `source` — see
`corpus.web.rss_seeds` for why. Ingestion then reuses `ingest_source` unchanged
(`corpus.ingest.rss_runner`).
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from corpus.ingest.pipelines import IngestEvent
from corpus.ingest.rss_runner import run_rss_ingestion
from corpus.web.rss_seeds import RssSeedError, append_feed, load_feeds, preview_feed

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/rss", tags=["rss"])

RunStatus = Literal["running", "done", "failed"]
_DONE = object()


@dataclass
class RssRun:
    id: str
    status: RunStatus = "running"
    error: str | None = None
    fetched: int = 0
    skipped: int = 0
    failed: int = 0
    _queue: queue.Queue = field(default_factory=queue.Queue)


class RssRunManager:
    """Mirrors `corpus.web.runs.RunManager` for the same reason
    `EnrichmentRunManager` does — RSS runs have no credit accounting, and the
    ingestion manager is load-bearing for the working YouTube flow."""

    def __init__(self) -> None:
        self._runs: dict[str, RssRun] = {}
        self._lock = threading.Lock()

    def start(self, *, feed_url: str, title: str, domain: str, tier: str, limit: int | None) -> str:
        run_id = str(uuid.uuid4())
        run = RssRun(id=run_id)
        with self._lock:
            self._runs[run_id] = run
        threading.Thread(
            target=self._execute,
            args=(run, feed_url, title, domain, tier, limit),
            name=f"rss-run-{run_id[:8]}",
            daemon=True,
        ).start()
        return run_id

    def get(self, run_id: str) -> RssRun | None:
        with self._lock:
            return self._runs.get(run_id)

    def _execute(self, run, feed_url, title, domain, tier, limit) -> None:
        def emit(event: IngestEvent) -> None:
            run._queue.put(event)

        try:
            result = run_rss_ingestion(
                feed_url=feed_url,
                title=title,
                domain=domain,
                authority_tier=tier,
                limit=limit,
                on_event=emit,
            )
            run.fetched, run.skipped, run.failed = result.fetched, result.skipped, result.failed
            run.status = "done"
        except Exception as exc:  # noqa: BLE001 - surfaced to the dashboard, not swallowed
            log.exception("rss_run_failed", run_id=run.id, feed_url=feed_url)
            run.status = "failed"
            run.error = str(exc)
        finally:
            run._queue.put(_DONE)

    async def stream(self, run_id: str) -> AsyncIterator[dict]:
        run = self.get(run_id)
        if run is None:
            yield {"data": json.dumps({"kind": "error", "detail": f"unknown run {run_id}"})}
            return
        while True:
            item = await asyncio.to_thread(run._queue.get)
            if item is _DONE:
                yield {
                    "data": json.dumps(
                        {
                            "kind": "run_complete",
                            "status": run.status,
                            "error": run.error,
                            "fetched": run.fetched,
                            "skipped": run.skipped,
                            "failed": run.failed,
                        }
                    )
                }
                return
            yield {"data": json.dumps(asdict(item))}


manager = RssRunManager()


@router.get("/feeds")
def list_feeds() -> list[dict[str, Any]]:
    return load_feeds()


class PreviewRequest(BaseModel):
    url: str


@router.post("/preview")
def preview(body: PreviewRequest) -> dict[str, Any]:
    try:
        return asdict(preview_feed(body.url))
    except RssSeedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class AddRequest(BaseModel):
    url: str
    domain: str = "unknown"
    authority_tier: str = "unknown"
    limit: int | None = None


@router.post("/add")
def add_feed(body: AddRequest) -> dict[str, Any]:
    """Preview again server-side before writing — the client's preview is advisory,
    and re-parsing is cheap next to adding a dead feed to a reviewed file."""
    try:
        seen = preview_feed(body.url)
        return append_feed(seen, domain=body.domain, authority_tier=body.authority_tier)
    except RssSeedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/start")
def start_run(body: AddRequest) -> dict[str, str]:
    try:
        seen = preview_feed(body.url)
    except RssSeedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    run_id = manager.start(
        feed_url=seen.url,
        title=seen.title,
        domain=body.domain,
        tier=body.authority_tier,
        limit=body.limit,
    )
    return {"run_id": run_id}


@router.get("/stream/{run_id}")
async def stream_run(run_id: str) -> EventSourceResponse:
    return EventSourceResponse(manager.stream(run_id))
