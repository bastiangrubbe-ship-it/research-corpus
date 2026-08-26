"""RSS source adapter — the real test of `sources/base.py`'s interface (step 10).

**Finding: `base.py` needed no changes.** RSS's actual shape is a genuine mismatch
from YouTube's — a feed is parsed once and every item's content is already present,
there is no per-item network fetch the way a video's transcript needs one — but the
existing `discover()` + per-id `fetch()` Protocol still fits, using the exact caching
pattern `YouTubeAdapter` already established for an unrelated reason (avoiding a
second metadata call when discovery already carried the data, see
`_last_discovery_details`). `discover()` parses the feed and caches each entry by
its guid; `fetch()` looks the entry up from that cache rather than making a second
network call. The interface survived contact with a second, structurally different
source without needing to change — which is exactly what step 10 exists to test.

RSS content is real, human-authored text — `TranscriptProvider.NATIVE`, not an ASR
provider — so `is_auto_generated=False` and `provenance_confidence=KNOWN` rather than
the YouTube adapter's frequent `None`/`unknown`. A genuinely higher-provenance-
confidence source than anything else in this corpus so far.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

import feedparser
import structlog

from corpus.db.enums import DatePrecision, ProvenanceConfidence, SourceKind, TranscriptProvider
from corpus.sources.base import (
    FetchResult,
    NormalizedDocument,
    NormalizedSegment,
    NormalizedTranscript,
    RawResponse,
    TranscriptUnavailable,
)

log = structlog.get_logger(__name__)


def _entry_published_at(entry) -> dt.datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    return dt.datetime(*parsed[:6], tzinfo=dt.UTC)


def _entry_content(entry) -> str:
    """Prefer full content (content:encoded, common on blog/podcast feeds) over
    the summary — the summary is often just a truncated teaser."""
    content = entry.get("content")
    if content:
        return content[0].get("value", "")
    return entry.get("summary", "")


def _entry_external_id(entry) -> str | None:
    return entry.get("id") or entry.get("guid") or entry.get("link")


class RssAdapter:
    kind = SourceKind.RSS

    def __init__(self) -> None:
        # Populated by discover(), consumed by fetch() — the same "discovery
        # already has the data, don't throw it away and re-fetch" pattern
        # YouTubeAdapter uses, here load-bearing rather than an optimization:
        # there is no other way to fetch a single RSS entry after the fact.
        self._entries: dict[str, object] = {}
        self._feed_url: str | None = None

    def discover(
        self,
        source_ref: str,
        *,
        limit: int | None = None,
        since: dt.datetime | None = None,
    ) -> Iterable[str]:
        self._feed_url = source_ref
        parsed = feedparser.parse(source_ref)
        entries = parsed.entries

        if since is not None:
            entries = [e for e in entries if (_entry_published_at(e) or since) >= since]
        if limit is not None:
            entries = entries[:limit]

        external_ids = []
        for entry in entries:
            external_id = _entry_external_id(entry)
            if not external_id:
                continue
            self._entries[external_id] = entry
            external_ids.append(external_id)
        return external_ids

    def fetch(self, external_id: str, *, lang: str = "en") -> FetchResult:
        entry = self._entries.get(external_id)
        if entry is None:
            raise TranscriptUnavailable(
                f"no cached entry for {external_id!r} — discover() must run first, RSS has no "
                "per-item endpoint to fetch from directly"
            )

        published_at = _entry_published_at(entry)
        document = NormalizedDocument(
            external_id=external_id,
            url=entry.get("link"),
            title=entry.get("title"),
            description=entry.get("summary"),
            published_at=published_at,
            published_at_precision=DatePrecision.EXACT if published_at else DatePrecision.UNKNOWN,
            published_at_source="api" if published_at else None,
        )

        content = _entry_content(entry)
        transcript = None
        error_code = None
        if content.strip():
            transcript = NormalizedTranscript(
                provider=TranscriptProvider.NATIVE,
                lang=lang,
                segments=[NormalizedSegment(idx=0, text=content, offset_ms=0, duration_ms=0)],
                is_auto_generated=False,
                provenance_confidence=ProvenanceConfidence.KNOWN,
            )
        else:
            error_code = "no-content-in-entry"

        raw = RawResponse(
            provider="rss",
            endpoint=self._feed_url or "",
            external_id=external_id,
            fetched_at=dt.datetime.now(dt.UTC),
            payload=dict(entry),
        )
        return FetchResult(
            document=document, transcript=transcript, raw=[raw], error_code=error_code
        )
