#!/usr/bin/env python
"""Fetch long-form video counts for every channel in subscriptions_full_resolved.json.

    uv run python scripts/resolve_subscriptions_full.py

One-time survey script, not part of the ingest pipeline — same category as
build_channel_seeds.py and resolve_channels_ytdlp.py. Free (yt-dlp, no Supadata
credits): flat-playlist enumeration of each channel's /videos tab, which is
exactly what build_channel_seeds.py's "videos_at_survey" already means for the
existing 155 (excludes Shorts, which live on a separate /shorts tab).

Capped at PLAYLIST_END per channel so one very large channel (thousands of
videos) can't blow up total runtime the way @Databricks or the deferred AWS
channel would uncapped — capped rows are marked so a cap doesn't silently read
as an exact count.

Runs a small thread pool (MAX_WORKERS) rather than a large one: this is yt-dlp
scraping YouTube without an API key, and hammering it with heavy concurrency is
more likely to trigger throttling or a temporary block than the modest pace here.
Not a job for parallel agents either — each call is a subprocess waiting on a
network response, no judgment involved, so a thread pool does the same work for
a fraction of the cost.
"""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

from corpus.config import get_settings

PLAYLIST_END = 3000
MAX_WORKERS = 5


def fetch_video_count(handle: str) -> dict:
    # subscriptions_full_resolved.json stores handles without the leading "@" —
    # unlike seeds/youtube_channels.yaml, which always includes it. YouTube's own
    # URLs 404 without the sigil (confirmed directly: /a16z/videos -> 404,
    # /@a16z/videos -> 200), so it has to be added back here.
    at_handle = handle if handle.startswith("@") else f"@{handle}"
    url = f"https://www.youtube.com/{at_handle}/videos"
    try:
        proc = subprocess.run(
            [
                "yt-dlp",
                "--flat-playlist",
                "--dump-single-json",
                "--no-warnings",
                "--skip-download",
                "--socket-timeout",
                "20",
                "--playlist-end",
                str(PLAYLIST_END),
                url,
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return {"handle": handle, "ok": False, "error": "timeout"}

    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        return {"handle": handle, "ok": False, "error": err[-1][:140] if err else "yt-dlp error"}

    try:
        d = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"handle": handle, "ok": False, "error": "unparseable json"}

    entries = d.get("entries", [])
    return {
        "handle": handle,
        "ok": True,
        "videos": len(entries),
        "capped": len(entries) >= PLAYLIST_END,
    }


def main() -> int:
    settings = get_settings()
    bronze = settings.bronze_dir / "channels"
    full = json.loads((bronze / "subscriptions_full_resolved.json").read_text())["channels"]

    existing_path = bronze / "subscriptions_full_counts.json"
    done: dict[str, dict] = {}
    if existing_path.exists():
        done = {r["handle"]: r for r in json.loads(existing_path.read_text())["channels"]}
        already_ok = sum(1 for r in done.values() if r.get("ok"))
        print(f"resuming: {already_ok} already ok, {len(done) - already_ok} to retry")

    # Only successes count as "done" -- a prior failure (e.g. the missing "@" bug
    # this script used to have) gets retried rather than left permanently missing.
    results = [r for r in done.values() if r.get("ok")]
    todo = [ch["handle"] for ch in full if not done.get(ch["handle"], {}).get("ok")]
    total = len(full)

    def _save() -> None:
        # Written after every completion, not just at the end -- a killed run
        # resumes from here rather than re-fetching everything already done.
        payload = {
            "_fetched_at": datetime.now(UTC).isoformat(),
            "_source": "yt-dlp flat-playlist, /videos tab (no Supadata credits spent)",
            "_playlist_end_cap": PLAYLIST_END,
            "channels": results,
        }
        existing_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    completed = len(results)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_video_count, handle): handle for handle in todo}
        for future in as_completed(futures):
            handle = futures[future]
            row = future.result()
            results.append(row)
            completed += 1
            if row["ok"]:
                cap_note = " (capped)" if row.get("capped") else ""
                print(f"[{completed}/{total}] ok   {handle:28} {row['videos']:>6}{cap_note}")
            else:
                print(f"[{completed}/{total}] MISS {handle:28} {row['error'][:60]}")
            _save()

    ok = sum(1 for r in results if r["ok"])
    print(f"\nresolved {ok}/{len(results)}; 0 credits spent; -> {existing_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
