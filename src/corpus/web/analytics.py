"""Analytics routes.

One route, five analyses — a thin wrapper over `corpus.mcp.tools.corpus_analytics`,
which already dispatches to `corpus.analytics.{velocity,emergence,saturation,drift,
diffusion}` and returns JSON-shaped dicts. Deliberately not re-wrapping those five
modules separately here: a second dispatch layer would be a second place for the
argument rules (which analyses require `entity_name`, how `domain` is parsed) to
drift out of sync.

Fast — these are plain GROUP BY queries over `entity_mention`, no model inference
anywhere, so unlike the search panel there is no latency tradeoff to expose.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from mcp.server.mcpserver.exceptions import ToolError

from corpus.config import get_settings
from corpus.db.session import tenant_session
from corpus.ingest.runner import resolve_tenant_id
from corpus.mcp.tools import AnalysisType, corpus_analytics

router = APIRouter(prefix="/api", tags=["analytics"])


@router.get("/analytics")
def analytics(
    analysis: AnalysisType,
    domain: str | None = None,
    entity_name: str | None = None,
    entity_kind: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Run one analysis. `analysis` is validated by FastAPI against the same Literal
    the MCP tool uses, so an unknown value returns 422 before reaching the corpus.

    `co_occurrence_drift` and `diffusion_timeline` require `entity_name`; the others
    ignore it. Bad input (unknown domain, unresolvable entity, missing entity_name)
    surfaces as a 400 carrying the tool's own message, which is written to be read by
    a caller — "did you mean one of: [...]" rather than a bare failure.
    """
    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)
    try:
        with tenant_session(tenant_id) as session:
            return corpus_analytics(
                session,
                tenant_id=tenant_id,
                analysis=analysis,
                domain=domain,
                entity_name=entity_name,
                entity_kind=entity_kind,
                as_of=as_of,
            )
    except ToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        # `dt.date.fromisoformat` on a malformed as_of — a user typo, not a fault.
        raise HTTPException(status_code=400, detail=f"invalid as_of date: {exc}") from exc
