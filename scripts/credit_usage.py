#!/usr/bin/env python
"""Print current Supadata credit usage.

    uv run python scripts/credit_usage.py

"credits remaining" here is our own estimate against the configured monthly
budget (SUPADATA_MONTHLY_CREDITS) — Supadata itself reports no consumption back
to the caller, so this number can drift from what their dashboard shows if any
spend ever happens outside this tool. See docs/SUPADATA.md.
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
    print(f"  remaining (estimate):    {summary.remaining_estimate}")
    print()
    print("Estimate only — Supadata reports no usage back to the caller;")
    print("this reflects only what this tool has itself recorded spending.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
