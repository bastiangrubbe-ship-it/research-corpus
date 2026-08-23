#!/usr/bin/env python
"""Print current Supadata credit usage.

    uv run python scripts/credit_usage.py

No "credits remaining" figure — that would only be `budget - what we logged`,
which is true only if every credit Supadata has ever charged went through this
tool. The numbers below are different in kind: exactly what this tool recorded
spending, true regardless of what happened elsewhere. See docs/SUPADATA.md.
"""

from __future__ import annotations

from corpus.config import get_settings
from corpus.db.session import tenant_session
from corpus.ingest.runner import resolve_tenant_id
from corpus.ops.credit_usage import summarize


def main() -> int:
    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)

    with tenant_session(tenant_id) as session:
        summary = summarize(
            session,
            tenant_id=tenant_id,
            provider="supadata",
            budget=settings.supadata_monthly_credits,
        )

    if not summary.has_data:
        print("No credit spend recorded yet.")
        print(f"Budget configured: {summary.budget}/month.")
        return 0

    print(f"Supadata credits — budget {summary.budget}/month")
    print(f"  used today:              {summary.used_today}")
    print(f"  used this calendar month: {summary.used_this_month}")
    print(f"  used, last 30 days:      {summary.used_last_30_days}")
    print(f"  average per day (30d):   {summary.avg_per_day_last_30_days:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
