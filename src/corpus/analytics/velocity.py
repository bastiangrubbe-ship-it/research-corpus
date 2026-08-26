"""How often an entity is discussed, and whether that rate is rising or falling.

No embeddings, no LLM — this is a GROUP BY over `entity_mention` joined to `document`
for its publication date, per the build plan's step 5 milestone. Live aggregation, not
a materialized view: at this corpus's current scale (289k mentions), the query is fast
enough that refresh staleness isn't worth the operational cost of a view to keep in
sync. Revisit if `EXPLAIN ANALYZE` says otherwise at a larger corpus size.

`domain` is a required keyword, not optional-defaulting-to-all: `Domain`'s own
docstring is explicit that analytics filter by domain by default, and blending across
domains is something a caller opts into by passing `domain=None`, not something that
happens by not thinking about it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corpus.db.enums import DatePrecision, Domain
from corpus.db.models import Document, Entity, EntityMention, Source


def mention_counts_by_month(
    session: Session, *, tenant_id: uuid.UUID, entity_id: uuid.UUID, domain: Domain | None
) -> list[tuple[dt.date, int]]:
    """(month, mention_count) for one entity, chronological, months with zero
    mentions omitted rather than filled — callers that need a continuous series
    fill the gaps themselves, since "no data" and "zero" are different claims."""
    month = func.date_trunc("month", Document.published_at)
    stmt = (
        select(month.label("month"), func.count().label("n"))
        .select_from(EntityMention)
        .join(Document, Document.id == EntityMention.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            EntityMention.tenant_id == tenant_id,
            EntityMention.entity_id == entity_id,
            Document.published_at.is_not(None),
            Document.published_at_precision != DatePrecision.UNKNOWN,
        )
    )
    if domain is not None:
        stmt = stmt.where(Source.domain == domain)
    stmt = stmt.group_by(month).order_by(month)
    return [(row.month.date(), row.n) for row in session.execute(stmt).all()]


def _counts_by_entity_in_window(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    domain: Domain | None,
    start: dt.date,
    end: dt.date,
) -> dict[uuid.UUID, int]:
    stmt = (
        select(EntityMention.entity_id, func.count().label("n"))
        .select_from(EntityMention)
        .join(Document, Document.id == EntityMention.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            EntityMention.tenant_id == tenant_id,
            Document.published_at.is_not(None),
            Document.published_at_precision != DatePrecision.UNKNOWN,
            Document.published_at >= start,
            Document.published_at < end,
        )
    )
    if domain is not None:
        stmt = stmt.where(Source.domain == domain)
    stmt = stmt.group_by(EntityMention.entity_id)
    return {row.entity_id: row.n for row in session.execute(stmt).all()}


@dataclass(frozen=True, slots=True)
class VelocityResult:
    entity_id: uuid.UUID
    canonical_name: str
    kind: str
    recent_count: int
    prior_count: int

    @property
    def pct_change(self) -> float | None:
        """None when `prior_count` is zero — "infinite growth from nothing" is not
        a percentage, and callers should treat it as its own case (a new entrant,
        see corpus.analytics.emergence) rather than a numeric outlier."""
        if self.prior_count == 0:
            return None
        return (self.recent_count - self.prior_count) / self.prior_count


def top_rising_entities(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    domain: Domain | None,
    as_of: dt.date,
    recent_months: int = 6,
    prior_months: int = 6,
    min_mentions: int = 5,
    limit: int = 20,
) -> list[VelocityResult]:
    """The direct answer to "which vendors are discussed more now than six months
    ago": compares mention counts in the `recent_months` window ending at `as_of`
    against the `prior_months` window immediately before it, ranked by absolute
    growth (recent - prior). `min_mentions` (applied to the recent window) exists
    because a jump from 1 mention to 3 is a 200% increase and complete noise — real
    trend signal needs a floor.
    """
    recent_start = as_of - dt.timedelta(days=30 * recent_months)
    prior_start = recent_start - dt.timedelta(days=30 * prior_months)

    recent = _counts_by_entity_in_window(
        session, tenant_id=tenant_id, domain=domain, start=recent_start, end=as_of
    )
    prior = _counts_by_entity_in_window(
        session, tenant_id=tenant_id, domain=domain, start=prior_start, end=recent_start
    )

    candidate_ids = [eid for eid, n in recent.items() if n >= min_mentions]
    if not candidate_ids:
        return []

    entities = {
        row.id: row
        for row in session.execute(
            select(Entity).where(Entity.tenant_id == tenant_id, Entity.id.in_(candidate_ids))
        )
        .scalars()
        .all()
    }

    results = [
        VelocityResult(
            entity_id=eid,
            canonical_name=entities[eid].canonical_name,
            kind=entities[eid].kind.value,
            recent_count=recent[eid],
            prior_count=prior.get(eid, 0),
        )
        for eid in candidate_ids
        if eid in entities
    ]
    results.sort(key=lambda r: r.recent_count - r.prior_count, reverse=True)
    return results[:limit]
