"""Coverage-assessment route.

Answers the question a ranked result list structurally cannot: is there actually
anything here, how broad is it, and what would make it better. See
`corpus.analytics.coverage` for the measurement decisions behind it.

Slower than the analytics routes (~25s) because it reranks a wide candidate pool —
breadth is the whole point, so the pool can't be narrow. Deliberate analysis action,
not something to fire on keystroke.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from mcp.server.mcpserver.exceptions import ToolError

from corpus.analytics.coverage import assess_coverage
from corpus.config import get_settings
from corpus.db.enums import Domain
from corpus.db.session import tenant_session
from corpus.ingest.runner import resolve_tenant_id

router = APIRouter(prefix="/api", tags=["coverage"])


@router.get("/coverage")
def coverage(
    query: str = Query(..., min_length=1),
    domain: str | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)

    domain_enum: Domain | None = None
    if domain:
        try:
            domain_enum = Domain(domain)
        except ValueError as exc:
            valid = ", ".join(d.value for d in Domain)
            raise HTTPException(
                status_code=400, detail=f"unknown domain {domain!r}; valid values: {valid}"
            ) from exc

    try:
        with tenant_session(tenant_id) as session:
            report = assess_coverage(session, tenant_id=tenant_id, query=query, domain=domain_enum)
    except ToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    payload = asdict(report)
    # dataclass tuples serialize as JSON arrays; dates need explicit isoformat.
    payload["date_earliest"] = report.date_earliest.isoformat() if report.date_earliest else None
    payload["date_latest"] = report.date_latest.isoformat() if report.date_latest else None
    return payload
