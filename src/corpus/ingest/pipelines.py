"""Incremental ingestion for a source.

Deliberately hand-rolled rather than built on a workflow library (dlt was tried and
rejected — see docs/DECISIONS.md). The pieces a library like that would provide are
already here in a shape that fits the rest of the schema:

* **Dedup** rides on the database, not a separately-maintained cursor. `document`
  carries a unique constraint on (tenant_id, source_id, external_id); checking whether
  a video is new is one indexed lookup, and it can never drift out of sync with what
  was actually persisted the way a hand-maintained "last seen" cursor can.
* **Idempotent writes** ride on the bronze store's content-addressing (§bronze/store.py)
  and the same unique constraints via upsert.
* **Incremental discovery** is genuinely simple for this source: yt-dlp's channel
  listing has no date filter to speak of, so the real incremental behaviour is "skip
  what's already a `document` row" rather than a paginated cursor walk.

`ingest_state` still exists and is written to — not as the dedup mechanism, but as the
last-checked-at bookkeeping the heartbeat and ops tooling read.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from corpus.bronze.store import BronzeStore
from corpus.db.enums import DocumentStatus
from corpus.db.models import Document, IngestState, Segment, Source, TranscriptVersion
from corpus.sources.base import CreditBudgetExceeded, FetchResult, SourceAdapter, SourceError

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IngestSummary:
    source_id: uuid.UUID
    discovered: int
    already_ingested: int
    fetched: int
    failed: int
    errors: list[str]


def ingest_source(
    session: Session,
    *,
    source: Source,
    adapter: SourceAdapter,
    bronze: BronzeStore,
    limit: int | None = None,
) -> IngestSummary:
    """Ingest one source. Safe to call twice: the second call discovers the same
    videos, finds every one already has a `document` row, and fetches nothing.
    Safe to interrupt: each video is committed independently, so a kill mid-run
    leaves already-processed videos persisted and unprocessed ones simply unstarted
    — never a half-written document.
    """
    external_ids = list(adapter.discover(source.external_id, limit=limit))
    log.info("discovered", source=source.external_id, count=len(external_ids))

    existing = set(
        session.execute(
            select(Document.external_id).where(
                Document.tenant_id == source.tenant_id,
                Document.source_id == source.id,
                Document.external_id.in_(external_ids),
            )
        )
        .scalars()
        .all()
    )

    fetched = 0
    failed = 0
    errors: list[str] = []

    for external_id in external_ids:
        if external_id in existing:
            continue
        try:
            result = adapter.fetch(external_id)
        except CreditBudgetExceeded:
            # Not a per-video failure: the budget is exhausted for this run.
            # Stop cleanly rather than let every remaining video fail one at a time.
            log.warning("credit_budget_exceeded", source=source.external_id, remaining=external_ids)
            break
        except SourceError as exc:
            failed += 1
            errors.append(f"{external_id}: {exc}")
            log.warning(
                "fetch_failed", source=source.external_id, video=external_id, error=str(exc)
            )
            continue

        _persist(session, source=source, external_id=external_id, result=result, bronze=bronze)
        session.commit()
        fetched += 1

    _touch_ingest_state(session, source)
    session.commit()

    return IngestSummary(
        source_id=source.id,
        discovered=len(external_ids),
        already_ingested=len(existing),
        fetched=fetched,
        failed=failed,
        errors=errors,
    )


def _persist(
    session: Session, *, source: Source, external_id: str, result: FetchResult, bronze: BronzeStore
) -> None:
    for raw in result.raw:
        bronze.write(raw)

    doc = Document(
        tenant_id=source.tenant_id,
        source_id=source.id,
        external_id=external_id,
        url=result.document.url,
        title=result.document.title,
        description=result.document.description,
        duration_s=result.document.duration_s,
        published_at=result.document.published_at,
        published_at_precision=result.document.published_at_precision,
        published_at_source=result.document.published_at_source,
        status=DocumentStatus.ENRICHED if result.transcript else DocumentStatus.FAILED,
    )
    session.add(doc)
    session.flush()  # assigns doc.id without committing

    if result.transcript is not None:
        tv = TranscriptVersion(
            tenant_id=source.tenant_id,
            document_id=doc.id,
            provider=result.transcript.provider,
            is_auto_generated=result.transcript.is_auto_generated,
            provenance_confidence=result.transcript.provenance_confidence,
            lang=result.transcript.lang,
            available_langs=list(result.transcript.available_langs),
        )
        session.add(tv)
        session.flush()

        session.add_all(
            Segment(
                tenant_id=source.tenant_id,
                transcript_version_id=tv.id,
                idx=seg.idx,
                text=seg.text,
                offset_ms=seg.offset_ms,
                duration_ms=seg.duration_ms,
            )
            for seg in result.transcript.segments
        )


def _touch_ingest_state(session: Session, source: Source) -> None:
    stmt = (
        pg_insert(IngestState)
        .values(tenant_id=source.tenant_id, source_id=source.id, updated_at=dt.datetime.now(dt.UTC))
        .on_conflict_do_update(
            index_elements=[IngestState.tenant_id, IngestState.source_id],
            set_={"updated_at": dt.datetime.now(dt.UTC)},
        )
    )
    # ingest_state has no unique constraint on (tenant_id, source_id) yet — added
    # in the accompanying migration specifically so this upsert has a target.
    session.execute(stmt)
