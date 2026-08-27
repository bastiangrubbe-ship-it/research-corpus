"""Which transcript version a stage should use — and why that depends on the stage.

A document can hold several `transcript_version` rows: the raw provider transcript, and
a punctuation-restored derivation of it. "Which one is current" has two different right
answers, and conflating them has caused two separate defects on this corpus
(docs/DECISIONS.md, 2026-08-26 and 2026-08-27).

**Reading a document → `latest_versions`.** Synthesis quotes, the eval judge, entity
extraction. These hand text to a language model, and restored text is genuinely better
for that: a verbatim citation pulled from unpunctuated ASR is unreadable.

**Building an index → `index_versions`.** Summaries and chunks. These are embedded by a
bi-encoder and counted by BM25, and restored text is measurably *worse* input for both:

* summaries re-derived from restored text: dense P −0.046 / R −0.069, lexical P −0.138
* chunks built from restored text: chunk_dense P 0.413 → 0.358, R 0.485 → 0.431

Restoration adds punctuation and drops nothing, but the extractive summariser then
selects *fewer, better-formed* sentences — 27% less text, so less topical surface for a
vector to carry. Coverage beats grammaticality when nothing human reads the artefact.

The trap this closes: both stages previously resolved "newest by created_at", which is
correct until restoration runs and then silently wrong forever after. A backfill run at
any point after restoration would quietly rebuild the index from the worse source, and
nothing would fail. Pinning by provider makes the choice explicit at the query, where
it cannot be forgotten.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from corpus.db.enums import TranscriptProvider
from corpus.db.models import TranscriptVersion


def latest_versions(tenant_id: uuid.UUID):
    """Newest version per document, whatever its provider — restored if one exists.

    For stages that give text to a model to read.
    """
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


def index_versions(tenant_id: uuid.UUID):
    """Newest **non-restored** version per document.

    For stages that build something a machine searches — summaries, chunks. See the
    module docstring for the measurements.

    A document with only a restored version cannot occur (restoration always writes
    `derived_from_id` pointing at its raw parent, and never deletes it), so excluding
    restored rows here cannot orphan a document. If that invariant is ever broken the
    document drops out of the index rather than being indexed from the wrong source,
    which is the safer failure: `flows/doctor.py` reports a missing stage loudly, while
    a quietly worse index reports nothing at all.
    """
    return (
        select(
            TranscriptVersion.document_id,
            TranscriptVersion.id.label("transcript_version_id"),
        )
        .where(
            TranscriptVersion.tenant_id == tenant_id,
            TranscriptVersion.provider != TranscriptProvider.RESTORED,
        )
        .distinct(TranscriptVersion.document_id)
        .order_by(TranscriptVersion.document_id, TranscriptVersion.created_at.desc())
        .subquery()
    )
