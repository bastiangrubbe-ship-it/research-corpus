"""The source adapter contract.

This is the interface that must not need reworking when a source type is added. RSS
(step 10) is the real test of it: if `base.py` has to change to accommodate the second
source, that is the finding, and it is far cheaper to learn now than at source five.

The shape deliberately separates three things that are easy to conflate:

* `RawResponse` — exactly what the provider returned, destined for the bronze store
  unmodified. Never normalised, never trimmed.
* `NormalizedDocument` / `NormalizedTranscript` — our model of it, including explicit
  records of what the provider did *not* tell us.
* The adapter itself — stateless with respect to the database. Adapters fetch and
  normalise; they do not write. Persistence is the ingestion layer's job (step 3),
  which keeps adapters testable without a database.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from corpus.db.enums import DatePrecision, ProvenanceConfidence, SourceKind, TranscriptProvider


@dataclass(frozen=True, slots=True)
class RawResponse:
    """A provider response, verbatim. This is what lands in bronze.

    `payload` is stored as received. If a parse turns out to be wrong, the parser is
    fixed and the document re-derived — the raw response is never edited, because
    re-fetching is often impossible (rate limits, expired credentials, deleted
    upstream records, spent quota).
    """

    provider: str
    endpoint: str
    external_id: str
    fetched_at: dt.datetime
    payload: Any
    http_status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    request_params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NormalizedSegment:
    """A timestamped span. Offsets are milliseconds, always.

    Providers disagree on units — Supadata returns milliseconds as integers,
    youtube-transcript-api returns seconds as floats. Normalising at the adapter
    boundary means nothing downstream has to know or care which produced a row.
    """

    idx: int
    text: str
    offset_ms: int
    duration_ms: int


@dataclass(frozen=True, slots=True)
class NormalizedTranscript:
    provider: TranscriptProvider
    lang: str
    segments: Sequence[NormalizedSegment]
    available_langs: Sequence[str] = ()

    # None means the provider did not say. It does not mean "human-authored".
    # The paired confidence field records which of those two situations applies.
    is_auto_generated: bool | None = None
    provenance_confidence: ProvenanceConfidence = ProvenanceConfidence.UNKNOWN

    def __post_init__(self) -> None:
        # Mirrors the CHECK constraint on transcript_version. Enforcing it here too
        # means a bad adapter fails at the boundary with a clear message rather than
        # as an IntegrityError three layers away.
        known = self.provenance_confidence is not ProvenanceConfidence.UNKNOWN
        if known != (self.is_auto_generated is not None):
            raise ValueError(
                "is_auto_generated must be None exactly when provenance_confidence "
                f"is 'unknown'; got {self.is_auto_generated!r} / "
                f"{self.provenance_confidence.value!r}"
            )

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments)


@dataclass(frozen=True, slots=True)
class NormalizedDocument:
    external_id: str
    url: str | None = None
    title: str | None = None
    description: str | None = None
    duration_s: int | None = None

    # Publication date and how well we know it are separate facts. Trend analysis
    # over dates of unknown precision quietly lies.
    published_at: dt.datetime | None = None
    published_at_precision: DatePrecision = DatePrecision.UNKNOWN
    published_at_source: str | None = None  # api | parsed | inferred

    # Everything the provider gave us that we do not model as a column yet.
    # Cheap insurance: it lands in bronze anyway, but keeping it here means the
    # enrichment step does not have to re-read bronze to see it.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FetchResult:
    document: NormalizedDocument
    transcript: NormalizedTranscript | None
    raw: Sequence[RawResponse]
    #: Populated when a transcript could not be obtained. A document with no
    #: transcript is still worth storing — its metadata feeds entity analytics.
    error_code: str | None = None


@runtime_checkable
class SourceAdapter(Protocol):
    """What every source type must implement. Nothing more, nothing source-specific."""

    kind: SourceKind

    def discover(
        self,
        source_ref: str,
        *,
        limit: int | None = None,
        since: dt.datetime | None = None,
    ) -> Iterable[str]:
        """Yield external ids for a source (a channel, a playlist, a feed URL).

        `since` is advisory: providers that cannot filter by date return everything
        and the ingestion layer's cursor does the filtering.
        """
        ...

    def fetch(self, external_id: str, *, lang: str = "en") -> FetchResult:
        """Fetch one item. Raises only on unexpected failure.

        An item that simply has no transcript is a normal outcome and comes back as a
        FetchResult with `transcript=None` and an `error_code`.
        """
        ...


class SourceError(Exception):
    """Base class for adapter failures."""


class TranscriptUnavailable(SourceError):
    """No transcript exists for this item, in any language. Not retryable."""


class ProviderBlocked(SourceError):
    """The provider refused us — IP block, auth failure, quota exhausted.

    Distinct from TranscriptUnavailable on purpose: this one means *try the other
    provider*, whereas an unavailable transcript means no provider will have it.
    """


class CreditBudgetExceeded(SourceError):
    """Refused before spending: the call would exceed the configured credit budget."""
