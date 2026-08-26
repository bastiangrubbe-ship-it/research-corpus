"""RSS feed seed reads and writes — the parallel of `corpus.web.seeds` for feeds.

Every write goes through `seeds/rss_feeds.yaml`, never straight into the database,
for the reason `seeds.py` states for YouTube channels: a dashboard action that
bypassed the reviewed, git-tracked file would create a second source of truth with no
diff history and no review step. Ingestion reads confirmed rows from here.

One real difference from the YouTube path, and it's about failure modes rather than
taste. `seeds.resolve_input` validates a channel by *raising* when yt-dlp can't
resolve it. `feedparser.parse` does not raise on a typo'd or dead URL — it returns an
object with zero entries and a `bozo` flag, which is indistinguishable from a valid
but currently-empty feed unless you look. `preview_feed` exists to make that
difference visible before anything is written.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import feedparser
import yaml

from corpus.ingest.runner import SEED_PATH

RSS_SEED_PATH = SEED_PATH.parent / "rss_feeds.yaml"


class RssSeedError(Exception):
    """The feed couldn't be used. Shown to the operator verbatim — a UI validation
    error, not an internal failure."""


@dataclass(frozen=True, slots=True)
class FeedPreview:
    url: str
    title: str
    entry_count: int
    sample_titles: list[str]
    #: True when the feed has entries but they carry no content beyond a link — a
    #: link-aggregator feed (Hacker News, for instance). Worth surfacing: it will
    #: ingest successfully and produce documents with almost no text.
    link_only: bool


def load_feeds() -> list[dict[str, Any]]:
    if not RSS_SEED_PATH.exists():
        return []
    data = yaml.safe_load(RSS_SEED_PATH.read_text()) or {}
    return data.get("feeds") or []


def preview_feed(url: str, *, limit: int = 5) -> FeedPreview:
    """Parse a feed without writing anything. Raises `RssSeedError` where
    `feedparser` would silently return an empty result."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise RssSeedError(f"{url!r} is not an http(s) URL")

    parsed = feedparser.parse(url)
    entries = parsed.entries or []
    if not entries:
        # feedparser sets `bozo` for malformed feeds but also for many harmless
        # quirks, so it isn't reliable on its own — no entries is the signal that
        # actually matters to a caller.
        reason = getattr(parsed, "bozo_exception", None)
        raise RssSeedError(
            f"no entries found at {url!r}"
            + (f" ({type(reason).__name__}: {reason})" if reason else "")
            + " — check the URL; feedparser returns empty rather than failing on a bad one"
        )

    def content_of(entry) -> str:
        content = entry.get("content")
        return content[0].get("value", "") if content else entry.get("summary", "")

    bodies = [content_of(e) for e in entries[:limit]]
    return FeedPreview(
        url=url,
        title=(parsed.feed or {}).get("title") or url,
        entry_count=len(entries),
        sample_titles=[e.get("title") or "(untitled)" for e in entries[:limit]],
        link_only=all(len(b) < 400 for b in bodies if b is not None),
    )


def append_feed(preview: FeedPreview, *, domain: str, authority_tier: str) -> dict[str, Any]:
    """Append one feed to the YAML. Refuses duplicates by URL.

    Appends only the new row's own block rather than re-serializing the file, the
    same discipline `seeds._append_row` uses — it keeps diffs to exactly what
    changed, so review stays cheap.
    """
    existing = load_feeds()
    if any(row.get("url") == preview.url for row in existing):
        raise RssSeedError(f"{preview.url} is already in {RSS_SEED_PATH.name}")

    row = {
        "url": preview.url,
        "title": preview.title,
        "domain": domain,
        "authority_tier": authority_tier,
        "added_at": dt.date.today().isoformat(),
    }
    block = yaml.safe_dump([row], sort_keys=False, allow_unicode=True)

    text = RSS_SEED_PATH.read_text()
    if "feeds: []" in text:
        # First real row replaces the empty-list placeholder.
        text = text.replace("feeds: []", "feeds:\n" + _indent(block))
    else:
        text = text.rstrip("\n") + "\n" + _indent(block)
    RSS_SEED_PATH.write_text(text)
    return row


def _indent(block: str) -> str:
    return "".join(f"  {line}\n" for line in block.splitlines())
