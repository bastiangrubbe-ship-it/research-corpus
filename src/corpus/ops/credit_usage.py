"""Credit usage: persisted record and reporting.

Supadata reports no credit consumption back to the caller — there is no endpoint
and no header confirming spend (see docs/SUPADATA.md). `credit_usage_event` is
therefore the only source of truth this system has, built from what we ourselves
recorded spending. "Credits remaining" below is our own estimate against the
configured monthly budget, not a number verified against Supadata's own dashboard.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corpus.db.models import CreditUsageEvent


@dataclass(frozen=True, slots=True)
class CreditUsageSummary:
    provider: str
    budget: int
    used_today: int
    used_this_month: int
    used_last_30_days: int
    avg_per_day_last_30_days: float
    remaining_estimate: int
    #: True once at least one event has ever been recorded. False means the log
    #: is empty — either nothing has been spent yet, or spend happened before
    #: this tracking existed and was never backfilled (see docs/DECISIONS.md).
    has_data: bool


def record_spend(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    provider: str,
    endpoint: str,
    external_id: str | None,
    credits: int,
) -> None:
    session.add(
        CreditUsageEvent(
            tenant_id=tenant_id,
            provider=provider,
            endpoint=endpoint,
            external_id=external_id,
            credits=credits,
        )
    )
    session.commit()


def summarize(
    session: Session, *, tenant_id: uuid.UUID, provider: str, budget: int
) -> CreditUsageSummary:
    now = dt.datetime.now(dt.UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = today_start.replace(day=1)
    window_30d_start = now - dt.timedelta(days=30)

    base = select(CreditUsageEvent).where(
        CreditUsageEvent.tenant_id == tenant_id, CreditUsageEvent.provider == provider
    )

    def _sum_since(since: dt.datetime) -> int:
        stmt = select(func.coalesce(func.sum(CreditUsageEvent.credits), 0)).where(
            CreditUsageEvent.tenant_id == tenant_id,
            CreditUsageEvent.provider == provider,
            CreditUsageEvent.occurred_at >= since,
        )
        return int(session.execute(stmt).scalar_one())

    used_today = _sum_since(today_start)
    used_this_month = _sum_since(month_start)
    used_last_30_days = _sum_since(window_30d_start)
    has_data = session.execute(select(base.exists())).scalar_one()

    return CreditUsageSummary(
        provider=provider,
        budget=budget,
        used_today=used_today,
        used_this_month=used_this_month,
        used_last_30_days=used_last_30_days,
        # Average over the full 30-day window, not just days with activity — a
        # quiet week should pull the average down, which is what "how much am I
        # burning per day, lately" actually needs to answer for budget planning.
        avg_per_day_last_30_days=used_last_30_days / 30,
        remaining_estimate=max(0, budget - used_this_month),
        has_data=has_data,
    )
