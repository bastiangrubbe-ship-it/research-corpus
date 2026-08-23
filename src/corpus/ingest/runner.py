"""Shared ingestion orchestration: resolve the tenant, build the adapter, loop over
seed rows. Both `flows/ingest_youtube.py` (CLI) and the web dashboard's run manager
call this — neither re-implements it, so there is exactly one place that decides
what "run these seeds" means.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from corpus.bronze.store import BronzeStore
from corpus.config import Settings
from corpus.db.enums import AuthorityTier, Domain, SourceKind
from corpus.db.models import Source, Tenant
from corpus.db.session import get_session_factory, tenant_session
from corpus.ingest.pipelines import EventSink, IngestSummary, ingest_source, noop_sink
from corpus.sources.youtube.adapter import YouTubeAdapter
from corpus.sources.youtube.supadata import SupadataClient
from corpus.sources.youtube.ytapi import YtApiTranscriptClient

REPO_ROOT = Path(__file__).resolve().parents[3]
SEED_PATH = REPO_ROOT / "seeds" / "youtube_channels.yaml"


def load_seeds(phase: str | None = None, handle: str | None = None) -> list[dict]:
    rows = yaml.safe_load(SEED_PATH.read_text())
    if handle is not None:
        rows = [r for r in rows if r["handle"].lstrip("@").lower() == handle.lstrip("@").lower()]
    if phase is not None:
        rows = [r for r in rows if r["phase"] == phase]
    return rows


def cap_for(seed: dict) -> int | None:
    # Two channels are capped in the seed table's note (§seeds/README.md) because a
    # full backfill would be mostly repetition. Parsed here rather than adding a
    # dedicated column, since exactly two rows need it today.
    note = seed.get("note", "")
    if "CAP at" in note:
        return int(note.split("CAP at")[1].split()[0])
    return None


def resolve_tenant_id(settings: Settings) -> uuid.UUID:
    """The single tenant for this deployment, resolved from config — never from a
    request argument or anything else that crosses a trust boundary.
    """
    with get_session_factory()() as bootstrap:
        tenant = bootstrap.execute(
            select(Tenant).where(Tenant.slug == settings.tenant_slug)
        ).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(slug=settings.tenant_slug, name=settings.tenant_slug)
            bootstrap.add(tenant)
            bootstrap.commit()
        return tenant.id


def get_or_create_source(session: Session, tenant_id: uuid.UUID, seed: dict) -> Source:
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
        authority_tier=AuthorityTier(seed.get("authority_tier", "unknown")),
        domain=Domain(seed.get("domain", "unknown")),
    )
    session.add(source)
    session.flush()
    return source


def build_adapter(tenant_id: uuid.UUID) -> tuple[YouTubeAdapter, SupadataClient]:
    """`on_spend` persists every credit spend the moment it happens, via its own
    short-lived session — decoupled from whatever ingestion transaction is in
    flight, because the credit was genuinely spent regardless of what happens to
    that transaction afterward. Without this, `credit_usage_event` never gets
    written and "credits used" resets to zero on every process restart, since
    Supadata itself reports no consumption back to the caller.
    """
    from corpus.config import get_settings
    from corpus.ops.credit_usage import record_spend

    def on_spend(credits: int, endpoint: str, external_id: str | None) -> None:
        with tenant_session(tenant_id) as log_session:
            record_spend(
                log_session,
                tenant_id=tenant_id,
                provider="supadata",
                endpoint=endpoint,
                external_id=external_id,
                credits=credits,
            )

    settings = get_settings()
    supadata = SupadataClient(
        api_key=settings.supadata_api_key.get_secret_value() if settings.has_supadata_key else "",
        base_url=settings.supadata_base_url,
        requests_per_second=settings.supadata_requests_per_second,
        monthly_credits=settings.supadata_monthly_credits,
        on_spend=on_spend,
    )
    adapter = YouTubeAdapter(
        ytapi=YtApiTranscriptClient(), supadata=supadata, provider_order=("ytapi", "supadata")
    )
    return adapter, supadata


@dataclass(frozen=True, slots=True)
class RunResult:
    summaries: list[IngestSummary]
    credits_spent: int
    credits_budget: int


def run_ingestion(
    seeds: list[dict],
    *,
    limit: int | None = None,
    on_event: EventSink = noop_sink,
) -> RunResult:
    """Ingest each seed row in order."""
    from corpus.config import get_settings

    settings = get_settings()
    bronze = BronzeStore(settings.bronze_dir)
    tenant_id = resolve_tenant_id(settings)
    adapter, supadata = build_adapter(tenant_id)

    summaries: list[IngestSummary] = []
    try:
        with tenant_session(tenant_id) as session:
            for seed in seeds:
                source = get_or_create_source(session, tenant_id, seed)
                session.commit()

                cap = cap_for(seed)
                effective_limit = min(limit, cap) if limit and cap else (limit or cap)

                summary = ingest_source(
                    session,
                    source=source,
                    adapter=adapter,
                    bronze=bronze,
                    limit=effective_limit,
                    on_event=on_event,
                )
                summaries.append(summary)
    finally:
        credits_spent = supadata.ledger.spent
        credits_budget = supadata.ledger.budget
        supadata.close()

    return RunResult(
        summaries=summaries, credits_spent=credits_spent, credits_budget=credits_budget
    )
