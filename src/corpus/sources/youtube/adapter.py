"""YouTube source adapter.

Implements `SourceAdapter`. Discovery and metadata are separate concerns from
transcript text, and use different providers for it:

* **Discovery and metadata** come from yt-dlp (`ytdlp_meta.py`) — free, no credits.
  It returns strictly more than Supadata's metadata endpoint did (an exact upload
  timestamp, tags, and the subtitles/automatic_captions split) and costs nothing.
  See docs/DECISIONS.md.
* **Transcript text** comes from two providers, tried in an order that is a
  deployment property rather than a preference: youtube-transcript-api first on
  this laptop, because it is the only source of `is_generated` and a residential IP
  is not blocked; Supadata first on the eventual Linux server, where YouTube blocks
  cloud IPs and ytapi fails. The order inverts by configuration, not by a code
  change.
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
from corpus.sources.youtube.ytdlp_meta import YtDlpMetadataClient

log = structlog.get_logger(__name__)


class YouTubeAdapter:
    kind = SourceKind.YOUTUBE_CHANNEL

    def __init__(
        self,
        *,
        metadata: YtDlpMetadataClient | None = None,
        supadata: SupadataClient | None = None,
        ytapi: YtApiTranscriptClient | None = None,
        provider_order: Sequence[str] = ("ytapi", "supadata"),
    ) -> None:
        self._metadata = metadata or YtDlpMetadataClient()
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
        """Video ids from a channel's /videos tab. Free, via yt-dlp.

        `since` is advisory — yt-dlp's channel listing has no date filter, so the
        real incremental work is `document`'s own unique constraint (step 3): a
        video already ingested is simply skipped rather than re-fetched.
        """
        return self._metadata.discover_channel_videos(source_ref, limit=limit)

    # -- fetch -------------------------------------------------------------
    def fetch(self, external_id: str, *, lang: str = "en") -> FetchResult:
        raws: list[RawResponse] = []
        document = NormalizedDocument(external_id=external_id)

        try:
            document, meta_raw = self._metadata.fetch_metadata(external_id)
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
