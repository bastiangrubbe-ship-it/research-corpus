"""Document-level dense (semantic) search — the one embedding-based retrieval lane
the build plan calls for, over `document_summary.embedding`.

General-purpose: searches the *whole* corpus, unlike
`corpus.enrich.relevance_gate.find_relevant_unenriched_documents`, which answers a
narrower question ("what should Haiku process next") and excludes anything already
enriched. That function is being refactored to call `dense_search` internally with an
exclusion subquery, rather than duplicating this same cosine-distance query a second
time — `exclude_document_ids` takes a `Select` (not a materialized collection)
specifically so a large exclusion set stays a subquery in the executed SQL rather than
thousands of literal UUIDs inlined into an IN list.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, select, text
from sqlalchemy.orm import Session

from corpus.db.enums import SourceStatus, SummaryMethod
from corpus.db.models import Document, DocumentSummary, Source
from corpus.embedding.encode import embed_query, embedding_model_version


def dense_search(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    query_text: str,
    domain,
    top_k: int = 20,
    exclude_document_ids: Select | None = None,
) -> list[tuple[uuid.UUID, float]]:
    """(document_id, cosine_distance) ranked by relevance to `query_text`, lower
    distance = more similar. Restricted to active sources with a document summary
    embedded (`backfill_document_summaries` must have run for a document to be
    findable here at all — see `corpus.enrich.relevance_gate`).

    `domain` follows the same convention as `corpus.analytics`: pass a specific
    `Domain` to filter, or `None` explicitly to search across all of them — no
    silent default either way.
    """
    query_vector = embed_query(query_text)
    distance = DocumentSummary.embedding.cosine_distance(query_vector)

    stmt = (
        select(DocumentSummary.document_id, distance.label("distance"))
        .join(Document, Document.id == DocumentSummary.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            DocumentSummary.tenant_id == tenant_id,
            DocumentSummary.method == SummaryMethod.EXTRACTIVE_TEXTRANK,
            DocumentSummary.embedding.is_not(None),
            Source.status == SourceStatus.ACTIVE,
        )
    )
    if domain is not None:
        stmt = stmt.where(Source.domain == domain)
    if exclude_document_ids is not None:
        stmt = stmt.where(DocumentSummary.document_id.notin_(exclude_document_ids))
    stmt = stmt.order_by(distance).limit(top_k)

    rows = session.execute(stmt).all()
    return [(row.document_id, float(row.distance)) for row in rows]


def chunk_dense_search(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    query_text: str,
    domain,
    top_k: int = 20,
    exclude_document_ids: Select | None = None,
) -> list[tuple[uuid.UUID, float, uuid.UUID | None, int | None]]:
    """Dense search over transcript *chunks*, collapsed to documents.

    Returns (document_id, best_cosine_distance, best_chunk_id, best_chunk_start_ms),
    ordered best-first. The score for a document is its single best-matching chunk —
    not an average. Averaging would penalise exactly the documents this lane exists
    to find: an hour-long video that covers the query thoroughly for four minutes and
    discusses other things for the rest is a strong hit, and its mean chunk distance
    would say otherwise.

    The collapse happens in SQL (`DISTINCT ON`), so every chunk is considered and
    then reduced to one row per document before `top_k` is applied — no over-fetch
    heuristic is needed to avoid several top chunks from one video crowding out
    other documents.

    `start_ms` comes back so a caller can cite a timestamp rather than a whole video;
    it is None for chunks whose segments carried no offsets.
    """
    query_vector = embed_query(query_text)
    vector_literal = "[" + ",".join(f"{v:.6f}" for v in query_vector) + "]"

    # chunk_embedding is a partitioned table with no ORM model (see
    # corpus.chunking.backfill), so this leg is raw SQL. DISTINCT ON collapses to the
    # best chunk per document inside the database rather than pulling every chunk
    # back to Python to reduce.
    sql = """
        SELECT DISTINCT ON (c.document_id)
               c.document_id, c.id AS chunk_id, c.start_ms,
               ce.embedding <=> CAST(:qv AS halfvec) AS distance
        FROM chunk_embedding ce
        JOIN chunk c ON c.id = ce.chunk_id
        JOIN document d ON d.id = c.document_id
        JOIN source s ON s.id = d.source_id
        WHERE ce.tenant_id = :tenant_id
          AND ce.model_version = :mv
          AND s.status = 'active'
          {domain_clause}
          {exclude_clause}
        ORDER BY c.document_id, distance
    """
    params: dict = {
        "qv": vector_literal,
        "tenant_id": tenant_id,
        "mv": embedding_model_version(),
        "limit": top_k,
    }
    domain_clause = ""
    if domain is not None:
        domain_clause = "AND s.domain = :domain"
        params["domain"] = domain.value if hasattr(domain, "value") else domain

    exclude_clause = ""
    if exclude_document_ids is not None:
        excluded = [row[0] for row in session.execute(exclude_document_ids).all()]
        if excluded:
            exclude_clause = "AND c.document_id <> ALL(:excluded)"
            params["excluded"] = excluded

    inner = sql.format(domain_clause=domain_clause, exclude_clause=exclude_clause)
    # Rank the per-document bests against each other, then cut to top_k.
    stmt = text(
        f"SELECT document_id, chunk_id, start_ms, distance FROM ({inner}) best "
        "ORDER BY distance LIMIT :limit"
    )
    rows = session.execute(stmt, params).all()
    return [(r.document_id, float(r.distance), r.chunk_id, r.start_ms) for r in rows]
