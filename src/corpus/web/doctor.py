"""Pipeline completeness route.

Read-only and free — no model loads, no API calls, no quota — so unlike most panels
this one can be polled without thinking about cost.

It belongs on the dashboard specifically because the dashboard is now the operations
console: day-to-day querying happens inside Claude over MCP, and what remains here is
ingestion, enrichment and knowing whether any of it actually finished.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter

from corpus.config import get_settings
from corpus.db.session import tenant_session
from corpus.ingest.runner import resolve_tenant_id
from corpus.ops.doctor import diagnose

router = APIRouter(prefix="/api", tags=["doctor"])


@router.get("/doctor")
def doctor() -> dict[str, Any]:
    """Per-stage completeness, with what silently degrades while a stage is partial."""
    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)
    with tenant_session(tenant_id) as session:
        reports = diagnose(session, tenant_id=tenant_id)
    return {
        "stages": [{**asdict(r), "share": r.share, "missing": r.missing} for r in reports],
        "incomplete": sum(1 for r in reports if r.status in ("partial", "empty")),
    }
