"""Synthesis routes — plan first, then read.

A thin wrapper over `corpus.mcp.tools.corpus_synthesize`, reused rather than
re-wrapped so the browser and an MCP client get identical answers for identical
arguments.

Two routes rather than one, because this is the only capability in the dashboard
whose cost depends on what the user typed and is not knowable in advance. `/plan`
answers "how many documents does this filter match, and therefore how many LLM calls
and how long" without spending anything; `/run` does the work. Every other panel can
just run — `corpus_search` costs seconds and `corpus_analytics` is plain SQL — so
none of them needed this and this one does.

The panel is expected to call `/plan` on every filter change and `/run` only on an
explicit click. That is not merely a nicety: an unbounded filter on this corpus
matches over a thousand documents, and a run is one LLM call per document.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from mcp.server.mcpserver.exceptions import ToolError

from corpus.config import get_settings
from corpus.db.session import tenant_session
from corpus.ingest.runner import resolve_tenant_id
from corpus.mcp.tools import DEFAULT_SYNTHESIS_MAX_DOCUMENTS, corpus_synthesize

router = APIRouter(prefix="/api/synthesis", tags=["synthesis"])


def _call(*, dry_run: bool, **kwargs: Any) -> dict[str, Any]:
    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)
    try:
        with tenant_session(tenant_id) as session:
            return corpus_synthesize(session, tenant_id=tenant_id, dry_run=dry_run, **kwargs)
    except ToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/plan")
def plan(
    question: str = Query("(planning)", min_length=1),
    query: str | None = None,
    domain: str | None = None,
    entity_name: str | None = None,
    since: str | None = None,
    until: str | None = None,
    max_documents: int = Query(DEFAULT_SYNTHESIS_MAX_DOCUMENTS, ge=1, le=2000),
) -> dict[str, Any]:
    """What a run would cost. Spends nothing.

    `question` is irrelevant to the cost — the filter decides it — so it defaults to a
    placeholder here, letting the panel price a filter before the user has finished
    writing the question.
    """
    return _call(
        dry_run=True,
        question=question,
        query=query,
        domain=domain,
        entity_name=entity_name,
        since=since,
        until=until,
        max_documents=max_documents,
    )


@router.post("/run")
def run(
    question: str = Query(..., min_length=1),
    query: str | None = None,
    domain: str | None = None,
    entity_name: str | None = None,
    since: str | None = None,
    until: str | None = None,
    max_documents: int = Query(DEFAULT_SYNTHESIS_MAX_DOCUMENTS, ge=1, le=2000),
) -> dict[str, Any]:
    """Read every matched document and answer the question with citations.

    Blocking, and genuinely slow — minutes, not seconds. There is no SSE stream here
    because `synthesize` fans out with a ThreadPoolExecutor and reduces at the end;
    per-document completions arrive in a jumbled order and mean little individually,
    so a progress bar over them would be closer to decoration than information. The
    panel shows the plan it is working through instead, which is the number the user
    actually wants while waiting.
    """
    return _call(
        dry_run=False,
        question=question,
        query=query,
        domain=domain,
        entity_name=entity_name,
        since=since,
        until=until,
        max_documents=max_documents,
    )
