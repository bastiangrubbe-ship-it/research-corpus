"""Enrichment triggers: entity extraction, speaker attribution, transcript restoration.

Three operations that all mutate the corpus, grouped because they share a shape —
"run a maintenance pass that the CLI also runs" — not because they're one feature.
Only entity extraction streams: `enrich_documents_concurrent` yields per document, so
there is real progress to report. `attribute_speakers` is one blocking call with no
per-item callback, and restoration acts on a single document, so both simply return
their result. Inventing a fake progress bar for either would be theatre.

`EnrichmentRunManager` mirrors `corpus.web.runs.RunManager` rather than extending it.
`Run` carries `credits_spent`/`credits_budget`, which are Supadata concepts and
meaningless here, and the ingestion manager is the one component the working YouTube
flow depends on — a "kind" branch through it to serve a second caller would put that
at risk for no gain. The duplication is about sixty lines and buys isolation.

Every operation here is also a CLI flow (`flows/nightly_entities.py`,
`flows/backfill_summaries.py`). Triggering from the dashboard is an ad-hoc extra
pass, never a replacement for or a disabling of a schedule.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sse_starlette.sse import EventSourceResponse

from corpus.config import get_settings
from corpus.db.models import Speaker, TranscriptVersion
from corpus.db.session import tenant_session
from corpus.enrich.entities import (
    enrich_documents_concurrent,
    extractor_version,
    find_unenriched_documents,
)
from corpus.enrich.restore import restore_transcript_version
from corpus.enrich.speakers import attribute_speakers
from corpus.ingest.runner import resolve_tenant_id

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api", tags=["enrichment"])

RunStatus = Literal["running", "done", "failed"]

# Same reasoning as runs.py: a sentinel, not None, so "no events then finished" stays
# distinguishable from a closed queue.
_DONE = object()


@dataclass
class EnrichmentRun:
    id: str
    status: RunStatus = "running"
    error: str | None = None
    succeeded: int = 0
    failed: int = 0
    total: int = 0
    _queue: queue.Queue = field(default_factory=queue.Queue)


class EnrichmentRunManager:
    def __init__(self) -> None:
        self._runs: dict[str, EnrichmentRun] = {}
        self._lock = threading.Lock()

    def start_entity_backfill(self, *, limit: int | None, concurrency: int) -> str:
        settings = get_settings()
        tenant_id = resolve_tenant_id(settings)
        version = extractor_version()
        with tenant_session(tenant_id) as session:
            pending = find_unenriched_documents(
                session, tenant_id=tenant_id, extractor_version_str=version, limit=limit
            )
        if not pending:
            raise ValueError("nothing to extract — every document is enriched at this version")

        run_id = str(uuid.uuid4())
        run = EnrichmentRun(id=run_id, total=len(pending))
        with self._lock:
            self._runs[run_id] = run

        threading.Thread(
            target=self._execute_entities,
            args=(run, tenant_id, pending, concurrency),
            name=f"entity-backfill-{run_id[:8]}",
            daemon=True,
        ).start()
        return run_id

    def get(self, run_id: str) -> EnrichmentRun | None:
        with self._lock:
            return self._runs.get(run_id)

    def _execute_entities(self, run, tenant_id, pending, concurrency) -> None:
        try:
            for document_id, mention_count, error in enrich_documents_concurrent(
                pending, tenant_id=tenant_id, concurrency=concurrency
            ):
                if error is not None:
                    run.failed += 1
                    run._queue.put(
                        {
                            "kind": "failed",
                            "document_id": str(document_id),
                            "detail": str(error)[:300],
                        }
                    )
                else:
                    run.succeeded += 1
                    run._queue.put(
                        {
                            "kind": "extracted",
                            "document_id": str(document_id),
                            "mentions": mention_count,
                        }
                    )
            run.status = "done"
        except Exception as exc:  # noqa: BLE001 - surfaced to the dashboard, not swallowed
            log.exception("entity_backfill_failed", run_id=run.id)
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
                            "succeeded": run.succeeded,
                            "failed": run.failed,
                            "total": run.total,
                        }
                    )
                }
                return
            yield {"data": json.dumps(item)}


manager = EnrichmentRunManager()


# --- entity extraction ------------------------------------------------------


@router.get("/entities/status")
def entity_status() -> dict[str, Any]:
    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)
    version = extractor_version()
    with tenant_session(tenant_id) as session:
        pending = find_unenriched_documents(
            session, tenant_id=tenant_id, extractor_version_str=version
        )
    return {"pending": len(pending), "extractor_version": version}


class EntityBackfillRequest(BaseModel):
    limit: int | None = None
    concurrency: int = 20


@router.post("/entities/backfill/start")
def start_entity_backfill(body: EntityBackfillRequest) -> dict[str, str]:
    try:
        run_id = manager.start_entity_backfill(limit=body.limit, concurrency=body.concurrency)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"run_id": run_id}


@router.get("/entities/stream/{run_id}")
async def stream_entity_backfill(run_id: str) -> EventSourceResponse:
    return EventSourceResponse(manager.stream(run_id))


# --- speaker attribution ----------------------------------------------------


@router.get("/speakers/status")
def speaker_status() -> dict[str, Any]:
    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)
    with tenant_session(tenant_id) as session:
        rows = session.execute(
            select(Speaker.attribution_method, func.count())
            .where(Speaker.tenant_id == tenant_id)
            .group_by(Speaker.attribution_method)
        ).all()
    return {"by_method": {method.value: count for method, count in rows}}


class SpeakerRequest(BaseModel):
    limit: int | None = None


@router.post("/speakers/attribute")
async def run_speaker_attribution(body: SpeakerRequest) -> dict[str, Any]:
    """Tier-1 heuristics only, no model inference — fast enough to answer inline.
    Wrapped in a thread anyway because it is a blocking DB workload and this handler
    is async; without that it would stall the event loop for every other request."""
    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)

    def work() -> int:
        with tenant_session(tenant_id) as session:
            return attribute_speakers(session, tenant_id=tenant_id, limit=body.limit)

    return {"processed": await asyncio.to_thread(work)}


# --- transcript restoration -------------------------------------------------


@router.post("/documents/{document_id}/restore")
async def restore_document(document_id: str) -> dict[str, Any]:
    """Punctuation-restore one document's latest transcript into a new version.

    Non-destructive by construction: writes a new `transcript_version` with
    `derived_from_id` pointing at the parent and never touches the original, so no
    confirmation step is warranted. CPU model inference, hence the thread.
    """
    try:
        doc_uuid = uuid.UUID(document_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"{document_id!r} is not a valid document_id"
        ) from exc

    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)

    def work() -> uuid.UUID | None:
        with tenant_session(tenant_id) as session:
            # Same "latest transcript version" lookup corpus_provenance uses.
            tv_id = session.execute(
                select(TranscriptVersion.id)
                .where(
                    TranscriptVersion.document_id == doc_uuid,
                    TranscriptVersion.tenant_id == tenant_id,
                )
                .order_by(TranscriptVersion.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if tv_id is None:
                return None
            return restore_transcript_version(
                session, tenant_id=tenant_id, transcript_version_id=tv_id
            )

    new_id = await asyncio.to_thread(work)
    if new_id is None:
        raise HTTPException(
            status_code=400,
            detail="no transcript to restore for that document (unknown id, or empty transcript)",
        )
    return {"transcript_version_id": str(new_id)}
