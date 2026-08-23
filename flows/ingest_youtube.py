#!/usr/bin/env python
"""Ingest YouTube channels from seeds/youtube_channels.yaml, in phase order.

    uv run python flows/ingest_youtube.py [--phase research-p1] [--limit 5] [--dry-run]

`--limit` caps videos discovered per channel — useful for a first smoke-test run
against real channels without spending the full phase's credit budget.

This is a plain script, not embedded in a scheduler. The scheduler (launchd now,
systemd later) is meant to be a thin wrapper that invokes this, per the migration
constraints in the original build plan — no orchestration logic belongs in here.
"""

from __future__ import annotations

import argparse
import sys
import uuid

import structlog
import yaml
from sqlalchemy import select

from corpus.bronze.store import BronzeStore
from corpus.config import get_settings
from corpus.db.enums import AuthorityTier, Domain, SourceKind
from corpus.db.models import Source, Tenant
from corpus.db.session import tenant_session
from corpus.ingest.pipelines import ingest_source
from corpus.sources.youtube.adapter import YouTubeAdapter
from corpus.sources.youtube.supadata import SupadataClient
from corpus.sources.youtube.ytapi import YtApiTranscriptClient

log = structlog.get_logger(__name__)

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
SEED_PATH = REPO_ROOT / "seeds" / "youtube_channels.yaml"


def _load_seeds(phase: str | None, handle: str | None) -> list[dict]:
    rows = yaml.safe_load(SEED_PATH.read_text())
    if handle is not None:
        rows = [r for r in rows if r["handle"].lstrip("@").lower() == handle.lstrip("@").lower()]
    if phase is not None:
        rows = [r for r in rows if r["phase"] == phase]
    return rows


def _get_or_create_source(session, tenant_id: uuid.UUID, seed: dict) -> Source:
    handle = seed["handle"]
    existing = session.execute(
        select(Source).where(
            Source.tenant_id == tenant_id,
            Source.kind == SourceKind.YOUTUBE_CHANNEL,
            Source.external_id == handle,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    source = Source(
        tenant_id=tenant_id,
        kind=SourceKind.YOUTUBE_CHANNEL,
        external_id=handle,
        title=seed["name"],
        authority_tier=AuthorityTier(seed["authority_tier"]),
        domain=Domain(seed["domain"]),
    )
    session.add(source)
    session.flush()
    return source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", default=None, help="only ingest this seed-table phase")
    parser.add_argument(
        "--handle", default=None, help="only ingest this one channel, e.g. @nateherk"
    )
    parser.add_argument("--limit", type=int, default=None, help="cap videos discovered per channel")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, fetch nothing")
    args = parser.parse_args()

    settings = get_settings()
    seeds = _load_seeds(args.phase, args.handle)
    if not seeds:
        print(f"no seed rows for phase={args.phase!r}", file=sys.stderr)
        return 1

    print(f"{len(seeds)} channel(s) queued" + (f" (phase={args.phase})" if args.phase else ""))
    if args.dry_run:
        for s in seeds:
            print(f"  {s['handle']:28} {s['domain']:22} {s.get('videos_at_survey', '?')} videos")
        return 0

    bronze = BronzeStore(settings.bronze_dir)
    ytapi = YtApiTranscriptClient()
    supadata = SupadataClient(
        api_key=settings.supadata_api_key.get_secret_value() if settings.has_supadata_key else "",
        base_url=settings.supadata_base_url,
        requests_per_second=settings.supadata_requests_per_second,
        monthly_credits=settings.supadata_monthly_credits,
    )
    adapter = YouTubeAdapter(ytapi=ytapi, supadata=supadata, provider_order=("ytapi", "supadata"))

    tenant_id = _resolve_tenant_id(settings)
    with tenant_session(tenant_id) as session:
        for seed in seeds:
            source = _get_or_create_source(session, tenant_id, seed)
            session.commit()

            cap = _cap_for(seed)
            limit = min(args.limit, cap) if args.limit and cap else (args.limit or cap)

            summary = ingest_source(
                session, source=source, adapter=adapter, bronze=bronze, limit=limit
            )
            print(
                f"{seed['handle']:28} discovered={summary.discovered:<5} "
                f"already={summary.already_ingested:<5} fetched={summary.fetched:<5} "
                f"failed={summary.failed}"
            )
            for err in summary.errors[:5]:
                print(f"    ! {err}")

    print(f"\ncredits spent this run: {supadata.ledger.spent} of {supadata.ledger.budget}")
    supadata.close()
    return 0


def _cap_for(seed: dict) -> int | None:
    # Two channels are capped in the seed table's note (§seeds/README.md) because a
    # full backfill would be mostly repetition. Parsed here rather than adding a
    # dedicated column, since exactly two rows need it today.
    note = seed.get("note", "")
    if "CAP at" in note:
        return int(note.split("CAP at")[1].split()[0])
    return None


def _resolve_tenant_id(settings) -> uuid.UUID:
    """The single tenant for this deployment, resolved from config — never from a
    command-line argument or anything else that crosses a trust boundary.
    """
    from corpus.db.session import get_session_factory

    with get_session_factory()() as bootstrap:
        tenant = bootstrap.execute(
            select(Tenant).where(Tenant.slug == settings.tenant_slug)
        ).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(slug=settings.tenant_slug, name=settings.tenant_slug)
            bootstrap.add(tenant)
            bootstrap.commit()
        return tenant.id


if __name__ == "__main__":
    raise SystemExit(main())
