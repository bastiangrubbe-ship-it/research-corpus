"""RSS ingestion orchestration — the RSS counterpart to `runner.run_ingestion`.

Lives in `corpus.ingest` rather than `corpus.web` on purpose: `runner.py` already
establishes that orchestration is shared, so a future `flows/ingest_rss.py` and the
dashboard call the same function instead of each growing their own copy.

**`ingest_source` needed no changes.** It operates on `Source` / `SourceAdapter` /
`BronzeStore` with no YouTube-specific branching, which is exactly what step 10 of
the build plan set out to test. What *was* YouTube-specific lived in `runner.py` —
`get_or_create_source` hardcodes `SourceKind.YOUTUBE_CHANNEL` and the seed-row shape,
and `run_ingestion` wires up Supadata plus the credit ledger. Those are reimplemented
here for RSS; the pipeline itself is reused untouched.

No credit accounting: RSS is a plain HTTP fetch of a public feed, with no metered
provider behind it. The result still reports zeros so callers sharing a shape with
the YouTube path don't need to special-case it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from corpus.bronze.store import BronzeStore
from corpus.config import get_settings
from corpus.db.enums import AuthorityTier, Domain, SourceKind
from corpus.db.models import Source
from corpus.db.session import tenant_session
from corpus.ingest.pipelines import EventSink, noop_sink
from corpus.ingest.runner import resolve_tenant_id


@dataclass(frozen=True, slots=True)
class RssRunResult:
    feed_url: str
    fetched: int
    skipped: int
    failed: int
    #: Always zero — see the module docstring. Present so the shape matches the
    #: YouTube run result rather than making callers branch on source kind.
    credits_spent: int = 0
    credits_budget: int = 0


def get_or_create_rss_source(
    session: Session,
    tenant_id: uuid.UUID,
    *,
    feed_url: str,
    title: str,
    domain: str = "unknown",
    authority_tier: str = "unknown",
) -> Source:
    """Modeled on `runner.get_or_create_source`. The feed URL is the natural dedup
    key and doubles as `external_id` — RSS has no handle, and the URL is also what
    `RssAdapter.discover` takes as its `source_ref`."""
    existing = session.execute(
        select(Source).where(
            Source.tenant_id == tenant_id,
            Source.kind == SourceKind.RSS,
            Source.external_id == feed_url,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    source = Source(
        tenant_id=tenant_id,
        kind=SourceKind.RSS,
        external_id=feed_url,
        title=title,
        url=feed_url,
        authority_tier=AuthorityTier(authority_tier),
        domain=Domain(domain),
    )
    session.add(source)
    session.flush()
    return source


def run_rss_ingestion(
    *,
    feed_url: str,
    title: str,
    domain: str = "unknown",
    authority_tier: str = "unknown",
    limit: int | None = None,
    on_event: EventSink = noop_sink,
) -> RssRunResult:
    """Ingest one feed. Idempotent for the same reason the YouTube path is:
    `ingest_source` skips entries whose `document` row already exists."""
    from corpus.ingest.pipelines import ingest_source
    from corpus.sources.rss.adapter import RssAdapter

    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)
    bronze = BronzeStore(settings.bronze_dir)

    with tenant_session(tenant_id) as session:
        source = get_or_create_rss_source(
            session,
            tenant_id,
            feed_url=feed_url,
            title=title,
            domain=domain,
            authority_tier=authority_tier,
        )
        summary = ingest_source(
            session,
            source=source,
            adapter=RssAdapter(),
            bronze=bronze,
            limit=limit,
            on_event=on_event,
        )

    return RssRunResult(
        feed_url=feed_url,
        fetched=summary.fetched,
        skipped=summary.already_ingested,
        failed=summary.failed,
    )
