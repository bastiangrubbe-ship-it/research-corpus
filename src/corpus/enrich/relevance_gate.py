"""Query-triggered entity extraction: a local, cheap relevance gate in front of the
expensive `claude -p` call in corpus.enrich.entities.

The nightly backfill (flows/nightly_entities.py) processes every unenriched document
in FIFO order regardless of whether anything will ever query it. This module is the
other mode: given a query, embed it locally (no LLM, no network), rank unenriched
documents by cosine similarity against their `document_summary.embedding`, and return
only the ones actually relevant to that query — so Haiku only runs on documents a real
question touched.

Two steps, deliberately separate:

1. `backfill_document_summaries` — extractive summary (corpus.enrich.summarize) +
   local embedding (corpus.embedding.encode) for documents that don't have one yet.
   Cheap enough to run eagerly for the whole corpus; unlike entity extraction, this
   step never calls Claude.
2. `find_relevant_unenriched_documents` — the cheap gate. Embeds the query, does a
   pgvector cosine-distance search over `document_summary`, and filters out documents
   already enriched at the current extractor_version — the same "already done" check
   `entities.find_unenriched_documents` makes, scoped down to a similarity-ranked
   candidate set instead of the whole backlog.
3. `rerank_relevant_documents` — an optional, more accurate refinement over step 2's
   output. Cosine similarity compares two independently-computed vectors; a
   cross-encoder (corpus.embedding.rerank) lets the query and each candidate's summary
   attend to each other jointly, which is why it catches things step 2 misses (see
   docs/EVAL_RELEVANCE_GATE.md's marketing/SEO query). Only ever run over step 2's
   narrow candidate pool, never the whole backlog — a cross-encoder scores one pair at
   a time and doesn't have an index to search the way embeddings do.

This is a soft signal, not a guarantee, at either step: cosine similarity over a
5-sentence TextRank summary can miss a relevant document (false negative, silently
starves it of enrichment) or surface an irrelevant one (false positive, wastes a Haiku
call), and reranking narrows that error rate without eliminating it. Per docs/EVAL.md's
own standard for the extractor, this gate should get the same precision/recall
treatment before anything depends on it exclusively — it is not yet a replacement for
the nightly backfill, only a way to jump the queue for what's actually being asked
about right now.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from corpus.db.enums import SummaryMethod
from corpus.db.models import DocumentSummary, EntityExtractionRun, TranscriptVersion
from corpus.embedding.encode import embed_documents, embedding_model_version
from corpus.enrich.entities import reconstruct_transcript_text
from corpus.enrich.summarize import summarize_extractive
from corpus.retrieval.dense import dense_search
from corpus.retrieval.rerank import rerank_documents


def _latest_transcript_versions(tenant_id: uuid.UUID):
    """(document_id, transcript_version_id) for each document's most recent
    transcript_version — same "which version is current" logic entities.py uses."""
    return (
        select(
            TranscriptVersion.document_id,
            TranscriptVersion.id.label("transcript_version_id"),
        )
        .where(TranscriptVersion.tenant_id == tenant_id)
        .distinct(TranscriptVersion.document_id)
        .order_by(TranscriptVersion.document_id, TranscriptVersion.created_at.desc())
        .subquery()
    )


def backfill_document_summaries(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    limit: int | None = None,
    redo: bool = False,
    offset: int = 0,
) -> int:
    """Summarize + embed documents that don't have a document_summary row yet.

    Runs entirely locally — no `claude -p` call anywhere in this path. Returns the
    number of documents processed.

    `redo=True` re-derives summaries that already exist, overwriting them. This is for
    when the *input* has changed rather than the code: `_latest_transcript_versions`
    resolves to a document's newest version, so once punctuation restoration has run,
    a re-derivation reads restored text where the original read raw ASR. That matters
    because `summarize_extractive` needs sentence boundaries — TextRank has nothing to
    rank when NLTK cannot split the text, which on this corpus left ~5% of documents
    with a summary that was effectively one truncated blob (docs/DECISIONS.md).

    Without `redo` the upsert is `on_conflict_do_nothing`, so re-running is a no-op
    rather than a refresh — safe, but silently so. Anyone expecting a re-derivation
    from a plain re-run would get "3,269 processed" and no change at all.

    `offset` exists only for `redo`. The normal path needs no cursor because its work
    list shrinks as rows are written, so repeated calls walk forward on their own; a
    redo's work list is every document every time, and a caller batching on
    "returned 0" would otherwise re-derive the same first batch until interrupted.
    """
    latest = _latest_transcript_versions(tenant_id)
    stmt = select(latest.c.document_id, latest.c.transcript_version_id)
    if not redo:
        already_summarized = select(DocumentSummary.document_id).where(
            DocumentSummary.tenant_id == tenant_id,
            DocumentSummary.method == SummaryMethod.EXTRACTIVE_TEXTRANK,
        )
        stmt = stmt.where(latest.c.document_id.notin_(already_summarized))
    # Ordered so `offset` is stable across calls — without it Postgres may return
    # rows in a different order each time and a paginated redo would skip documents
    # while re-deriving others twice.
    stmt = stmt.order_by(latest.c.document_id)
    if offset:
        stmt = stmt.offset(offset)
    if limit:
        stmt = stmt.limit(limit)
    pending = session.execute(stmt).all()
    if not pending:
        return 0

    texts = []
    for _document_id, transcript_version_id in pending:
        text = reconstruct_transcript_text(session, transcript_version_id)
        texts.append(summarize_extractive(text))

    non_empty_idx = [i for i, t in enumerate(texts) if t.strip()]
    embeddings: dict[int, list[float]] = {}
    if non_empty_idx:
        vectors = embed_documents([texts[i] for i in non_empty_idx])
        # strict: a length mismatch would pair a summary with another's vector.
        embeddings = dict(zip(non_empty_idx, vectors, strict=True))

    model_version = embedding_model_version()
    rows = [
        {
            "tenant_id": tenant_id,
            "document_id": document_id,
            "method": SummaryMethod.EXTRACTIVE_TEXTRANK,
            "text": texts[i],
            "embedding": embeddings.get(i),
            "model_version": model_version if i in embeddings else None,
        }
        for i, (document_id, _tv_id) in enumerate(pending)
    ]
    insert = pg_insert(DocumentSummary).values(rows)
    if redo:
        # Overwrite text, embedding and model_version together. Updating the text
        # without its vector would leave the dense lane searching a vector of the old
        # summary while the reranker reads the new one — the two would silently
        # disagree about what the document says.
        session.execute(
            insert.on_conflict_do_update(
                index_elements=["document_id", "method"],
                set_={
                    "text": insert.excluded.text,
                    "embedding": insert.excluded.embedding,
                    "model_version": insert.excluded.model_version,
                },
            )
        )
    else:
        session.execute(insert.on_conflict_do_nothing(index_elements=["document_id", "method"]))
    session.commit()
    return len(rows)


def find_relevant_unenriched_documents(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    query_text: str,
    extractor_version_str: str,
    top_k: int = 20,
    max_distance: float | None = None,
) -> list[tuple[uuid.UUID, uuid.UUID, float]]:
    """(document_id, transcript_version_id, cosine_distance) ranked by relevance to
    `query_text`, restricted to documents not yet entity-extracted at
    `extractor_version_str`. Lower distance = more similar; 0.0 is identical, 2.0 is
    opposite. `max_distance` is an optional hard cutoff — leave unset to just take the
    top_k regardless of how weak the match is, which is the right default until this
    gate has an eval-measured threshold (see docs/EVAL.md's rubric for the precedent).

    Thin wrapper over `corpus.retrieval.dense.dense_search` — this function's only
    job is the "not yet enriched" exclusion; the cosine-distance search itself lives
    there so retrieval and enrichment-gating never carry two copies of the same query.
    """
    already_enriched = select(EntityExtractionRun.document_id).where(
        EntityExtractionRun.tenant_id == tenant_id,
        EntityExtractionRun.extractor_version == extractor_version_str,
    )
    hits = dense_search(
        session,
        tenant_id=tenant_id,
        query_text=query_text,
        domain=None,
        top_k=top_k,
        exclude_document_ids=already_enriched,
    )
    if max_distance is not None:
        hits = [(doc_id, dist) for doc_id, dist in hits if dist <= max_distance]
    if not hits:
        return []

    latest = _latest_transcript_versions(tenant_id)
    doc_ids = [doc_id for doc_id, _dist in hits]
    tv_by_doc = dict(
        session.execute(
            select(latest.c.document_id, latest.c.transcript_version_id).where(
                latest.c.document_id.in_(doc_ids)
            )
        ).all()
    )
    return [(doc_id, tv_by_doc[doc_id], dist) for doc_id, dist in hits if doc_id in tv_by_doc]


def rerank_relevant_documents(
    session: Session,
    *,
    query_text: str,
    candidates: list[tuple[uuid.UUID, uuid.UUID, float]],
    top_k: int = 10,
    min_score: float | None = None,
) -> list[tuple[uuid.UUID, uuid.UUID, float]]:
    """Cross-encoder refinement over `find_relevant_unenriched_documents`'s output.

    `candidates` is that function's return value (or any (document_id,
    transcript_version_id, _) triples) — the cosine distance in the third slot is
    ignored here, not reused, since a reranker score and a cosine distance are not the
    same unit and mixing them is exactly the kind of silent bug this split is meant to
    avoid. Returns (document_id, transcript_version_id, rerank_score) sorted
    descending — higher score is more relevant, the opposite sense of cosine distance.

    `min_score`, if set, is checked ONLY against the top-ranked candidate's score, not
    applied as a per-row filter — measured in docs/EVAL_RELEVANCE_GATE.md, the top
    score across three real queries was 0.82 (strong match), 0.068 (weaker but real
    match), and 0.0006 (no real match in the backlog at all) — a ~100x gap between
    "nothing here" and "something here," clean enough to threshold on. But genuine
    hits within a real query can themselves score as low as 0.003-0.004 (score
    compression on this corpus's noisy summary text, same finding as the rest of that
    doc), so filtering every row by an absolute cutoff would silently drop real
    matches from a query that already found some. Checking only the top score answers
    a different, safer question — "does this query have any real match in the
    backlog at all" — and returns everything or nothing accordingly, never a
    row-by-row guess.
    """
    if not candidates:
        return []

    tv_by_doc = {doc_id: tv_id for doc_id, tv_id, _ in candidates}
    ranked = rerank_documents(
        session,
        query_text=query_text,
        document_ids=list(tv_by_doc.keys()),
        top_k=top_k,
        min_score=min_score,
    )
    return [(doc_id, tv_by_doc[doc_id], score) for doc_id, score in ranked]
