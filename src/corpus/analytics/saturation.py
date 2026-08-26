"""How widespread an entity is across sources, not just how often it's mentioned.

The distinction matters because raw mention count conflates two different things: one
prolific channel repeating itself 200 times, versus 50 different creators each
mentioning something once. The first is one person's opinion; the second is the
corpus's actual comparative value — a view distributed across many independent
sources, which is the premise the whole project is built on (see the build plan's
opening framing). Saturation is the second signal, deliberately separated from volume.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corpus.db.enums import Domain, SourceStatus
from corpus.db.models import Document, Entity, EntityMention, Source


def source_breadth(session: Session, *, tenant_id: uuid.UUID, entity_id: uuid.UUID) -> int:
    """Count of distinct sources (channels) with at least one mention of this
    entity — across all domains, since "how many independent sources talk about
    this" is the same question regardless of domain grouping.
    """
    stmt = (
        select(func.count(func.distinct(Document.source_id)))
        .select_from(EntityMention)
        .join(Document, Document.id == EntityMention.document_id)
        .where(EntityMention.tenant_id == tenant_id, EntityMention.entity_id == entity_id)
    )
    return session.execute(stmt).scalar_one()


@dataclass(frozen=True, slots=True)
class SaturationResult:
    entity_id: uuid.UUID
    canonical_name: str
    kind: str
    distinct_sources: int
    total_mentions: int

    @property
    def mentions_per_source(self) -> float:
        """High values flag the "one loud channel" case this module exists to
        separate from genuine breadth — a large number here alongside a small
        `distinct_sources` is the tell, not a good sign on its own."""
        return self.total_mentions / self.distinct_sources if self.distinct_sources else 0.0


def most_saturated_entities(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    domain: Domain | None,
    min_sources: int = 3,
    limit: int = 20,
) -> list[SaturationResult]:
    """Entities mentioned by the widest spread of independent sources within
    `domain`, ranked by `distinct_sources` (not total mentions — that's exactly the
    volume-vs-breadth conflation this module exists to avoid). `min_sources` drops
    single-source mentions outright rather than ranking them at the bottom; they
    aren't a weak saturation signal, they're a different thing (see
    `corpus.analytics.emergence` for "is this new" instead of "how widespread").
    """
    breadth = (
        select(
            EntityMention.entity_id,
            func.count(func.distinct(Document.source_id)).label("n_sources"),
            func.count().label("n_mentions"),
        )
        .select_from(EntityMention)
        .join(Document, Document.id == EntityMention.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(EntityMention.tenant_id == tenant_id, Source.status == SourceStatus.ACTIVE)
    )
    if domain is not None:
        breadth = breadth.where(Source.domain == domain)
    breadth = breadth.group_by(EntityMention.entity_id).having(
        func.count(func.distinct(Document.source_id)) >= min_sources
    )
    breadth = breadth.subquery()

    stmt = (
        select(Entity, breadth.c.n_sources, breadth.c.n_mentions)
        .join(breadth, breadth.c.entity_id == Entity.id)
        .where(Entity.tenant_id == tenant_id)
        .order_by(breadth.c.n_sources.desc())
        .limit(limit)
    )
    return [
        SaturationResult(
            entity_id=entity.id,
            canonical_name=entity.canonical_name,
            kind=entity.kind.value,
            distinct_sources=n_sources,
            total_mentions=n_mentions,
        )
        for entity, n_sources, n_mentions in session.execute(stmt).all()
    ]
