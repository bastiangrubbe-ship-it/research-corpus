"""Hybrid search — the `corpus_search` primitive the MCP tool and dashboard wrap.

Three lanes, RRF-fused, then reranked:

* **lexical** — Postgres full-text over title/description/summary. Exact terms, and
  the only lane that needs no embeddings at all.
* **summary dense** — one vector per document, over its extractive summary. Answers
  "what is this document *about*", holistically.
* **chunk dense** — vectors over ~1,600-character windows of the actual transcript.
  Answers "does this document *discuss* the query anywhere in its runtime".

The third lane is the point. Until 2026-08-25 there were only two, and the summary
lane searched roughly **7% of a median transcript** (median transcript 18,776 chars,
median summary 1,307), so a video covering a topic forty minutes in — without saying
so in its opening — was unreachable semantically no matter how it was queried. The
build plan made chunk-level dense conditional on evidence that document-level was
insufficient; that evidence arrived (docs/DECISIONS.md, 2026-08-25).

Summary dense is kept rather than replaced because the two answer different
questions. A passing mention in one chunk can outrank a document that is *entirely*
about the query if only chunks are considered; the summary lane is what keeps
whole-document aboutness in the fusion.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from corpus.retrieval.dense import chunk_dense_search, dense_search
from corpus.retrieval.fusion import reciprocal_rank_fusion
from corpus.retrieval.lexical import lexical_search
from corpus.retrieval.rerank import rerank_documents


def hybrid_search(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    query_text: str,
    domain,
    top_k: int = 10,
    candidate_pool: int = 50,
    rerank: bool = True,
    min_score: float | None = None,
) -> list[tuple[uuid.UUID, float]]:
    """(document_id, score) sorted best-first. Without `rerank`, score is the RRF
    fused score (higher is better, unbounded, only meaningful relative to other
    results in the same call — see `reciprocal_rank_fusion`). With `rerank` (the
    default), score is the cross-encoder's score instead, and `min_score` gates on
    it the same way `corpus.enrich.relevance_gate.rerank_relevant_documents` does:
    checked only against the single best candidate, never as a per-row filter.
    """
    lexical_hits = lexical_search(
        session, tenant_id=tenant_id, query_text=query_text, domain=domain, top_k=candidate_pool
    )
    dense_hits = dense_search(
        session, tenant_id=tenant_id, query_text=query_text, domain=domain, top_k=candidate_pool
    )
    chunk_hits = chunk_dense_search(
        session, tenant_id=tenant_id, query_text=query_text, domain=domain, top_k=candidate_pool
    )
    # chunk_dense_search carries chunk_id/start_ms for timestamp citation; RRF only
    # reads rank position, so the extra columns are dropped for fusion here rather
    # than making the fusion signature lane-specific.
    fused = reciprocal_rank_fusion(
        [lexical_hits, dense_hits, [(doc_id, dist) for doc_id, dist, _c, _s in chunk_hits]],
        top_k=candidate_pool,
    )

    if not rerank:
        return fused[:top_k]

    document_ids = [doc_id for doc_id, _score in fused]
    return rerank_documents(
        session, query_text=query_text, document_ids=document_ids, top_k=top_k, min_score=min_score
    )


def search_with_timestamps(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    query_text: str,
    domain,
    top_k: int = 10,
) -> list[tuple[uuid.UUID, float, int | None]]:
    """(document_id, cosine_distance, best_chunk_start_ms) — chunk lane only.

    Separate from `hybrid_search` on purpose: a timestamp is a property of a
    *chunk*, and once RRF has fused three lanes and a cross-encoder has rescored
    documents, the winning document's score no longer belongs to any one chunk. This
    is for "where in the video does this come up", not "which video is best".
    """
    hits = chunk_dense_search(
        session, tenant_id=tenant_id, query_text=query_text, domain=domain, top_k=top_k
    )
    return [(doc_id, dist, start_ms) for doc_id, dist, _chunk_id, start_ms in hits]
