"""Who talked about something first, and who followed — the corpus's temporal
comparative value made concrete (see the build plan's opening framing: "who said
something before it was consensus").

One row per source, not per mention: the question is which independent sources
picked this entity up and in what order, not how many times any one of them repeated
it (that's `corpus.analytics.saturation`'s question, not this one's).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corpus.db.enums import DatePrecision
from corpus.db.models import Document, EntityMention, Source


@dataclass(frozen=True, slots=True)
class SourceFirstMention:
    source_id: uuid.UUID
    source_title: str | None
    first_mention_date: dt.date


def diffusion_timeline(
    session: Session, *, tenant_id: uuid.UUID, entity_id: uuid.UUID
) -> list[SourceFirstMention]:
    """Every source that has ever mentioned this entity, one row each, ordered by
    that source's *first* mention of it — the adoption sequence across the corpus's
    independent sources. Across all domains deliberately: which domains picked
    something up first (e.g. research content before automation-tutorial content)
    is itself part of the diffusion story, not noise to filter out.
    """
    stmt = (
        select(
            Document.source_id,
            func.min(Document.published_at).label("first_seen"),
        )
        .select_from(EntityMention)
        .join(Document, Document.id == EntityMention.document_id)
        .where(
            EntityMention.tenant_id == tenant_id,
            EntityMention.entity_id == entity_id,
            Document.published_at.is_not(None),
            Document.published_at_precision != DatePrecision.UNKNOWN,
        )
        .group_by(Document.source_id)
        .order_by(func.min(Document.published_at))
    )
    rows = session.execute(stmt).all()

    sources = {
        row.id: row
        for row in session.execute(
            select(Source).where(
                Source.tenant_id == tenant_id, Source.id.in_([r.source_id for r in rows])
            )
        )
        .scalars()
        .all()
    }

    return [
        SourceFirstMention(
            source_id=row.source_id,
            source_title=sources[row.source_id].title if row.source_id in sources else None,
            first_mention_date=row.first_seen.date(),
        )
        for row in rows
    ]
