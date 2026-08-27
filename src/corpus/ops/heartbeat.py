"""Writing the dead-man's switch that `db.models.Heartbeat` was designed for.

The model existed from the start, with a docstring naming precisely the failure it was
meant to catch: "a job that failed silently and a job that never ran both look
identical here — which is exactly the property log-scraping lacks." Nothing ever wrote
to it. The nightly ingest then failed on four consecutive nights and nobody noticed,
because the only evidence was a traceback in a log that gets opened when something
already looks wrong (docs/DECISIONS.md, 2026-08-27).

A heartbeat inverts that. Instead of needing to notice a failure, you notice the
absence of a success — which shows up on its own the moment anything asks "when did
this last work?"

`record_failure` exists so a run that dies loudly is distinguishable from one that
never started. Both are bad; they call for different fixes.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from corpus.db.models import Heartbeat


def _upsert(session: Session, *, tenant_id: uuid.UUID, name: str, values: dict) -> None:
    stmt = pg_insert(Heartbeat).values(tenant_id=tenant_id, flow_name=name, **values)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Heartbeat.tenant_id, Heartbeat.flow_name], set_=values
        )
    )
    session.commit()


def record_heartbeat(
    session: Session, *, tenant_id: uuid.UUID, name: str, detail: str | None = None
) -> None:
    """Mark `name` as having completed successfully, now."""
    _upsert(
        session,
        tenant_id=tenant_id,
        name=name,
        values={
            "last_success_at": dt.datetime.now(dt.UTC),
            "last_status": "ok",
            "detail": detail,
        },
    )


def record_failure(
    session: Session, *, tenant_id: uuid.UUID, name: str, detail: str | None = None
) -> None:
    """Mark `name` as having failed. `last_success_at` is deliberately left alone —
    it answers "when did this last actually work", and a failure does not change that.
    """
    _upsert(
        session,
        tenant_id=tenant_id,
        name=name,
        values={"last_status": "failed", "detail": (detail or "")[:2000]},
    )


def stale_flows(
    session: Session, *, tenant_id: uuid.UUID, max_age_hours: float
) -> list[tuple[str, dt.datetime | None, str | None]]:
    """(flow_name, last_success_at, last_status) for flows that have not succeeded
    within `max_age_hours`. A flow that has never succeeded is included, with None."""
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=max_age_hours)
    rows = session.execute(
        Heartbeat.__table__.select().where(Heartbeat.tenant_id == tenant_id)
    ).all()
    return [
        (r.flow_name, r.last_success_at, r.last_status)
        for r in rows
        if r.last_success_at is None or r.last_success_at < cutoff
    ]
