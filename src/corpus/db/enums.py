"""Enumerations backed by native Postgres enum types.

Several of these encode *uncertainty* rather than fact — `ProvenanceConfidence`,
`DatePrecision`, `AttributionMethod`. That is deliberate. The metadata layer is the
bottleneck for every high-value capability, and the honest failure mode is to record
what is unknown rather than to default it to something plausible.
"""

from enum import StrEnum


class SourceKind(StrEnum):
    YOUTUBE_CHANNEL = "youtube_channel"
    YOUTUBE_PLAYLIST = "youtube_playlist"
    RSS = "rss"
    ARXIV = "arxiv"
    WEB = "web"


class Domain(StrEnum):
    """What a source is fundamentally about, orthogonal to how authoritative it is.

    A vendor demo and a personal-development channel can both carry the same
    authority_tier (both 'practitioner', say) while meaning entirely different things
    for analytics: "mentioned 200 times" is a saturation signal for a tool name and
    noise for a self-help phrase. Without this column those two counts blend into one
    meaningless number. Analytics filter by domain by default; cross-domain queries
    (e.g. "which founder-tier ideas showed up later in adoption content") opt in
    explicitly rather than happening by accident.
    """

    AI_RESEARCH = "ai_research"
    AI_AUTOMATION = "ai_automation"
    ENTREPRENEURSHIP = "entrepreneurship"
    PERSONAL_DEVELOPMENT = "personal_development"
    REGULATORY = "regulatory"
    # Education/documentary/business content that doesn't fit the four analytical
    # domains above, kept on request rather than dropped as out_of_scope — see
    # docs/DECISIONS.md, 2026-08-24.
    GENERAL = "general"
    UNKNOWN = "unknown"


class SourceStatus(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"  # hidden from retrieval, rows retained
    PURGED = "purged"  # content deleted, tombstone retained


class AuthorityTier(StrEnum):
    """A podcast and a peer-reviewed paper must not carry identical weight.

    Curated by hand. The set of sources is small enough that this is tractable and
    far more reliable than anything inferred.
    """

    PEER_REVIEWED = "peer_reviewed"
    PRIMARY_REGULATORY = "primary_regulatory"
    VENDOR_OFFICIAL = "vendor_official"
    ESTABLISHED_MEDIA = "established_media"
    PRACTITIONER = "practitioner"
    AGGREGATOR = "aggregator"
    UNKNOWN = "unknown"


class TranscriptProvider(StrEnum):
    SUPADATA = "supadata"
    YTAPI = "ytapi"  # youtube-transcript-api; the only source of is_generated
    WHISPER_LOCAL = "whisper_local"
    RESTORED = "restored"  # punctuation/truecasing output, derived from a parent
    NATIVE = "native"  # source published real text (RSS, arXiv)


class ProvenanceConfidence(StrEnum):
    KNOWN = "known"  # provider told us explicitly
    INFERRED = "inferred"  # deduced, e.g. Supadata HTTP 202 implying the Whisper path
    UNKNOWN = "unknown"  # Supadata's normal case: it simply does not say


class DatePrecision(StrEnum):
    """How well we know publication date. Trend analysis lies without this."""

    EXACT = "exact"  # timestamp from the API
    DATE = "date"  # day known, time not
    MONTH = "month"
    YEAR = "year"
    INFERRED = "inferred"  # derived from surrounding evidence
    UNKNOWN = "unknown"


class AttributionMethod(StrEnum):
    """How a speaker was identified. Neither transcript API provides attribution,
    so everything except DIARIZED is a heuristic and is marked as one.
    """

    DIARIZED = "diarized"  # tier 2: pyannote + WhisperX alignment
    CHANNEL_DEFAULT = "channel_default"  # tier 1: solo content, channel owner
    TITLE_PARSED = "title_parsed"  # tier 1: guest name from title
    DESCRIPTION_PARSED = "description_parsed"  # tier 1: guest name from description
    UNKNOWN = "unknown"


class EntityKind(StrEnum):
    VENDOR = "vendor"
    PERSON = "person"
    TECHNIQUE = "technique"
    REGULATION = "regulation"
    PRODUCT = "product"
    ORGANIZATION = "organization"


class DocumentStatus(StrEnum):
    PENDING = "pending"
    ENRICHED = "enriched"
    FAILED = "failed"
    PURGED = "purged"


class SummaryMethod(StrEnum):
    EXTRACTIVE_TEXTRANK = "extractive_textrank"
    ABSTRACTIVE_LLM = "abstractive_llm"  # on-demand only, never a corpus-wide pass
