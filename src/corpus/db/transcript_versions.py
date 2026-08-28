"""Which transcript version a stage should use.

With restoration dropped (docs/DECISIONS.md, 2026-08-27) a document has exactly one
transcript version per provider fetch, so "newest" is unambiguous and both resolvers
below return the same rows. They are kept **separate on purpose**.

The distinction they encode is real and cost two defects to learn:

* Reading a document — synthesis quotes, the eval judge, entity extraction — wants the
  most legible text available.
* Building an index — summaries, chunks — wants the text that embeds and BM25-indexes
  best, which measured *worse* when it was the more legible one: summaries dense
  P −0.046 / R −0.069 and lexical P −0.138; chunks chunk_dense P 0.413 → 0.358.

While restored versions existed, a single "newest by created_at" rule silently sent the
index to the worse source and nothing failed. Collapsing these into one function now
would delete the record of that, and the next derived-version feature — a translation,
a diarised rewrite, a cleaned variant — would reintroduce the same bug from scratch.
The cost of keeping them apart is one extra function; the cost of merging them is
rediscovering this the hard way.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from corpus.db.enums import TranscriptProvider
from corpus.db.models import TranscriptVersion

#: Providers whose output is a *derivation* of another version rather than a fetch, and
#: which must therefore never feed an index. Empty of consequence today because
#: restoration was dropped; the moment another derived provider is added, add it here.
DERIVED_PROVIDERS = (TranscriptProvider.RESTORED,)


def latest_versions(tenant_id: uuid.UUID):
    """Newest version per document, whatever the provider.

    For stages that hand text to a model to read.
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
    """Newest **non-derived** version per document — what summaries and chunks build on.

    Excludes derived providers rather than allowlisting fetched ones, so a new
    transcript source is indexable by default instead of silently vanishing from the
    index because nobody updated a list.
    """
    return (
        select(
            TranscriptVersion.document_id,
            TranscriptVersion.id.label("transcript_version_id"),
        )
        .where(
            TranscriptVersion.tenant_id == tenant_id,
            TranscriptVersion.provider.notin_(DERIVED_PROVIDERS),
        )
        .distinct(TranscriptVersion.document_id)
        .order_by(TranscriptVersion.document_id, TranscriptVersion.created_at.desc())
        .subquery()
    )
