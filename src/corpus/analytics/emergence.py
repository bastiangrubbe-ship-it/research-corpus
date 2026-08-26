"""When an entity first entered the discourse, and what's newly appeared recently.

`velocity.top_rising_entities` compares two windows for entities that already had
mentions in the prior one — it structurally can't surface a vendor with zero prior
mentions, since `pct_change` from zero isn't a percentage (see that module's
`VelocityResult` docstring). This module is the other half: entities whose entire
mention history starts recently, not ones that grew from an existing base.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corpus.db.enums import DatePrecision, Domain
from corpus.db.models import Document, Entity, EntityMention, Source


def first_mention_date(
    session: Session, *, tenant_id: uuid.UUID, entity_id: uuid.UUID
) -> dt.date | None:
    """Earliest known publication date carrying a mention of this entity, across
    all domains — "when did this first show up" isn't a domain-scoped question the
    way trend/saturation comparisons are, so this one doesn't take a `domain` filter.
    None if every mention happens to be on undated documents.
    """
    stmt = (
        select(func.min(Document.published_at))
        .select_from(EntityMention)
        .join(Document, Document.id == EntityMention.document_id)
        .where(
            EntityMention.tenant_id == tenant_id,
            EntityMention.entity_id == entity_id,
            Document.published_at.is_not(None),
            Document.published_at_precision != DatePrecision.UNKNOWN,
        )
    )
    result = session.execute(stmt).scalar_one_or_none()
    return result.date() if result else None


@dataclass(frozen=True, slots=True)
class EmergentEntity:
    entity_id: uuid.UUID
    canonical_name: str
    kind: str
    first_mention_date: dt.date
    mention_count_since: int


def newly_emerged_entities(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    domain: Domain | None,
    since: dt.date,
    min_mentions: int = 2,
    limit: int | None = None,
) -> list[EmergentEntity]:
    """Entities whose *entire* mention history starts on or after `since` — not
    "mentioned since then" (most entities are), but "didn't exist in this corpus's
    discourse before then." `min_mentions` filters out one-off ASR noise that
    slipped past the extractor rather than treating every never-seen-before name as
    a real emergence signal.

    Ordered by mention count descending — the most-discussed new entrants first,
    matching how `velocity.top_rising_entities` and `saturation.
    most_saturated_entities` rank. This was chronological until 2026-08-25; the
    change matters because a six-month window on this corpus yields ~3,200 rows,
    where a chronological head is just "whatever appeared in week one" rather than
    anything notable. `first_mention_date` is still on every row, so a caller
    wanting the timeline re-sorts it themselves.

    `limit=None` returns everything, which is the right default for an analytical
    caller but wrong for a UI or an agent context window — both pass a real limit.
    """
    first_seen = (
        select(
            EntityMention.entity_id,
            func.min(Document.published_at).label("first_seen"),
            func.count().label("n"),
        )
        .select_from(EntityMention)
        .join(Document, Document.id == EntityMention.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            EntityMention.tenant_id == tenant_id,
            Document.published_at.is_not(None),
            Document.published_at_precision != DatePrecision.UNKNOWN,
        )
    )
    if domain is not None:
        first_seen = first_seen.where(Source.domain == domain)
    first_seen = first_seen.group_by(EntityMention.entity_id).subquery()

    stmt = (
        select(Entity, first_seen.c.first_seen, first_seen.c.n)
        .join(first_seen, first_seen.c.entity_id == Entity.id)
        .where(
            Entity.tenant_id == tenant_id,
            first_seen.c.first_seen >= since,
            first_seen.c.n >= min_mentions,
        )
        .order_by(first_seen.c.n.desc(), first_seen.c.first_seen)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return [
        EmergentEntity(
            entity_id=entity.id,
            canonical_name=entity.canonical_name,
            kind=entity.kind.value,
            first_mention_date=first_seen_at.date(),
            mention_count_since=n,
        )
        for entity, first_seen_at, n in session.execute(stmt).all()
    ]
