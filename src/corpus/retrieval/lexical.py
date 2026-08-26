"""Lexical search — Postgres full-text search over document title/description and
the extractive summary, the "BM25 alone" first lane in the build plan's step 8 order.

Not literal BM25 (Postgres's `ts_rank_cd` is a different, older ranking function),
but the same role: fast, no embeddings, catches exact-term matches dense search can
miss. Computed live via `to_tsvector`/`plainto_tsquery` at query time rather than a
stored generated column with a GIN index — at this corpus's current scale (3,344
documents) a sequential text scan is fast enough that the index is premature, same
reasoning as `corpus.analytics`'s live-query decision (see docs/DECISIONS.md). Revisit
if `EXPLAIN ANALYZE` says otherwise.

Searches `document.title`/`description` (always present) OUTER JOINed with
`document_summary.text` (only present where `backfill_document_summaries` has run) —
a document without a summary yet still competes on title/description alone rather
than being invisible to lexical search entirely.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, literal, select
from sqlalchemy.orm import Session

from corpus.db.enums import SourceStatus, SummaryMethod
from corpus.db.models import Document, DocumentSummary, Source


def lexical_search(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    query_text: str,
    domain,
    top_k: int = 20,
) -> list[tuple[uuid.UUID, float]]:
    """(document_id, ts_rank_cd score) ranked descending — higher means a stronger
    lexical match, the opposite sense of `corpus.retrieval.dense.dense_search`'s
    cosine distance. `domain` follows the same required-keyword convention as
    `corpus.analytics` and `dense_search`: pass `None` explicitly to search across
    all domains.
    """
    searchable_text = func.concat_ws(
        " ",
        func.coalesce(Document.title, ""),
        func.coalesce(Document.description, ""),
        func.coalesce(DocumentSummary.text, ""),
    )
    tsvector = func.to_tsvector("english", searchable_text)
    tsquery = func.plainto_tsquery("english", literal(query_text))
    rank = func.ts_rank_cd(tsvector, tsquery)

    stmt = (
        select(Document.id.label("document_id"), rank.label("rank"))
        .select_from(Document)
        .join(Source, Source.id == Document.source_id)
        .outerjoin(
            DocumentSummary,
            (DocumentSummary.document_id == Document.id)
            & (DocumentSummary.method == SummaryMethod.EXTRACTIVE_TEXTRANK),
        )
        .where(
            Document.tenant_id == tenant_id,
            Source.status == SourceStatus.ACTIVE,
            tsvector.op("@@")(tsquery),
        )
    )
    if domain is not None:
        stmt = stmt.where(Source.domain == domain)
    stmt = stmt.order_by(rank.desc()).limit(top_k)

    rows = session.execute(stmt).all()
    return [(row.document_id, float(row.rank)) for row in rows]
