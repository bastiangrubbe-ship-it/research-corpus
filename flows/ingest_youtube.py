#!/usr/bin/env python
"""Ingest YouTube channels from seeds/youtube_channels.yaml, in phase order.

    uv run python flows/ingest_youtube.py [--phase research-p1] [--limit 5] [--dry-run]

`--limit` caps videos discovered per channel — useful for a first smoke-test run
against real channels without spending the full phase's credit budget.

This is a plain script, not embedded in a scheduler. The scheduler (launchd now,
systemd later) is meant to be a thin wrapper that invokes this, per the migration
constraints in the original build plan — no orchestration logic belongs in here.
The actual orchestration lives in corpus.ingest.runner, shared with the web
dashboard's run manager so the two never drift into two different definitions of
"ingest these seeds."
"""

from __future__ import annotations

import argparse
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
    parser.add_argument("--dry-run", action="store_true", help="print the plan, fetch nothing")
    args = parser.parse_args()

    seeds = load_seeds(args.phase, args.handle)
    if not seeds:
        print(f"no seed rows for phase={args.phase!r}", file=sys.stderr)
        return 1

    print(f"{len(seeds)} channel(s) queued" + (f" (phase={args.phase})" if args.phase else ""))
    if args.dry_run:
        for s in seeds:
            print(f"  {s['handle']:28} {s['domain']:22} {s.get('videos_at_survey', '?')} videos")
        return 0

    result = run_ingestion(seeds, limit=args.limit, on_event=_print_event)
    print(f"\ncredits spent this run: {result.credits_spent} of {result.credits_budget}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
