"""Shared ingestion orchestration: resolve the tenant, build the adapter, loop over
seed rows. Both `flows/ingest_youtube.py` (CLI) and the web dashboard's run manager
call this — neither re-implements it, so there is exactly one place that decides
what "run these seeds" means.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import structlog
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from corpus.bronze.store import BronzeStore
from corpus.config import Settings
from corpus.db.enums import AuthorityTier, Domain, SourceKind
from corpus.db.models import Source, Tenant
from corpus.db.session import get_session_factory, tenant_session
from corpus.ingest.pipelines import EventSink, IngestEvent, IngestSummary, ingest_source, noop_sink
from corpus.sources.youtube.adapter import YouTubeAdapter
from corpus.sources.youtube.supadata import SupadataClient
from corpus.sources.youtube.ytapi import YtApiTranscriptClient

log = structlog.get_logger(__name__)

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
    # Some channels are capped in the seed table's note (§seeds/README.md) because a
    # full backfill would be mostly repetition. Parsed here rather than adding a
    # dedicated column.
    #
    # Matched case-insensitively, and that is the whole point: this was written for
    # two rows saying "CAP at 400" and every row added since says "cap at 400". A
    # case-sensitive match silently ignored 15 of the 17 caps -- and a cap that does
    # not fire costs real money, because the channels carrying one are precisely the
    # 1,000-2,400 video catalogues the cap exists to avoid paying for. Nothing failed;
    # the ingest would simply have spent several thousand extra credits
    # (docs/DECISIONS.md, 2026-08-29).
    match = re.search(r"cap at\s+(\d+)", seed.get("note", "") or "", re.IGNORECASE)
    return int(match.group(1)) if match else None


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


def build_adapter(
    tenant_id: uuid.UUID, *, provider_order: tuple[str, ...] = ("ytapi", "supadata")
) -> tuple[YouTubeAdapter, SupadataClient]:
    """`on_spend` persists every credit spend the moment it happens, via its own
    short-lived session — decoupled from whatever ingestion transaction is in
    flight, because the credit was genuinely spent regardless of what happens to
    that transaction afterward. Without this, `credit_usage_event` never gets
    written and "credits used" resets to zero on every process restart, since
    Supadata itself reports no consumption back to the caller.

    `provider_order` defaults to ytapi-first (free, and the only source of caption
    provenance — see docs/DECISIONS.md), but is a deployment property, not a
    constant: on a cloud host where YouTube blocks ytapi, or when yt-dlp's
    per-video metadata endpoint needs a break (also in DECISIONS.md), Supadata-first
    is the right call, which is why this is a parameter rather than hardcoded here.
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
        ytapi=YtApiTranscriptClient(), supadata=supadata, provider_order=provider_order
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
    since: dt.datetime | None = None,
    provider_order: tuple[str, ...] = ("ytapi", "supadata"),
    batch: bool = False,
    metadata_source: str = "ytdlp",
    concurrency: int = 1,
    on_event: EventSink = noop_sink,
) -> RunResult:
    """Ingest each seed row, in order if `concurrency<=1` (the default), otherwise
    across a thread pool of that size — one channel per worker, each with its own
    DB session (`tenant_session` is not shared across threads; `adapter`/`supadata`
    are, but the rate limiter and credit ledger are already thread-safe locks — see
    `SupadataClient.RateLimiter`/`CreditLedger`).

    `since` and `limit` are alternatives, not composable — see
    `YouTubeAdapter.discover`. When `since` is set, the per-channel `cap_for` value
    (the two channels whose archive is too large to backfill in full) is not
    applied: a date-bounded run doesn't have the "backfill everything" problem that
    cap exists for.

    `batch=True` requires `"supadata"` to be in `provider_order` — see
    `ingest_source` and `YouTubeAdapter.fetch_batch`. `concurrency>1` additionally
    requires `metadata_source` to be `"supadata"` or `"skip"`: yt-dlp metadata is
    deliberately kept to one request at a time (see docs/DECISIONS.md), so running
    multiple channels concurrently while still calling it per-channel would
    multiply exactly the request burst that tripped bot detection earlier — this
    is refused, not just discouraged.
    """
    if concurrency > 1 and metadata_source not in ("supadata", "skip"):
        raise ValueError(
            "concurrency>1 requires metadata_source='supadata' or 'skip' — "
            "concurrent yt-dlp metadata calls already tripped bot detection once"
        )

    from corpus.config import get_settings

    settings = get_settings()
    bronze = BronzeStore(settings.bronze_dir)
    tenant_id = resolve_tenant_id(settings)
    adapter, supadata = build_adapter(tenant_id, provider_order=provider_order)

    def _process_seed(seed: dict) -> IngestSummary:
        handle = seed.get("handle", "?")
        try:
            with tenant_session(tenant_id) as session:
                source = get_or_create_source(session, tenant_id, seed)
                session.commit()

                if since is not None:
                    effective_limit = None
                else:
                    cap = cap_for(seed)
                    effective_limit = min(limit, cap) if limit and cap else (limit or cap)

                return ingest_source(
                    session,
                    source=source,
                    adapter=adapter,
                    bronze=bronze,
                    limit=effective_limit,
                    since=since,
                    batch=batch,
                    metadata_source=metadata_source,
                    on_event=on_event,
                )
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, see below
            # `ingest_source` already isolates a channel's *discovery* failures
            # (docs/DECISIONS.md, 2026-08-24), but that's one specific step. This
            # is the backstop for everything else in one seed's path — get_or_
            # create_source raising (a bad domain/tier value not in the DB enum,
            # 2026-08-24, was the second distinct crash in the same run) or any
            # future bug — so one bad seed row still can't take down every
            # channel queued after it. Broad on purpose: the whole point is not
            # having to enumerate failure modes in advance.
            log.error("seed_processing_failed", handle=handle, error=str(exc))
            on_event(
                IngestEvent(kind="failed", source_handle=handle, detail=f"seed processing: {exc}")
            )
            return IngestSummary(
                source_id=uuid.uuid4(),
                discovered=0,
                already_ingested=0,
                fetched=0,
                failed=1,
                errors=[f"seed processing: {exc}"],
            )

    summaries: list[IngestSummary] = []
    try:
        if concurrency <= 1:
            summaries = [_process_seed(seed) for seed in seeds]
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                summaries = list(pool.map(_process_seed, seeds))
    finally:
        credits_spent = supadata.ledger.spent
        credits_budget = supadata.ledger.budget
        supadata.close()

    return RunResult(
        summaries=summaries, credits_spent=credits_spent, credits_budget=credits_budget
    )
