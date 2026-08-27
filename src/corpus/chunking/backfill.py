"""Persist chunks and their embeddings — the chunk-level dense lane's index build.

Why this exists, in one number: before it, the dense lane searched
`document_summary`, a five-sentence extractive summary that is **~7% of a median
transcript** on this corpus (median transcript 18,776 chars, median summary 1,307).
A document discussing a topic forty minutes in, without saying so in its opening,
was invisible to semantic search no matter how it was queried. The build plan
anticipated this and made chunk-level dense conditional on evidence that
document-level was insufficient; that evidence arrived (docs/DECISIONS.md,
2026-08-25).

`chunk_embedding` is a partitioned table with no ORM model — partitioning by
`model_version` is the re-embedding strategy (build the new partition alongside the
live one, swap) and is expressed in raw DDL. Writes here are correspondingly raw SQL
rather than ORM inserts.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from corpus.chunking.windows import SegmentInput, build_windows
from corpus.db.models import Chunk, Segment, TranscriptVersion
from corpus.db.transcript_versions import index_versions
from corpus.embedding.encode import embed_documents, embedding_model_version

#: Chunks embedded per forward pass. Chunks are ~1,600 characters, so this is a far
#: smaller per-item payload than the reranker handles and a larger batch is safe.
_EMBED_BATCH = 64


def find_unchunked_transcript_versions(
    session: Session, *, tenant_id: uuid.UUID, limit: int | None = None
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """(transcript_version_id, document_id) for the latest transcript of each
    document that has **no chunks from any version**.

    Picks the most recent transcript version per document, the same rule
    `corpus.enrich.entities` and `relevance_gate` use.

    The exclusion is per *document*, not per transcript version, and that distinction
    is the whole point. It used to be per version, with a docstring claiming that
    "once restoration writes a derived version, chunking follows it automatically" —
    which was true, and also meant a later run silently built a SECOND chunk set
    against the restored version while leaving the raw one in place. Running it after
    a corpus-wide restoration took this corpus from 70,106 chunks to 140,849, with
    3,269 documents chunked twice.

    That was not merely wasteful. `chunk_dense_search` collapses with
    `DISTINCT ON (document_id)` ordered by distance, so the duplicate set competed for
    the same slots and won often enough to hurt: measured P 0.413 -> 0.358, R 0.485 ->
    0.431 on the frozen judgment set (docs/DECISIONS.md, 2026-08-27).

    To deliberately re-chunk against a newer version, delete that document's existing
    chunks first — `rechunk_document` does exactly that, and doing it explicitly is
    the point.
    """
    # Newest NON-restored version: a chunk is embedded, never read by a person, and
    # chunks built from restored text measured worse (chunk_dense P 0.413 -> 0.358,
    # R 0.485 -> 0.431). See corpus.db.transcript_versions.
    latest = index_versions(tenant_id)
    # Per document, not per transcript version — see the docstring.
    already = select(Chunk.document_id).where(Chunk.tenant_id == tenant_id)
    stmt = select(latest.c.transcript_version_id, latest.c.document_id).where(
        latest.c.document_id.notin_(already)
    )
    if limit:
        stmt = stmt.limit(limit)
    return [(row[0], row[1]) for row in session.execute(stmt).all()]


def chunk_transcript_version(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    transcript_version_id: uuid.UUID,
    document_id: uuid.UUID,
) -> list[uuid.UUID]:
    """Build and persist chunk rows. Returns the new chunk ids, in order."""
    rows = session.execute(
        select(Segment.text, Segment.offset_ms, Segment.duration_ms)
        .where(Segment.transcript_version_id == transcript_version_id)
        .order_by(Segment.idx)
    ).all()
    windows = build_windows(
        [SegmentInput(text=r.text, offset_ms=r.offset_ms, duration_ms=r.duration_ms) for r in rows]
    )
    if not windows:
        return []

    chunks = [
        Chunk(
            tenant_id=tenant_id,
            document_id=document_id,
            transcript_version_id=transcript_version_id,
            idx=w.idx,
            text=w.text,
            start_ms=w.start_ms,
            end_ms=w.end_ms,
            token_count=w.token_estimate,
        )
        for w in windows
    ]
    session.add_all(chunks)
    session.flush()  # populate server-generated ids before embedding
    return [c.id for c in chunks]


def embed_chunks(session: Session, *, tenant_id: uuid.UUID, chunk_ids: list[uuid.UUID]) -> int:
    """Embed the given chunks and write `chunk_embedding` rows. Returns the count.

    Skips chunks that already have an embedding at the current model version, so a
    re-run after an interruption is cheap rather than duplicative.
    """
    if not chunk_ids:
        return 0
    model_version = embedding_model_version()

    existing = set(
        session.execute(
            text(
                "SELECT chunk_id FROM chunk_embedding "
                "WHERE model_version = :mv AND chunk_id = ANY(:ids)"
            ),
            {"mv": model_version, "ids": list(chunk_ids)},
        ).scalars()
    )
    todo = [cid for cid in chunk_ids if cid not in existing]
    if not todo:
        return 0

    texts_by_id = dict(
        session.execute(select(Chunk.id, Chunk.text).where(Chunk.id.in_(todo))).all()
    )
    written = 0
    for start in range(0, len(todo), _EMBED_BATCH):
        batch = [cid for cid in todo[start : start + _EMBED_BATCH] if cid in texts_by_id]
        if not batch:
            continue
        vectors = embed_documents([texts_by_id[cid] for cid in batch])
        session.execute(
            text(
                "INSERT INTO chunk_embedding (chunk_id, tenant_id, model_version, embedding) "
                "VALUES (:chunk_id, :tenant_id, :mv, CAST(:embedding AS halfvec)) "
                "ON CONFLICT (model_version, chunk_id) DO NOTHING"
            ),
            [
                {
                    "chunk_id": cid,
                    "tenant_id": tenant_id,
                    "mv": model_version,
                    "embedding": "[" + ",".join(f"{v:.6f}" for v in vec) + "]",
                }
                # strict: a length mismatch here would silently mis-pair a chunk
                # with another chunk's vector — corrupt, and invisible afterwards.
                for cid, vec in zip(batch, vectors, strict=True)
            ],
        )
        written += len(batch)
    return written


def backfill_chunks(
    session: Session, *, tenant_id: uuid.UUID, limit: int | None = None
) -> tuple[int, int]:
    """Chunk and embed transcripts that have no chunks yet.
    Returns (documents processed, chunks embedded)."""
    pending = find_unchunked_transcript_versions(session, tenant_id=tenant_id, limit=limit)
    docs = 0
    chunks = 0
    for transcript_version_id, document_id in pending:
        chunk_ids = chunk_transcript_version(
            session,
            tenant_id=tenant_id,
            transcript_version_id=transcript_version_id,
            document_id=document_id,
        )
        chunks += embed_chunks(session, tenant_id=tenant_id, chunk_ids=chunk_ids)
        docs += 1
    session.commit()
    return docs, chunks


def rechunk_document(
    session: Session, *, tenant_id: uuid.UUID, document_id: uuid.UUID
) -> tuple[int, int]:
    """Delete a document's existing chunks and rebuild from its latest transcript
    version. Returns (deleted, written).

    Explicit because the alternative — having the backfill quietly follow the newest
    version — produced two live chunk sets per document and measurably worse
    retrieval. Superseding an index is a decision, not a side effect.
    """
    deleted = session.execute(
        delete(Chunk).where(Chunk.tenant_id == tenant_id, Chunk.document_id == document_id)
    ).rowcount
    version_id = session.execute(
        select(TranscriptVersion.id)
        .where(
            TranscriptVersion.tenant_id == tenant_id,
            TranscriptVersion.document_id == document_id,
        )
        .order_by(TranscriptVersion.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if version_id is None:
        session.commit()
        return deleted, 0
    chunk_ids = chunk_transcript_version(
        session, tenant_id=tenant_id, transcript_version_id=version_id, document_id=document_id
    )
    written = embed_chunks(session, tenant_id=tenant_id, chunk_ids=chunk_ids)
    session.commit()
    return deleted, written
