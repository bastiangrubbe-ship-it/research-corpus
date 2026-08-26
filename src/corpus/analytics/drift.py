"""How the company an entity keeps changes over time — a framing-drift proxy with
no LLM and no embeddings.

Sentiment or stance drift would need a model to judge; this doesn't try. What it
measures instead: which *kinds* of other entities co-occur in the same documents as
the target entity, bucketed by period. A vendor that used to co-occur mostly with
`regulation` entities and now co-occurs mostly with `vendor`/`product` entities has
visibly shifted from a compliance framing to a competitive one — the exact shape of
the build plan's own example query ("governance as competitive advantage, not
compliance cost"), answerable here as a distributional fact rather than a judgment
call.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from corpus.db.enums import DatePrecision, Domain
from corpus.db.models import Document, Entity, EntityMention, Source


def _period_bounds(date: dt.date, period_months: int, anchor: dt.date) -> tuple[dt.date, dt.date]:
    """(period_start, period_end) — first-of-month dates bounding the
    `period_months`-wide window `date` falls into, counted back from `anchor` in
    `period_months`-sized chunks (not calendar-aligned — "N months before now,"
    matching how `velocity.top_rising_entities` windows time, not calendar quarters).

    Returns both bounds explicitly rather than a single label: an earlier version of
    this returned only `period_end`, and every caller — including a from-scratch SQL
    validation pass in the same session that built it — misread it as "starting
    here," when a bucket ending in the anchor's month can start several months
    earlier. There's no single date that conveys "the window ending here" without
    also saying where it starts.
    """
    months_back = (anchor.year - date.year) * 12 + (anchor.month - date.month)
    bucket_index = months_back // period_months

    anchor_month_index = anchor.year * 12 + (anchor.month - 1)
    end_month_index = anchor_month_index - bucket_index * period_months
    start_month_index = end_month_index - (period_months - 1)

    end_year, end_month = divmod(end_month_index, 12)
    start_year, start_month = divmod(start_month_index, 12)
    return dt.date(start_year, start_month + 1, 1), dt.date(end_year, end_month + 1, 1)


def co_occurring_kinds_by_period(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
    domain: Domain | None,
    as_of: dt.date,
    period_months: int = 6,
) -> dict[tuple[dt.date, dt.date], dict[str, int]]:
    """{(period_start, period_end): {kind: distinct_co_occurring_entity_count}},
    oldest period first. Counts distinct co-occurring *entities* per document (not
    raw mention rows) so a document that mentions the same co-occurring entity five
    times doesn't inflate its kind's count fivefold — the question is how many
    different related things show up alongside the target, not how many times.
    """
    anchor = aliased(EntityMention)
    co = aliased(EntityMention)

    distinct_pairs = (
        select(
            anchor.document_id.label("document_id"),
            co.entity_id.label("co_entity_id"),
            Entity.kind.label("co_kind"),
        )
        .distinct()
        .select_from(anchor)
        .join(
            co,
            (co.document_id == anchor.document_id) & (co.entity_id != anchor.entity_id),
        )
        .join(Entity, Entity.id == co.entity_id)
        .where(
            anchor.tenant_id == tenant_id,
            anchor.entity_id == entity_id,
            co.tenant_id == tenant_id,
            Entity.tenant_id == tenant_id,
        )
        .subquery()
    )

    stmt = (
        select(Document.published_at, distinct_pairs.c.co_kind)
        .select_from(distinct_pairs)
        .join(Document, Document.id == distinct_pairs.c.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.published_at.is_not(None),
            Document.published_at_precision != DatePrecision.UNKNOWN,
        )
    )
    if domain is not None:
        stmt = stmt.where(Source.domain == domain)

    counts: dict[tuple[dt.date, dt.date], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for published_at, co_kind in session.execute(stmt).all():
        period = _period_bounds(published_at.date(), period_months, as_of)
        counts[period][co_kind.value] += 1

    return {period: dict(kinds) for period, kinds in sorted(counts.items())}
