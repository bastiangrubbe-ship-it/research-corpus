"""Seed-table reads and writes for the web dashboard.

Every write here goes through `seeds/youtube_channels.yaml` — never straight into
the database. That file is the single reviewed, git-tracked source of truth (see
seeds/README.md); a dashboard action that bypassed it in favor of writing directly
to `source` would create a second, invisible channel of truth with no diff history
and no review step. New rows land with domain/authority_tier defaulted to
'unknown', exactly like the channels already in the table before classification —
review is a YAML edit, not a separate approval workflow.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any

import yaml

from corpus.ingest.runner import SEED_PATH

_VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})")
_HANDLE_RE = re.compile(r"youtube\.com/(@[\w.-]+)")


class SeedInputError(Exception):
    """The pasted URL/handle couldn't be resolved to a channel. Shown to the user
    verbatim — this is a UI validation error, not an internal failure."""


@dataclass(frozen=True, slots=True)
class ResolvedChannel:
    handle: str
    name: str
    subscribers: int | None


def load_all_seeds() -> list[dict[str, Any]]:
    return yaml.safe_load(SEED_PATH.read_text()) or []


def _run_ytdlp_json(url: str) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "yt-dlp",
            "--skip-download",
            "--dump-single-json",
            "--no-warnings",
            "--playlist-items",
            "0",
            "--socket-timeout",
            "20",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise SeedInputError(f"couldn't resolve {url}: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout)


def resolve_input(raw: str) -> ResolvedChannel:
    """A channel URL, a bare @handle, or a single video URL — resolved to the
    channel it belongs to. yt-dlp reports `channel`/`channel_id` on a single
    video's own metadata, so a video link auto-resolves to its parent channel
    rather than requiring the user to go find the channel page themselves.
    """
    raw = raw.strip()
    if not raw:
        raise SeedInputError("paste a channel or video URL")

    video_match = _VIDEO_ID_RE.search(raw)
    if video_match:
        payload = _run_ytdlp_json(f"https://www.youtube.com/watch?v={video_match.group(1)}")
        channel_id = payload.get("channel_id")
        if not channel_id:
            raise SeedInputError("video metadata had no channel_id to resolve")
        channel_payload = _run_ytdlp_json(f"https://www.youtube.com/channel/{channel_id}")
        return ResolvedChannel(
            handle=channel_payload.get("uploader_id") or f"@{channel_id}",
            name=channel_payload.get("channel") or payload.get("channel") or "unknown",
            subscribers=channel_payload.get("channel_follower_count"),
        )

    handle_match = _HANDLE_RE.search(raw)
    handle = handle_match.group(1) if handle_match else (raw if raw.startswith("@") else None)
    if handle is None:
        raise SeedInputError(f"not a recognizable channel or video URL: {raw!r}")

    payload = _run_ytdlp_json(f"https://www.youtube.com/{handle}")
    return ResolvedChannel(
        handle=payload.get("uploader_id") or handle,
        name=payload.get("channel") or handle,
        subscribers=payload.get("channel_follower_count"),
    )


def append_seed(
    resolved: ResolvedChannel, *, domain: str = "unknown", authority_tier: str = "unknown"
) -> dict[str, Any]:
    """Append one channel to the seed YAML. Refuses a duplicate handle rather than
    creating a second row for the same channel.
    """
    rows = load_all_seeds()
    key = resolved.handle.lstrip("@").lower()
    if any(r["handle"].lstrip("@").lower() == key for r in rows):
        raise SeedInputError(f"{resolved.handle} is already in the seed table")

    new_row = {
        "handle": resolved.handle,
        "name": resolved.name,
        "domain": domain,
        "authority_tier": authority_tier,
        "phase": "manual-review",
        "note": "added via dashboard manual-add; needs domain/tier review",
    }
    if resolved.subscribers:
        new_row["subscribers_at_survey"] = resolved.subscribers

    _append_row(new_row)
    return new_row


def _append_row(row: dict[str, Any]) -> None:
    """Append one entry's YAML block to the end of the file, touching nothing
    else. Re-dumping the full row list through `yaml.safe_dump` was tried first
    and rejected: it reformats every existing row's quoting and spacing on every
    single append, turning a one-channel addition into a thousand-line diff — the
    opposite of what a reviewable, git-tracked seed table is for. `yaml.safe_dump`
    on a *single* one-item list gives the same safe quoting with no such blast
    radius, since nothing upstream of the new entry is ever re-serialized.
    """
    block = yaml.safe_dump([row], sort_keys=False, default_flow_style=False, allow_unicode=True)
    with SEED_PATH.open("a") as f:
        f.write("\n" + block)
