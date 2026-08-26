"""General-purpose cross-encoder reranking over a candidate document list — the
model mechanics live in `corpus.embedding.rerank`; this is the DB-facing half that
turns document_ids into the summary text the reranker actually scores.

`corpus.enrich.relevance_gate.rerank_relevant_documents` is a thin wrapper over this
for its narrower "which unenriched documents" use case, the same relationship
`corpus.retrieval.dense.dense_search` has with that module's dense-search wrapper —
one query, one place it's written.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from corpus.db.enums import SummaryMethod
from corpus.db.models import DocumentSummary
from corpus.embedding.rerank import rerank


def rerank_documents(
    session: Session,
    *,
    query_text: str,
    document_ids: list[uuid.UUID],
    top_k: int = 10,
    min_score: float | None = None,
) -> list[tuple[uuid.UUID, float]]:
    """(document_id, rerank_score) sorted descending — higher is more relevant.
    `document_ids` order doesn't matter and duplicates are harmless (deduped via the
    dict lookup below); this is meant to take the union of several lanes' candidates,
    not any one lane's already-ranked output specifically.

    `min_score`, if set, gates on the single best candidate's score, not a per-row
    filter — see `corpus.enrich.relevance_gate.rerank_relevant_documents`'s docstring
    for the measured reason (docs/EVAL_RELEVANCE_GATE.md): genuine hits can score far
    below a naive absolute threshold on this corpus's summary text, so filtering every
    row would silently drop real matches from a query that already found some.
    """
    if not document_ids:
        return []

    unique_ids = list(dict.fromkeys(document_ids))
    summary_rows = session.execute(
        select(DocumentSummary.document_id, DocumentSummary.text).where(
            DocumentSummary.document_id.in_(unique_ids),
            DocumentSummary.method == SummaryMethod.EXTRACTIVE_TEXTRANK,
        )
    ).all()
    text_by_doc = {row.document_id: row.text for row in summary_rows if row.text.strip()}
    if not text_by_doc:
        return []

    doc_ids = list(text_by_doc.keys())
    scores = rerank(query_text, [text_by_doc[doc_id] for doc_id in doc_ids])
    ranked = sorted(zip(doc_ids, scores, strict=True), key=lambda row: row[1], reverse=True)

    if min_score is not None and (not ranked or ranked[0][1] < min_score):
        return []
    return ranked[:top_k]
