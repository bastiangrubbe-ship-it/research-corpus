"""SQLAlchemy models.

Every table carries `tenant_id`. There is one real tenant today; the column and its
RLS policies exist from commit one because backfilling them across millions of rows
later is the one genuinely expensive retrofit in this system.

RLS policies themselves are raw DDL in the Alembic migration — SQLAlchemy has no
first-class representation for them, and pretending otherwise hides the security
boundary in an ORM abstraction.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from corpus.db import enums

EMBEDDING_DIM = 768  # nomic-embed-text-v1.5, Apache-2.0, Matryoshka-truncatable


def _enum(e: type, name: str) -> Enum:
    return Enum(e, name=name, native_enum=True, values_callable=lambda x: [i.value for i in x])


class Base(DeclarativeBase):
    pass


class TenantScoped:
    """Mixin. Presence of this mixin is what the migration keys RLS policies off."""

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False, index=True
    )


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )


def _now() -> Mapped[dt.datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ---------------------------------------------------------------------------
# tenancy
# ---------------------------------------------------------------------------
class Tenant(Base):
    __tablename__ = "tenant"

    id: Mapped[uuid.UUID] = _pk()
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[dt.datetime] = _now()


# ---------------------------------------------------------------------------
# sources and documents
# ---------------------------------------------------------------------------
class Source(Base, TenantScoped):
    __tablename__ = "source"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "external_id", name="uq_source_tenant_kind_external"),
    )

    id: Mapped[uuid.UUID] = _pk()
    kind: Mapped[enums.SourceKind] = mapped_column(_enum(enums.SourceKind, "source_kind"))
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)

    # Curated by hand; small N. Drives retrieval weighting and synthesis attribution.
    authority_tier: Mapped[enums.AuthorityTier] = mapped_column(
        _enum(enums.AuthorityTier, "authority_tier"),
        default=enums.AuthorityTier.UNKNOWN,
        server_default="unknown",
    )
    # Orthogonal to authority_tier: what the source is about, not how much to trust
    # it. Keeps entrepreneurship/personal-development content from blending into
    # AI-vendor term-velocity and saturation counts. See enums.Domain.
    domain: Mapped[enums.Domain] = mapped_column(
        _enum(enums.Domain, "domain"),
        default=enums.Domain.UNKNOWN,
        server_default="unknown",
        index=True,
    )
    # Curation: sources must be deprecable and their content purgeable, or quality
    # falls as the corpus grows.
    status: Mapped[enums.SourceStatus] = mapped_column(
        _enum(enums.SourceStatus, "source_status"),
        default=enums.SourceStatus.ACTIVE,
        server_default="active",
        index=True,
    )
    deprecated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _now()

    documents: Mapped[list[Document]] = relationship(back_populates="source")


class Document(Base, TenantScoped):
    __tablename__ = "document"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_id", "external_id", name="uq_document_tenant_source_external"
        ),
        Index("ix_document_published", "tenant_id", "published_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source.id", ondelete="CASCADE"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    duration_s: Mapped[int | None] = mapped_column(Integer)

    # Publication date and ingest date are different facts and are never conflated.
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    published_at_precision: Mapped[enums.DatePrecision] = mapped_column(
        _enum(enums.DatePrecision, "date_precision"),
        default=enums.DatePrecision.UNKNOWN,
        server_default="unknown",
    )
    published_at_source: Mapped[str | None] = mapped_column(String(32))  # api | parsed | inferred
    ingested_at: Mapped[dt.datetime] = _now()

    # Points into the bronze store. Bronze is a filesystem and deliberately not
    # under RLS: it is the rebuild guarantee and outlives the database.
    raw_ref: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[enums.DocumentStatus] = mapped_column(
        _enum(enums.DocumentStatus, "document_status"),
        default=enums.DocumentStatus.PENDING,
        server_default="pending",
        index=True,
    )

    source: Mapped[Source] = relationship(back_populates="documents")


# ---------------------------------------------------------------------------
# transcripts
# ---------------------------------------------------------------------------
    #: Why this document has no transcript, established by probing (see
    #: enums.TranscriptUnavailableReason). NULL means never checked, which is a
    #: different claim from any reason. Paired with `transcript_probed_at` because
    #: none of the reasons is permanent — members-only is a credentials gap, and
    #: captions can be added later.
    transcript_unavailable_reason: Mapped[str | None] = mapped_column(String(32))
    transcript_probed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
class TranscriptVersion(Base, TenantScoped):
    """One row per (document, provider). A document may have several.

    `is_auto_generated` is nullable on purpose. Supadata does not report it at all, so
    its rows carry NULL + provenance_confidence='unknown'. youtube-transcript-api does
    report it, so its rows carry a real boolean + 'known'. Never coalesce NULL to
    False — "we don't know" and "human-authored" are different claims.
    """

    __tablename__ = "transcript_version"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "document_id", "provider", "lang", name="uq_transcript_doc_provider_lang"
        ),
        CheckConstraint(
            "(is_auto_generated IS NULL) = (provenance_confidence = 'unknown')",
            name="ck_transcript_provenance_consistent",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[enums.TranscriptProvider] = mapped_column(
        _enum(enums.TranscriptProvider, "transcript_provider")
    )
    is_auto_generated: Mapped[bool | None] = mapped_column(Boolean)
    provenance_confidence: Mapped[enums.ProvenanceConfidence] = mapped_column(
        _enum(enums.ProvenanceConfidence, "provenance_confidence"),
        default=enums.ProvenanceConfidence.UNKNOWN,
        server_default="unknown",
    )
    lang: Mapped[str] = mapped_column(String(16), nullable=False)
    available_langs: Mapped[list[str] | None] = mapped_column(ARRAY(String(16)))

    # Punctuation/truecasing restoration produces a new row pointing at its parent.
    # Raw text is never mutated; both remain queryable.
    derived_from_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transcript_version.id", ondelete="CASCADE")
    )
    created_at: Mapped[dt.datetime] = _now()



class Segment(Base, TenantScoped):
    """Raw timestamped segments exactly as the provider returned them."""

    __tablename__ = "segment"
    __table_args__ = (
        UniqueConstraint("transcript_version_id", "idx", name="uq_segment_version_idx"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    transcript_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transcript_version.id", ondelete="CASCADE"), index=True
    )
    idx: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    offset_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


# ---------------------------------------------------------------------------
# speakers — neither transcript API provides this; everything here is our own work
# ---------------------------------------------------------------------------
class Speaker(Base, TenantScoped):
    __tablename__ = "speaker"

    id: Mapped[uuid.UUID] = _pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("entity.id", ondelete="SET NULL")
    )
    attribution_method: Mapped[enums.AttributionMethod] = mapped_column(
        _enum(enums.AttributionMethod, "attribution_method"),
        default=enums.AttributionMethod.UNKNOWN,
        server_default="unknown",
    )
    confidence: Mapped[float | None] = mapped_column(Float)


class Utterance(Base, TenantScoped):
    """Attributed spans. Populated only for the diarized tier."""

    __tablename__ = "utterance"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), index=True
    )
    speaker_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("speaker.id", ondelete="CASCADE"), index=True
    )
    start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)


# ---------------------------------------------------------------------------
# chunks and embeddings
# ---------------------------------------------------------------------------
class Chunk(Base, TenantScoped):
    __tablename__ = "chunk"
    __table_args__ = (
        UniqueConstraint("transcript_version_id", "idx", name="uq_chunk_version_idx"),
    )

    id: Mapped[uuid.UUID] = _pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), index=True
    )
    transcript_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transcript_version.id", ondelete="CASCADE"), index=True
    )
    idx: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_ms: Mapped[int | None] = mapped_column(BigInteger)
    end_ms: Mapped[int | None] = mapped_column(BigInteger)
    token_count: Mapped[int | None] = mapped_column(Integer)
    # Chunk boundaries respect utterance boundaries where attribution exists.
    speaker_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("speaker.id", ondelete="SET NULL")
    )


class DocumentSummary(Base, TenantScoped):
    """Extractive at ingest — no LLM, milliseconds per document.

    This carries the *first* dense lane: one vector per document rather than per chunk.
    Queries 4-6 are document-level arguments, not passage-level facts, so this is both
    ~30-50x cheaper and better matched to them. Chunk-level dense is added only if the
    eval harness shows this missing things.
    """

    __tablename__ = "document_summary"
    __table_args__ = (UniqueConstraint("document_id", "method", name="uq_summary_doc_method"),)

    id: Mapped[uuid.UUID] = _pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), index=True
    )
    method: Mapped[enums.SummaryMethod] = mapped_column(
        _enum(enums.SummaryMethod, "summary_method")
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(HALFVEC(EMBEDDING_DIM))
    model_version: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[dt.datetime] = _now()


# ---------------------------------------------------------------------------
# entities — what makes analytics possible
# ---------------------------------------------------------------------------
class Entity(Base, TenantScoped):
    __tablename__ = "entity"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "canonical_name", name="uq_entity_tenant_kind_name"),
    )

    id: Mapped[uuid.UUID] = _pk()
    kind: Mapped[enums.EntityKind] = mapped_column(
        _enum(enums.EntityKind, "entity_kind"), index=True
    )
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    external_ids: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = _now()


class EntityMention(Base, TenantScoped):
    """Entities as first-class rows, not just text. Query 8 is a GROUP BY over this."""

    __tablename__ = "entity_mention"
    __table_args__ = (Index("ix_mention_entity_doc", "tenant_id", "entity_id", "document_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entity.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), index=True
    )
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("chunk.id", ondelete="CASCADE"))
    speaker_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("speaker.id", ondelete="SET NULL")
    )
    surface: Mapped[str] = mapped_column(Text, nullable=False)
    start_char: Mapped[int | None] = mapped_column(Integer)
    end_char: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float | None] = mapped_column(Float)
    extractor_version: Mapped[str | None] = mapped_column(String(128))


class EntityExtractionRun(Base, TenantScoped):
    """Marks a document as checked for entities at a given extractor_version,
    independent of how many (if any) were found.

    `entity_mention` alone can't answer "has this document been processed" — a
    document with zero real entities produces zero mention rows, which is
    indistinguishable from "never attempted" and would otherwise be re-queued by
    `find_unenriched_documents` forever, once per scheduled run, forever (see
    docs/DECISIONS.md). This table exists purely to make that distinction explicit.
    """

    __tablename__ = "entity_extraction_run"
    __table_args__ = (
        UniqueConstraint("document_id", "extractor_version", name="uq_extraction_run_doc_version"),
    )

    id: Mapped[uuid.UUID] = _pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), index=True
    )
    extractor_version: Mapped[str] = mapped_column(String(128), nullable=False)
    mention_count: Mapped[int] = mapped_column(Integer, nullable=False)
    processed_at: Mapped[dt.datetime] = _now()


# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------
class CreditUsageEvent(Base, TenantScoped):
    """One provider credit spend, logged at the moment it happens.

    Supadata has no endpoint that reports credit consumption back to the caller
    (confirmed against the live API — no `x-billable-requests` header, nothing in
    any response body). This table is the only durable record that exists; without
    it, "credits used" and "credits remaining" reset to zero every time a process
    restarts, because `CreditLedger` is in-memory only. `endpoint` and `external_id`
    are kept specifically so a spend spike can be traced to what caused it, not just
    counted.
    """

    __tablename__ = "credit_usage_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255))
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class IngestState(Base, TenantScoped):
    """Last-checked-at bookkeeping for a source, read by the heartbeat and ops
    tooling. Not the dedup mechanism — that rides on document's own unique
    constraint, which cannot drift out of sync with what was actually persisted
    the way a hand-maintained cursor could.
    """

    __tablename__ = "ingest_state"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_id", name="uq_ingest_state_tenant_source"),
    )

    id: Mapped[uuid.UUID] = _pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source.id", ondelete="CASCADE"), index=True
    )
    cursor: Mapped[dict | None] = mapped_column(JSONB)
    updated_at: Mapped[dt.datetime] = _now()


class Heartbeat(Base, TenantScoped):
    """Dead-man's switch. A job that failed silently and a job that never ran both
    look identical here — which is exactly the property log-scraping lacks.
    """

    __tablename__ = "heartbeat"
    __table_args__ = (UniqueConstraint("tenant_id", "flow_name", name="uq_heartbeat_flow"),)

    id: Mapped[uuid.UUID] = _pk()
    flow_name: Mapped[str] = mapped_column(String(128), nullable=False)
    last_success_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(Text)


class QueryLog(Base, TenantScoped):
    """What was asked of this corpus, and what it managed to answer.

    Exists to close the loop the corpus otherwise cannot: nothing else records that a
    question was asked and answered badly. `corpus_coverage` grades a topic on demand,
    but that verdict evaporates with the response — so "this corpus graded `thin` on
    robotics fourteen times" is a sourcing decision nobody could previously make.

    Two consumers, both in `corpus.analytics.query_insights`:

    * a **sourcing backlog** — repeated weak coverage on a topic is evidence of what
      to ingest next, which is where this corpus's real headroom is. Measured
      retrieval headroom is +/-0.05; the gaps are whole topics.
    * a **step-0 proxy** — every `thin`/`none` on a real query is a recorded instance
      of this corpus failing to earn its keep, with the query text attached. Weaker
      than the build plan's web-search baseline, but it accumulates automatically and
      it is honest (docs/BUILD_PLAN.md, docs/FEDERATION.md).

    **This is the most sensitive table here.** The transcripts are public; these rows
    are what *you* are investigating — client prep, prospect research, positioning.
    They stay under RLS like everything else, never leave `$PROJECT_DATA_DIR`, and
    logging is disabled by setting `CORPUS_LOG_QUERIES=false`. `purge_queries` deletes
    on demand.

    `answered_well` is deliberately nullable rather than defaulted: for a search we
    only know the result count, not whether it helped, and recording a guess as a
    fact is the failure mode this schema keeps refusing (cf. `is_auto_generated`).
    """

    __tablename__ = "query_log"

    id: Mapped[uuid.UUID] = _pk()
    tool: Mapped[str] = mapped_column(String(32), nullable=False)
    surface: Mapped[str] = mapped_column(String(16), nullable=False)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str | None] = mapped_column(String(32))

    result_count: Mapped[int | None] = mapped_column(Integer)
    top_document_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUID(as_uuid=True)))

    #: Set only by coverage calls: none/thin/partial/good.
    coverage_grade: Mapped[str | None] = mapped_column(String(16))
    #: Index completeness *at the time of the call* — without it a historical `none`
    #: cannot be told apart from "the index was half-built that day".
    indexed_documents: Mapped[int | None] = mapped_column(Integer)
    total_documents: Mapped[int | None] = mapped_column(Integer)

    latency_ms: Mapped[int | None] = mapped_column(Integer)
    answered_well: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[dt.datetime] = _now()


class PurgeLog(Base, TenantScoped):
    __tablename__ = "purge_log"

    id: Mapped[uuid.UUID] = _pk()
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    rows_deleted: Mapped[int | None] = mapped_column(BigInteger)
    performed_at: Mapped[dt.datetime] = _now()
