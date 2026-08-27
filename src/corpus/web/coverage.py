"""Coverage-assessment route.

Answers the question a ranked result list structurally cannot: is there actually
anything here, how broad is it, and what would make it better. See
`corpus.analytics.coverage` for the measurement decisions behind it.

Slower than the analytics routes (~25s) because it reranks a wide candidate pool —
breadth is the whole point, so the pool can't be narrow. Deliberate analysis action,
not something to fire on keystroke.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from mcp.server.mcpserver.exceptions import ToolError

from corpus.config import get_settings
from corpus.db.session import tenant_session
from corpus.ingest.runner import resolve_tenant_id
from corpus.mcp.tools import corpus_coverage

router = APIRouter(prefix="/api", tags=["coverage"])


@router.get("/coverage")
def coverage(
    query: str = Query(..., min_length=1),
    domain: str | None = None,
) -> dict[str, Any]:
    """Delegates to `corpus.mcp.tools.corpus_coverage` rather than calling
    `assess_coverage` directly, as it used to.

    That indirection buys one thing worth having: a single place where a coverage
    verdict is written to `query_log`. Two call sites would mean the dashboard's
    verdicts silently missing from the sourcing backlog, which is exactly the kind of
    partial record that reads as complete.
    """
    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)
    try:
        with tenant_session(tenant_id) as session:
            return corpus_coverage(
                session, tenant_id=tenant_id, query=query, domain=domain, surface="web"
            )
    except ToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
