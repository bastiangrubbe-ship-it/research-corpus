"""youtube-transcript-api client.

This is the **only** source of transcript provenance. Supadata's response has three
fields and none of them says whether captions were human-authored or machine-generated.
That single fact is why this provider exists at all, and why it is tried first from a
residential connection.

It will stop working on the Linux server: YouTube blocks cloud-provider IP ranges, and
the library raises RequestBlocked/IpBlocked there. That is expected and handled by
failover to Supadata rather than treated as an error to fix.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    CouldNotRetrieveTranscript,
    IpBlocked,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
)

from corpus.db.enums import ProvenanceConfidence, TranscriptProvider
from corpus.sources.base import (
    NormalizedSegment,
    NormalizedTranscript,
    ProviderBlocked,
    RawResponse,
    TranscriptUnavailable,
)

PROVIDER = "ytapi"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class YtApiTranscriptClient:
    def __init__(self, api: YouTubeTranscriptApi | None = None) -> None:
        self._api = api or YouTubeTranscriptApi()

    def fetch_transcript(
        self, video_id: str, *, lang: str = "en"
    ) -> tuple[NormalizedTranscript, RawResponse]:
        """Fetch a transcript, preferring human-authored captions over generated ones.

        A video commonly has *both* an 'en' manual and an 'en' auto-generated track —
        `dQw4w9WgXcQ` is one. Selecting by language code alone silently picks whichever
        the API lists first, so a corpus built that way would contain a mix of
        human and machine captions all labelled identically. Provenance has to be part
        of the selection key, not just something recorded afterwards.
        """
        try:
            listing = self._api.list(video_id)
        except (RequestBlocked, IpBlocked) as exc:
            raise ProviderBlocked(f"youtube blocked this IP for {video_id}") from exc
        except (TranscriptsDisabled, VideoUnavailable) as exc:
            raise TranscriptUnavailable(f"{video_id}: {type(exc).__name__}") from exc
        except CouldNotRetrieveTranscript as exc:
            raise TranscriptUnavailable(f"{video_id}: {type(exc).__name__}") from exc

        candidates = list(listing)
        available = sorted({t.language_code for t in candidates})

        chosen = self._select(candidates, lang)
        if chosen is None:
            raise TranscriptUnavailable(f"{video_id}: no transcript in {lang!r} (have {available})")

        try:
            fetched = chosen.fetch()
        except (RequestBlocked, IpBlocked) as exc:
            raise ProviderBlocked(f"youtube blocked this IP for {video_id}") from exc
        except CouldNotRetrieveTranscript as exc:
            raise TranscriptUnavailable(f"{video_id}: {type(exc).__name__}") from exc

        raw_snippets: list[dict[str, Any]] = fetched.to_raw_data()

        raw = RawResponse(
            provider=PROVIDER,
            endpoint="list+fetch",
            external_id=video_id,
            fetched_at=_now(),
            payload={
                "snippets": raw_snippets,
                "language_code": chosen.language_code,
                "language": chosen.language,
                "is_generated": chosen.is_generated,
                "available": [
                    {"language_code": t.language_code, "is_generated": t.is_generated}
                    for t in candidates
                ],
            },
            request_params={"video_id": video_id, "lang": lang},
        )

        transcript = NormalizedTranscript(
            provider=TranscriptProvider.YTAPI,
            lang=chosen.language_code,
            segments=_to_segments(raw_snippets),
            available_langs=available,
            # The whole point of this provider: a real answer, not a guess.
            is_auto_generated=bool(chosen.is_generated),
            provenance_confidence=ProvenanceConfidence.KNOWN,
        )
        return transcript, raw

    @staticmethod
    def _select(candidates: list[Any], lang: str) -> Any | None:
        """Exact language + manual > exact language + generated > any manual > any."""
        exact = [t for t in candidates if t.language_code == lang]
        for pool in (exact, candidates):
            manual = [t for t in pool if not t.is_generated]
            if manual:
                return manual[0]
            if pool:
                return pool[0]
        return None


def _to_segments(snippets: list[dict[str, Any]]) -> list[NormalizedSegment]:
    """Seconds (float) to milliseconds (int).

    This library reports seconds; Supadata reports milliseconds. Getting this wrong
    would put every ytapi timestamp 1000x off, which is exactly the kind of error that
    looks fine in a spot check and ruins every temporal query.
    """
    return [
        NormalizedSegment(
            idx=i,
            text=s["text"],
            offset_ms=int(round(float(s["start"]) * 1000)),
            duration_ms=int(round(float(s.get("duration", 0.0)) * 1000)),
        )
        for i, s in enumerate(snippets)
    ]
