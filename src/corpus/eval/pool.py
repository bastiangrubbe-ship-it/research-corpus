"""Pooled-candidate collection — the union of each retrieval lane's top-k, each
document judged once regardless of how many lanes surfaced it.

This is what makes "recall" in this harness mean something specific and limited:
pooled recall@k is recall *relative to the pool*, not recall against the whole
corpus. A document no lane surfaces at all can never enter the pool and never counts
against any lane — this is the same caveat the build plan's own eval-cost section
already states for retrieval ground truth generally, not new to this file.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from corpus.db.models import Document
from corpus.retrieval.dense import chunk_dense_search, dense_search
from corpus.retrieval.lexical import lexical_search
from corpus.retrieval.rerank import rerank_documents

LANES = ("lexical", "dense", "chunk_dense", "reranked")


@dataclass(frozen=True, slots=True)
class PooledCandidate:
    document_id: uuid.UUID
    title: str | None
    surfaced_by: frozenset[str]


def build_pool(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    query_text: str,
    domain,
    top_k: int = 20,
) -> list[PooledCandidate]:
    """Runs all three lanes at `top_k` each and returns their deduplicated union.
    `surfaced_by` records which lane(s) placed each document in its own top-k —
    `run.py` uses this, not a re-query, to compute each lane's pooled recall.

    The "reranked" lane reranks the union of the lexical and dense candidates
    (matching how `corpus.retrieval.search.hybrid_search` composes them), not an
    independent top-k of its own — there's no such thing as a reranker search from
    scratch, it only ever refines another lane's candidates.
    """
    lexical_hits = lexical_search(
        session, tenant_id=tenant_id, query_text=query_text, domain=domain, top_k=top_k
    )
    dense_hits = dense_search(
        session, tenant_id=tenant_id, query_text=query_text, domain=domain, top_k=top_k
    )
    # The chunk lane searches transcript windows rather than a summary — measured
    # separately here because its whole reason to exist is finding documents the
    # summary lane structurally cannot (docs/DECISIONS.md, 2026-08-25). Reporting
    # them fused only would hide whether it actually contributes.
    chunk_hits = [
        (doc_id, dist)
        for doc_id, dist, _chunk_id, _start_ms in chunk_dense_search(
            session, tenant_id=tenant_id, query_text=query_text, domain=domain, top_k=top_k
        )
    ]
    union_ids = list(
        dict.fromkeys(
            [d for d, _ in lexical_hits] + [d for d, _ in dense_hits] + [d for d, _ in chunk_hits]
        )
    )
    reranked_hits = rerank_documents(
        session, query_text=query_text, document_ids=union_ids, top_k=top_k
    )

    surfaced_by: dict[uuid.UUID, set[str]] = defaultdict(set)
    for doc_id, _ in lexical_hits:
        surfaced_by[doc_id].add("lexical")
    for doc_id, _ in dense_hits:
        surfaced_by[doc_id].add("dense")
    for doc_id, _ in chunk_hits:
        surfaced_by[doc_id].add("chunk_dense")
    for doc_id, _ in reranked_hits:
        surfaced_by[doc_id].add("reranked")

    if not surfaced_by:
        return []

    titles = dict(
        session.execute(
            select(Document.id, Document.title).where(Document.id.in_(surfaced_by.keys()))
        ).all()
    )
    return [
        PooledCandidate(document_id=doc_id, title=titles.get(doc_id), surfaced_by=frozenset(lanes))
        for doc_id, lanes in surfaced_by.items()
    ]
