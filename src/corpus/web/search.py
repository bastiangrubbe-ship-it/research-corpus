"""Search and provenance routes.

Thin HTTP wrappers over `corpus.mcp.tools` — the same two functions the MCP server
exposes to Claude Code, deliberately reused rather than re-wrapped. Whatever the
dashboard shows here is exactly what an MCP client sees for the same query, so a
result that looks wrong in the browser is a real retrieval bug, never a
dashboard-only one.

Tenant comes from server config via `resolve_tenant_id`, never from a query
parameter — same boundary `corpus.mcp.server` draws for the same reason.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from mcp.server.mcpserver.exceptions import ToolError

from corpus.config import get_settings
from corpus.db.session import tenant_session
from corpus.ingest.runner import resolve_tenant_id
from corpus.mcp.tools import corpus_provenance, corpus_search

router = APIRouter(prefix="/api", tags=["search"])


@router.get("/search")
def search(
    query: str = Query(..., min_length=1),
    domain: str | None = None,
    top_k: int = Query(10, ge=1, le=50),
    candidate_pool: int = Query(20, ge=1, le=100),
    rerank: bool = True,
) -> list[dict[str, Any]]:
    """Hybrid lexical + dense retrieval, RRF-fused and cross-encoder reranked.

    Two separate costs worth knowing about, both measured on this corpus:

    * Cold start — the first call after a server start pays ~30s to load the
      reranker and embedding weights. Cached for the process lifetime afterward.
    * Per query — dominated by `candidate_pool`, since reranking is a cross-encoder
      pass over every candidate: pool=50 ~37s, pool=20 ~6s, pool=10 ~3s warm.

    `candidate_pool` defaults to 20 here rather than `corpus_search`'s own 50: an
    agent can wait 37s for the best possible recall, a person typing in a browser
    cannot. Callers wanting the MCP-equivalent result pass `candidate_pool=50`.
    """
    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)
    try:
        with tenant_session(tenant_id) as session:
            return corpus_search(
                session,
                tenant_id=tenant_id,
                query=query,
                domain=domain,
                top_k=top_k,
                candidate_pool=candidate_pool,
                rerank=rerank,
            )
    except ToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/documents/{document_id}/provenance")
def provenance(document_id: str) -> dict[str, Any]:
    """Where one document came from and how much of that we actually know.

    `is_auto_generated: null` means the provider never said — not "human-authored".
    The paired `provenance_confidence` records which of those two situations applies.
    """
    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)
    try:
        with tenant_session(tenant_id) as session:
            return corpus_provenance(session, tenant_id=tenant_id, document_id=document_id)
    except ToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
