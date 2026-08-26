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
from concurrent.futures import ThreadPoolExecutor

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
        # Populated by discover() when it has a channel handle to call, consumed
        # by fetch_batch()'s metadata_source="skip" path so the free title/
        # duration/view_count the channel listing already carries isn't thrown
        # away. Per-adapter-instance state, not thread-safe across channels —
        # fine today since concurrency>1 is refused for "skip" the same as for
        # "ytdlp" isn't required... but see the note on fetch_batch: this cache
        # is keyed by video id, not channel, so concurrent channels sharing one
        # adapter would still resolve correctly by id; only a real *race* to
        # populate it concurrently could interleave harmlessly since each
        # channel writes disjoint ids. Still, treat this as a single-channel-
        # at-a-time convenience, not a general-purpose cache.
        self._last_discovery_details: dict[str, dict] = {}

    # -- discovery ---------------------------------------------------------
    def discover(
        self,
        source_ref: str,
        *,
        limit: int | None = None,
        since: dt.datetime | None = None,
    ) -> Iterable[str]:
        """Video ids from a channel's /videos tab. Free, via yt-dlp.

        `since`, when given, takes over from `limit`: the channel listing itself has
        no date filter, so this walks the most recent candidates and checks each
        one's real publish date (see `YtDlpMetadataClient.discover_since`) rather
        than returning a fixed count. `document`'s own unique constraint (step 3)
        still does the incremental work either way — a video already ingested is
        simply skipped rather than re-fetched.

        Uses the `_detailed` listing call (same cost as the plain one — one flat-
        playlist fetch either way) so title/duration/view_count that the channel
        page already carries are cached for `fetch_batch`'s `metadata_source="skip"`
        to use, rather than thrown away. Only when `since` is unset: the since-walk
        path calls per-video metadata anyway to check dates, so there's nothing to
        cache there that isn't already the real thing.
        """
        if since is not None:
            return self._metadata.discover_since(source_ref, since=since)
        detailed = self._metadata.discover_channel_videos_detailed(source_ref, limit=limit)
        self._last_discovery_details.update({e["id"]: e for e in detailed if e.get("id")})
        return [e["id"] for e in detailed if e.get("id")]

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

    # -- batch fetch (Supadata only) ----------------------------------------
    def fetch_batch(
        self,
        external_ids: list[str],
        *,
        lang: str = "en",
        metadata_source: str = "ytdlp",
    ) -> dict[str, FetchResult]:
        """One Supadata batch job for the transcripts of every id.

        `metadata_source`:
        - `"ytdlp"` (default): free, exact upload timestamp, one call per video —
          kept sequential (see `_fetch_all_metadata_ytdlp`) since bulk concurrent
          calls to this endpoint tripped YouTube's bot detection earlier (see
          docs/DECISIONS.md). This means metadata is the effective floor on how
          fast many channels can run concurrently, since it's the one part of
          this method still touching yt-dlp.
        - `"supadata"`: a second Supadata batch job, ~1 credit/video, date-only
          precision (`uploadDate`'s time component is always midnight — confirmed
          empirically, see docs/SUPADATA.md). Removes yt-dlp from this method
          entirely, which is what makes running many channels concurrently safe —
          nothing left here touches the scraping-fragile endpoint. In practice
          this and channel-level concurrency together didn't speed anything up
          (see docs/DECISIONS.md, 2026-08-24) — the bottleneck turned out to be
          server-side at Supadata, not client-side request rate.
        - `"skip"`: no metadata *call* at all, but not fully empty — `discover()`
          caches the title/duration/view_count the channel listing already
          carries for free (see `_last_discovery_details`), so those land on the
          document; `description` and `published_at` stay unset until a separate
          backfill pass fills them in later (trivially findable:
          `published_at IS NULL`). Fastest, and costs nothing beyond the
          transcript batch itself.

        Does not participate in the ytapi/Supadata failover chain `fetch()` uses:
        there is no batch equivalent for ytapi, and this is an explicit choice for
        bulk speed over the per-video `mode="native"` cost guarantee (see
        docs/SUPADATA.md, docs/DECISIONS.md — batch silently ignores `mode`).

        Transcript and metadata are independent, so they run concurrently (2
        threads) rather than one after the other regardless of `metadata_source`.
        """
        if self._supadata is None:
            raise ValueError("fetch_batch requires a configured Supadata client")
        if not external_ids:
            return {}
        if metadata_source not in ("ytdlp", "supadata", "skip"):
            raise ValueError(f"unknown metadata_source {metadata_source!r}")

        if metadata_source == "skip":
            transcripts, error_codes, batch_raw = self._supadata.fetch_transcripts_batch(
                external_ids, lang=lang
            )
            metadata_result = None
        else:
            with ThreadPoolExecutor(max_workers=2) as pool:
                transcript_future = pool.submit(
                    self._supadata.fetch_transcripts_batch, external_ids, lang=lang
                )
                if metadata_source == "supadata":
                    metadata_job_future = pool.submit(
                        self._supadata.fetch_metadata_batch, external_ids
                    )
                else:
                    metadata_job_future = pool.submit(self._fetch_all_metadata_ytdlp, external_ids)
                transcripts, error_codes, batch_raw = transcript_future.result()
                metadata_result = metadata_job_future.result()

        log.info(
            "batch_fetched",
            requested=len(external_ids),
            succeeded=len(transcripts),
            failed=len(error_codes),
            metadata_source=metadata_source,
        )

        # Normalize every metadata_source branch into the same per-video shape
        # (document + its own metadata raw responses) so the results loop below
        # doesn't need to know which source produced them.
        documents_by_id: dict[str, NormalizedDocument] = {}
        metadata_raws_by_id: dict[str, list[RawResponse]] = {}
        if metadata_source == "supadata":
            documents_by_id, _meta_error_codes, shared_meta_raw = metadata_result
            metadata_raws_by_id = {vid: [shared_meta_raw] for vid in external_ids}
        elif metadata_source == "ytdlp":
            for video_id, meta in metadata_result.items():
                if meta is not None:
                    document, meta_raw = meta
                    documents_by_id[video_id] = document
                    metadata_raws_by_id[video_id] = [meta_raw]
        elif metadata_source == "skip":
            # No metadata call at all, but discover() already cached whatever the
            # channel listing carried for free (title/duration/view_count, no
            # date) — use it rather than leaving every document fully empty.
            for video_id in external_ids:
                cached = self._last_discovery_details.get(video_id)
                if cached:
                    documents_by_id[video_id] = NormalizedDocument(
                        external_id=video_id,
                        url=f"https://www.youtube.com/watch?v={video_id}",
                        title=cached.get("title"),
                        duration_s=cached.get("duration"),
                        extra={"view_count": cached.get("view_count")},
                    )

        results: dict[str, FetchResult] = {}
        for video_id in external_ids:
            document = documents_by_id.get(video_id, NormalizedDocument(external_id=video_id))
            raws = [batch_raw, *metadata_raws_by_id.get(video_id, [])]

            transcript = transcripts.get(video_id)
            error_code = None
            if transcript is None:
                error_code = error_codes.get(video_id, "not-in-batch-results")
            results[video_id] = FetchResult(
                document=document, transcript=transcript, raw=raws, error_code=error_code
            )
        return results

    def _fetch_all_metadata_ytdlp(
        self, external_ids: list[str]
    ) -> dict[str, tuple[NormalizedDocument, RawResponse] | None]:
        out: dict[str, tuple[NormalizedDocument, RawResponse] | None] = {}
        for vid in external_ids:
            try:
                out[vid] = self._metadata.fetch_metadata(vid)
            except SourceError as exc:
                log.warning("metadata_failed", video_id=vid, error=str(exc))
                out[vid] = None
        return out
