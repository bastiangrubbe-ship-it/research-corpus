"""YouTube source adapter.

Implements `SourceAdapter`. Two transcript providers, tried in an order that is a
deployment property rather than a preference:

* **On this laptop** youtube-transcript-api goes first, because it is the only source
  of `is_generated` and a residential IP is not blocked.
* **On the Linux server** it will be blocked, and Supadata becomes the only thing that
  works. The order inverts by configuration, not by a code change.

Metadata always comes from Supadata: youtube-transcript-api returns transcripts only,
and the YouTube Data API has quota costs this project will not pay.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence

import structlog

from corpus.db.enums import SourceKind
from corpus.sources.base import (
    FetchResult,
    InvalidRequest,
    NormalizedDocument,
    NormalizedTranscript,
    ProviderBlocked,
    RawResponse,
    SourceError,
    TranscriptUnavailable,
)
from corpus.sources.youtube.supadata import SupadataClient
from corpus.sources.youtube.ytapi import YtApiTranscriptClient

log = structlog.get_logger(__name__)


class YouTubeAdapter:
    kind = SourceKind.YOUTUBE_CHANNEL

    def __init__(
        self,
        *,
        supadata: SupadataClient | None = None,
        ytapi: YtApiTranscriptClient | None = None,
        provider_order: Sequence[str] = ("ytapi", "supadata"),
    ) -> None:
        self._supadata = supadata
        self._ytapi = ytapi
        self._order = tuple(provider_order)
        if not self._order:
            raise ValueError("provider_order must name at least one provider")

    # -- discovery ---------------------------------------------------------
    def discover(
        self,
        source_ref: str,
        *,
        limit: int | None = None,
        since: dt.datetime | None = None,
    ) -> Iterable[str]:
        """Video ids for a channel or playlist.

        `since` is advisory — Supadata's channel listing has no date filter, so the
        ingestion cursor does the real incremental work (step 3).
        """
        if self._supadata is None:
            raise SourceError("discovery requires a Supadata client")
        return self._supadata.list_channel_videos(source_ref, limit=limit)

    # -- fetch -------------------------------------------------------------
    def fetch(self, external_id: str, *, lang: str = "en") -> FetchResult:
        raws: list[RawResponse] = []
        document = NormalizedDocument(external_id=external_id)

        if self._supadata is not None:
            try:
                document, meta_raw = self._supadata.fetch_metadata(external_id)
                raws.append(meta_raw)
            except SourceError as exc:
                # Metadata failure is not fatal: a transcript with a thin document row
                # is still worth having, and the date can be backfilled later.
                log.warning("metadata_failed", video_id=external_id, error=str(exc))

        transcript, transcript_raws, error_code = self._fetch_transcript(external_id, lang=lang)
        raws.extend(transcript_raws)

        return FetchResult(
            document=document, transcript=transcript, raw=raws, error_code=error_code
        )

    def _fetch_transcript(
        self, video_id: str, *, lang: str
    ) -> tuple[NormalizedTranscript | None, list[RawResponse], str | None]:
        raws: list[RawResponse] = []
        last_error: str | None = None

        for name in self._order:
            client = self._client_for(name)
            if client is None:
                continue
            try:
                transcript, raw = client.fetch_transcript(video_id, lang=lang)
                raws.append(raw)
                log.info(
                    "transcript_fetched",
                    video_id=video_id,
                    provider=name,
                    segments=len(transcript.segments),
                    provenance=transcript.provenance_confidence.value,
                )
                return transcript, raws, None
            except TranscriptUnavailable as exc:
                # No provider will have it. Trying the next one only wastes a credit.
                log.info("transcript_unavailable", video_id=video_id, provider=name)
                return None, raws, f"transcript-unavailable: {exc}"
            except InvalidRequest as exc:
                # Our request was wrong. The next provider will reject it identically.
                log.warning("invalid_request", video_id=video_id, provider=name, error=str(exc))
                return None, raws, f"invalid-request: {exc}"
            except ProviderBlocked as exc:
                # This provider is refusing us; the next one may not be.
                log.warning("provider_blocked", video_id=video_id, provider=name, error=str(exc))
                last_error = f"provider-blocked: {exc}"
                continue
            except SourceError as exc:
                log.warning("provider_error", video_id=video_id, provider=name, error=str(exc))
                last_error = f"provider-error: {exc}"
                continue

        return None, raws, last_error or "no-provider-available"

    def _client_for(self, name: str) -> SupadataClient | YtApiTranscriptClient | None:
        if name == "supadata":
            return self._supadata
        if name == "ytapi":
            return self._ytapi
        raise ValueError(f"unknown provider {name!r}")
