"""Writing to `query_log` — one place, so the write can never become the thing that
breaks a search.

Every function here swallows its own exceptions. Logging is observability, and an
observability failure must not fail the query the user actually asked for: a broken
index, a full disk, or a schema drift should degrade the backlog, not the corpus.
The failure is logged rather than silently dropped, so a log that stops filling up is
diagnosable instead of merely puzzling.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import delete
from sqlalchemy.orm import Session

from corpus.config import get_settings
from corpus.db.models import QueryLog

log = structlog.get_logger(__name__)


def record_query(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    tool: str,
    surface: str,
    query_text: str,
    domain: str | None = None,
    result_count: int | None = None,
    top_document_ids: list[uuid.UUID] | None = None,
    coverage_grade: str | None = None,
    indexed_documents: int | None = None,
    total_documents: int | None = None,
    latency_ms: int | None = None,
    answered_well: bool | None = None,
) -> None:
    """Best-effort insert. Never raises, never rolls back the caller's work."""
    if not get_settings().log_queries:
        return
    try:
        session.add(
            QueryLog(
                tenant_id=tenant_id,
                tool=tool,
                surface=surface,
                query_text=query_text,
                domain=domain,
                result_count=result_count,
                top_document_ids=top_document_ids,
                coverage_grade=coverage_grade,
                indexed_documents=indexed_documents,
                total_documents=total_documents,
                latency_ms=latency_ms,
                answered_well=answered_well,
            )
        )
        session.commit()
    except Exception as exc:  # observability must not break the query it observes
        session.rollback()
        log.warning("query_log_write_failed", tool=tool, error=f"{type(exc).__name__}: {exc}")


def purge_queries(session: Session, *, tenant_id: uuid.UUID, before=None) -> int:
    """Delete logged queries, optionally only those older than `before`. Returns the
    row count. Unlike `record_query` this DOES raise — a purge that quietly failed
    would be worse than one that errored, because the caller would believe data was
    gone when it was not."""
    stmt = delete(QueryLog).where(QueryLog.tenant_id == tenant_id)
    if before is not None:
        stmt = stmt.where(QueryLog.created_at < before)
    result = session.execute(stmt)
    session.commit()
    return result.rowcount or 0
