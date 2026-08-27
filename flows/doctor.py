#!/usr/bin/env python
"""Report pipeline completeness across the whole corpus.

    uv run python flows/doctor.py [--json]

Read-only and free — no API calls, no model loads, no quota. Run it before trusting a
"nothing found" result, and after any backfill that was interrupted.

Exit code is 1 when a stage is incomplete, so this can gate a scheduled job.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from corpus.config import get_settings
from corpus.db.session import tenant_session
from corpus.ingest.runner import resolve_tenant_id
from corpus.ops.doctor import diagnose, format_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)
    with tenant_session(tenant_id) as session:
        reports = diagnose(session, tenant_id=tenant_id)

    if args.json:
        print(json.dumps([{**asdict(r), "share": r.share} for r in reports], indent=2))
    else:
        print(format_report(reports))

    return 1 if any(r.status in ("partial", "empty") for r in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
