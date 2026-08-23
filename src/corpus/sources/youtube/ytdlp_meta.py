"""yt-dlp for discovery and metadata. Free — no credits, no auth.

Supadata is reserved for transcript text only (see docs/DECISIONS.md — "yt-dlp for
discovery and metadata, Supadata for transcript text only"). This module is what
actually implements that split; discover() and fetch()'s metadata step in adapter.py
call into here rather than Supadata's /youtube/channel/videos and /youtube/video
endpoints, which would spend a credit per call for data available for free.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess

from corpus.db.enums import DatePrecision
from corpus.sources.base import (
    NormalizedDocument,
    ProviderBlocked,
    RawResponse,
    TranscriptUnavailable,
)

PROVIDER = "yt-dlp"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _run(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


class YtDlpMetadataClient:
    """Channel discovery and per-video metadata via yt-dlp subprocess calls.

    Deliberately not the transcript path — yt-dlp throttles hard under bulk load in
    a way a paid API does not, which is exactly why the high-volume transcript fetch
    stays on Supadata/ytapi. Discovery and metadata are comparatively cheap, one call
    per channel or per video, so the throttling risk is far smaller here.
    """

    def __init__(self, *, socket_timeout: float = 20.0, process_timeout: float = 90.0) -> None:
        self._socket_timeout = socket_timeout
        self._process_timeout = process_timeout

    def discover_channel_videos(self, handle: str, *, limit: int | None = None) -> list[str]:
        """Video ids from a channel's /videos tab specifically.

        Never the channel's total video count and never /shorts — Shorts carry no
        argument or datable position worth retrieving, and their share of a channel
        varies from 0% to 89%, so using a combined count would silently spend credits
        on content that degrades every downstream capability. See docs/DECISIONS.md.
        """
        url = f"https://www.youtube.com/{handle.lstrip('@')}/videos"
        if handle.startswith("@"):
            url = f"https://www.youtube.com/{handle}/videos"
        args = [
            "yt-dlp",
            "--flat-playlist",
            "--print",
            "%(id)s",
            "--no-warnings",
            "--socket-timeout",
            str(self._socket_timeout),
        ]
        if limit is not None:
            args += ["--playlist-end", str(limit)]
        args.append(url)

        proc = _run(args, timeout=self._process_timeout)
        if proc.returncode != 0:
            raise ProviderBlocked(f"yt-dlp discovery failed for {handle}: {proc.stderr[:200]}")
        ids = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        return ids[:limit] if limit else ids

    def fetch_metadata(self, video_id: str) -> tuple[NormalizedDocument, RawResponse]:
        url = f"https://www.youtube.com/watch?v={video_id}"
        proc = _run(
            [
                "yt-dlp",
                "--skip-download",
                "--dump-single-json",
                "--no-warnings",
                "--socket-timeout",
                str(self._socket_timeout),
                url,
            ],
            timeout=self._process_timeout,
        )
        if proc.returncode != 0:
            raise TranscriptUnavailable(
                f"yt-dlp metadata failed for {video_id}: {proc.stderr[:200]}"
            )

        payload = json.loads(proc.stdout)
        raw = RawResponse(
            provider=PROVIDER,
            endpoint="metadata",
            external_id=video_id,
            fetched_at=_now(),
            payload=_trim(payload),
            request_params={"video_id": video_id},
        )
        return _to_document(video_id, payload), raw


def _to_document(video_id: str, payload: dict) -> NormalizedDocument:
    published_at: dt.datetime | None = None
    precision = DatePrecision.UNKNOWN
    source = None

    timestamp = payload.get("timestamp")
    if timestamp:
        published_at = dt.datetime.fromtimestamp(timestamp, tz=dt.UTC)
        precision = DatePrecision.EXACT
        source = "api"
    elif payload.get("upload_date"):
        try:
            published_at = dt.datetime.strptime(payload["upload_date"], "%Y%m%d").replace(
                tzinfo=dt.UTC
            )
            precision = DatePrecision.DATE
            source = "api"
        except ValueError:
            pass

    return NormalizedDocument(
        external_id=payload.get("id") or video_id,
        url=f"https://www.youtube.com/watch?v={payload.get('id') or video_id}",
        title=payload.get("title"),
        description=payload.get("description"),
        duration_s=payload.get("duration"),
        published_at=published_at,
        published_at_precision=precision,
        published_at_source=source,
        extra={
            "channel_id": payload.get("channel_id"),
            "channel_name": payload.get("channel"),
            "tags": payload.get("tags"),
            "view_count": payload.get("view_count"),
            "like_count": payload.get("like_count"),
            # The closest thing either provider gives to transcript provenance:
            # which languages have a manual track vs only an auto-generated one.
            "manual_subtitle_langs": sorted((payload.get("subtitles") or {}).keys()),
            "auto_caption_langs": sorted((payload.get("automatic_captions") or {}).keys()),
        },
    )


def _trim(payload: dict) -> dict:
    """yt-dlp's per-video JSON includes every format/thumbnail variant, which bloats
    the bronze record with data nothing downstream reads. Keep the fields the
    normalizer and any future re-derivation actually use.
    """
    keep = (
        "id",
        "title",
        "description",
        "duration",
        "channel",
        "channel_id",
        "uploader_id",
        "timestamp",
        "upload_date",
        "view_count",
        "like_count",
        "comment_count",
        "tags",
        "categories",
        "language",
        "availability",
        "subtitles",
        "automatic_captions",
    )
    return {k: payload.get(k) for k in keep if k in payload}
