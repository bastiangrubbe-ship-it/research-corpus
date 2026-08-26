#!/usr/bin/env python
"""Ingest YouTube channels from seeds/youtube_channels.yaml, in phase order.

    uv run python flows/ingest_youtube.py [--phase research-p1] [--limit 5] [--dry-run]
    uv run python flows/ingest_youtube.py --since-days 30
    uv run python flows/ingest_youtube.py --limit 20 --provider-order supadata
    uv run python flows/ingest_youtube.py --limit 20 --provider-order supadata --batch
    uv run python flows/ingest_youtube.py --limit 20 --provider-order supadata \
        --batch --metadata-source supadata --concurrency 10

`--limit` caps videos discovered per channel — useful for a first smoke-test run
against real channels without spending the full phase's credit budget.

`--since-days N` discovers only videos published in the last N days instead of
capping by count — the two are alternatives, not composable (see
corpus.ingest.runner.run_ingestion), since a date cutoff and a count cap answer
different questions ("what's new" vs. "how much of the archive").

`--provider-order` picks which transcript provider(s) to try, in order (comma-
separated: "ytapi,supadata", "supadata", "supadata,ytapi", ...). Defaults to
ytapi-first (free, and the only source of caption provenance).

`--batch` fetches each channel's new videos in one Supadata batch job instead of
one call per video — faster, but requires "supadata" in --provider-order, and
silently ignores mode="native": a video with no existing captions is billed at
Supadata's 2-credits/minute generate rate instead of failing cheaply (confirmed
2026-08-24 — see docs/SUPADATA.md, docs/DECISIONS.md). It also carries no
per-video signal for which transcripts needed that fallback, unlike the
per-video path.

`--metadata-source` picks where video metadata comes from: "ytdlp" (default) is
free with an exact upload timestamp, one call per video, kept sequential. "supadata"
uses a second Supadata batch job instead — ~1 credit/video, date-only precision
(uploadDate's time is always midnight, confirmed empirically) — but removes
yt-dlp from the fetch path entirely, which is what `--concurrency` above 1
requires: running channels concurrently while yt-dlp metadata stays per-channel
would multiply the exact request burst that tripped bot detection earlier.

`--concurrency N` runs N channels at once instead of one at a time (only valid
with --batch --metadata-source supadata). Bounded by the DB connection pool
(15 total — pool_size=5 + max_overflow=10 in corpus.db.session); stay comfortably
under that.

This is a plain script, not embedded in a scheduler. The scheduler (launchd now,
systemd later) is meant to be a thin wrapper that invokes this, per the migration
constraints in the original build plan — no orchestration logic belongs in here.
The actual orchestration lives in corpus.ingest.runner, shared with the web
dashboard's run manager so the two never drift into two different definitions of
"ingest these seeds."
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from corpus.ingest.pipelines import IngestEvent
from corpus.ingest.runner import load_seeds, run_ingestion


def _print_event(event: IngestEvent) -> None:
    if event.kind == "discovered":
        print(f"  {event.source_handle:28} discovered={event.total}")
    elif event.kind == "fetched":
        provider = event.extra.get("provider", "?")
        pos = f"[{event.current}/{event.total}]"
        print(f"  {event.source_handle:28} {pos} {event.detail} ({provider})")
    elif event.kind == "failed":
        print(f"    ! {event.detail}")
    elif event.kind == "budget_exceeded":
        print(f"  {event.source_handle:28} credit budget exceeded, stopping")
    elif event.kind == "done":
        e = event.extra
        print(
            f"  {event.source_handle:28} discovered={event.total:<5} "
            f"already={e['already_ingested']:<5} fetched={e['fetched']:<5} failed={e['failed']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", default=None, help="only ingest this seed-table phase")
    parser.add_argument(
        "--handle", default=None, help="only ingest this one channel, e.g. @nateherk"
    )
    parser.add_argument("--limit", type=int, default=None, help="cap videos discovered per channel")
    parser.add_argument(
        "--since-days", type=int, default=None, help="only videos published in the last N days"
    )
    parser.add_argument(
        "--provider-order",
        default="ytapi,supadata",
        help="comma-separated transcript provider order, e.g. 'supadata' or 'supadata,ytapi'",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="one Supadata batch job per channel instead of per-video calls",
    )
    parser.add_argument(
        "--metadata-source",
        default="ytdlp",
        choices=("ytdlp", "supadata", "skip"),
        help="where metadata comes from, or 'skip' for none (only matters with --batch)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="channels to run at once (requires --batch --metadata-source supadata/skip)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan, fetch nothing")
    args = parser.parse_args()

    if args.limit is not None and args.since_days is not None:
        print("--limit and --since-days are alternatives, not composable", file=sys.stderr)
        return 1

    provider_order = tuple(p.strip() for p in args.provider_order.split(",") if p.strip())
    if not provider_order or any(p not in ("ytapi", "supadata") for p in provider_order):
        print(
            f"--provider-order must list only 'ytapi'/'supadata', got {args.provider_order!r}",
            file=sys.stderr,
        )
        return 1

    if args.batch and "supadata" not in provider_order:
        print("--batch requires 'supadata' in --provider-order", file=sys.stderr)
        return 1

    if args.concurrency > 1 and args.metadata_source not in ("supadata", "skip"):
        print("--concurrency > 1 requires --metadata-source supadata or skip", file=sys.stderr)
        return 1

    seeds = load_seeds(args.phase, args.handle)
    if not seeds:
        print(f"no seed rows for phase={args.phase!r}", file=sys.stderr)
        return 1

    print(f"{len(seeds)} channel(s) queued" + (f" (phase={args.phase})" if args.phase else ""))
    if args.dry_run:
        for s in seeds:
            print(f"  {s['handle']:28} {s['domain']:22} {s.get('videos_at_survey', '?')} videos")
        return 0

    since = None
    if args.since_days is not None:
        since = dt.datetime.now(dt.UTC) - dt.timedelta(days=args.since_days)
        print(f"since: {since.date()} ({args.since_days} days back)")

    result = run_ingestion(
        seeds,
        limit=args.limit,
        since=since,
        provider_order=provider_order,
        batch=args.batch,
        metadata_source=args.metadata_source,
        concurrency=args.concurrency,
        on_event=_print_event,
    )
    print(f"\ncredits spent this run: {result.credits_spent} of {result.credits_budget}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
