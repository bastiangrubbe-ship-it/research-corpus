"""Channel discovery over YouTube's per-channel RSS feed, instead of yt-dlp.

    https://www.youtube.com/feeds/videos.xml?channel_id=UC...

Why: the nightly job discovers with yt-dlp, one channel at a time, enumerating each
channel's entire back catalogue. Measured, a single RSS fetch is ~0.11s of plain HTTP
against a yt-dlp subprocess that takes seconds, returns 15 recent entries instead of
thousands, parallelises freely, and does not trip YouTube's bot detection — which a
burst of ~90 yt-dlp calls demonstrably does (docs/DECISIONS.md, 2026-08-27).

For a *nightly* pass, 15 recent videos per channel is the right window rather than a
limitation: the job only needs what appeared since it last ran. Initial backfill of a
new channel still needs yt-dlp, which is why this supplements discovery rather than
replacing it.

**The Shorts caveat, which is the reason this is not simply the default.**
`ytdlp_meta.discover_channel_videos` hits a channel's `/videos` tab specifically and
never `/shorts`, because Shorts are 0-89% of a channel and spending credits on them
degrades every downstream capability (docs/DECISIONS.md). **RSS makes no such
distinction and carries no duration field**, so it returns Shorts mixed in with real
uploads and cannot filter them by itself.

Nothing downstream currently filters by duration either: the Shorts exclusion lives
entirely in the choice of discovery URL. So switching the nightly to RSS without a
duration guard would quietly start ingesting Shorts corpus-wide. `adapter.fetch`
resolves metadata (including `duration_s`) *before* the transcript call that spends a
credit, so the guard belongs there — see `SHORTS_MAX_DURATION_S`.
"""

from __future__ import annotations

import datetime as dt
import subprocess
from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger(__name__)

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

#: A YouTube Short is at most 3 minutes. Anything at or under this from an RSS-derived
#: candidate is presumed a Short and must not have a credit spent on it. Deliberately
#: a presumption and not a certainty: a genuinely short real upload is caught by it.
#: That trade is the right way round — the recorded decision is that Shorts degrade
#: downstream analytics, and a handful of missed 3-minute uploads costs far less than
#: a corpus-wide Shorts intake.
SHORTS_MAX_DURATION_S = 180

#: The feed always returns the most recent ~15 uploads and offers no paging. A channel
#: that posted more than this since the last run will have older items missed, which is
#: exactly why `run_ingestion` must keep yt-dlp available for backfill.
FEED_ENTRY_LIMIT = 15


@dataclass(frozen=True, slots=True)
class FeedVideo:
    video_id: str
    published_at: dt.datetime
    title: str


class ChannelIdUnresolved(Exception):
    """The @handle could not be turned into a UC... channel id."""


def resolve_channel_id(handle: str, *, timeout_s: float = 60.0) -> str:
    """@handle -> UC... channel id, via one yt-dlp call.

    Callers should cache the result: a channel id never changes, so this is a one-time
    cost per source and the whole point is to avoid yt-dlp on the recurring path.
    """
    handle = handle if handle.startswith("@") else f"@{handle}"
    try:
        proc = subprocess.run(
            [
                "yt-dlp",
                "--no-warnings",
                "--playlist-end",
                "1",
                "--print",
                "%(channel_id)s",
                f"https://www.youtube.com/{handle}/videos",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise ChannelIdUnresolved(f"{handle}: timed out") from exc

    channel_id = (proc.stdout or "").strip().splitlines()
    channel_id = channel_id[0].strip() if channel_id else ""
    if not channel_id.startswith("UC"):
        raise ChannelIdUnresolved(f"{handle}: got {channel_id!r} (stderr: {proc.stderr[:120]})")
    return channel_id


def discover_via_rss(
    channel_id: str,
    *,
    since: dt.datetime | None = None,
    client: httpx.Client | None = None,
    timeout_s: float = 20.0,
) -> list[FeedVideo]:
    """Recent uploads for one channel, newest first.

    `since` filters on the feed's own `published` value, so an incremental run does no
    work for channels that posted nothing. Returns [] rather than raising when the feed
    is empty or malformed — a channel that has gone quiet is not an error, and a single
    bad feed must not stop a 169-channel pass.

    **Results may include Shorts** (see the module docstring); the caller is
    responsible for the duration guard before spending anything on them.
    """
    import feedparser

    url = FEED_URL.format(channel_id=channel_id)
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout_s, follow_redirects=True)
    try:
        response = client.get(url)
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
    except Exception as exc:
        log.warning("rss_discovery_failed", channel_id=channel_id, error=str(exc))
        return []
    finally:
        if owns_client:
            client.close()

    videos: list[FeedVideo] = []
    for entry in parsed.entries[:FEED_ENTRY_LIMIT]:
        video_id = entry.get("yt_videoid") or entry.get("id", "").rsplit(":", 1)[-1]
        published = entry.get("published_parsed")
        if not video_id or not published:
            continue
        published_at = dt.datetime(*published[:6], tzinfo=dt.UTC)
        if since is not None and published_at < since:
            continue
        videos.append(
            FeedVideo(video_id=video_id, published_at=published_at, title=entry.get("title", ""))
        )
    return videos


def is_probable_short(duration_s: int | None) -> bool:
    """True when a candidate should not have a credit spent on it.

    `None` returns False: an unknown duration is not evidence of a Short, and refusing
    to fetch on missing metadata would silently drop real uploads. Consistent with the
    schema's refusal to coalesce unknown into a value.
    """
    if duration_s is None:
        return False
    return duration_s <= SHORTS_MAX_DURATION_S
