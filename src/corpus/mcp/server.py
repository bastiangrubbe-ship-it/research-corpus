#!/usr/bin/env python
"""The corpus's MCP surface — the build plan's step 9.

    uv run python -m corpus.mcp.server

Tenant is resolved exactly once, here, at process startup, from server config
(`resolve_tenant_id`, the same function every other entry point in this codebase
uses) — never from a tool argument, never from anything a caller's model says. A
malicious or merely careless tool argument cannot escape the tenant boundary because
no tool argument schema below has a tenant field to escape through in the first
place. This is the plan's own stated requirement, not an incidental design choice.

Each tool opens its own `tenant_session`, used only for the duration of that one
call — no session is held across calls or shared between them.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from corpus.config import get_settings
from corpus.db.session import tenant_session
from corpus.ingest.runner import resolve_tenant_id
from corpus.mcp import tools as corpus_tools

_settings = get_settings()
_TENANT_ID = resolve_tenant_id(_settings)

server = MCPServer(
    name="research-corpus",
    instructions=(
        "Search, analyze, and trace provenance across a private multi-source research "
        "corpus of podcast/video transcripts. Prefer this over general web search when "
        "the question is comparative or temporal — how many sources discuss something, "
        "whether a topic is rising or falling, who mentioned it first — not when the "
        "question is a single current fact web search already answers well."
    ),
)


@server.tool()
def corpus_search(query: str, domain: str | None = None, top_k: int = 10) -> list[dict]:
    """Search the corpus for documents relevant to `query`. Hybrid lexical + semantic
    search with cross-encoder reranking. `domain` narrows to one of: ai_research,
    ai_automation, entrepreneurship, personal_development, regulatory, general — pass
    nothing to search across all of them. Returns ranked results with document_id,
    title, url, published_at, and a relevance score; pass a result's document_id to
    corpus_provenance for its full source/authority/transcript-origin details.
    """
    with tenant_session(_TENANT_ID) as session:
        return corpus_tools.corpus_search(
            session, tenant_id=_TENANT_ID, query=query, domain=domain, top_k=top_k
        )


@server.tool()
def corpus_analytics(
    analysis: corpus_tools.AnalysisType,
    domain: str | None = None,
    entity_name: str | None = None,
    entity_kind: str | None = None,
    as_of: str | None = None,
) -> dict:
    """Run one of the corpus's analytics: `rising_entities` (what's discussed more
    now than 6 months ago), `emerging_entities` (what's new in the discourse),
    `saturated_entities` (discussed by the widest spread of independent sources, not
    just mentioned often), `co_occurrence_drift` (how the company an entity keeps has
    shifted over time — requires `entity_name`), or `diffusion_timeline` (which
    sources picked up an entity first and in what order — requires `entity_name`).
    `domain` follows the same convention as corpus_search. `entity_kind` (vendor/
    person/technique/regulation/product/organization) disambiguates a name that
    exists under more than one kind (e.g. "Claude" is both a product and a vendor);
    omit it to get whichever reading has the most mentions. `as_of` is an ISO date
    (defaults to today) — analytics compare time windows ending at this date.
    """
    with tenant_session(_TENANT_ID) as session:
        return corpus_tools.corpus_analytics(
            session,
            tenant_id=_TENANT_ID,
            analysis=analysis,
            domain=domain,
            entity_name=entity_name,
            entity_kind=entity_kind,
            as_of=as_of,
        )


@server.tool()
def corpus_coverage(query: str, domain: str | None = None) -> dict:
    """Assess how well this corpus covers a topic before relying on it. Returns a
    grade (none/thin/partial/good), the supporting counts (documents, distinct
    sources, date span, domain and authority breakdown), and concrete suggestions
    for improving weak coverage. Call this when the answer matters and you need to
    know whether silence means "no" or means "this corpus can't say" — corpus_search
    always returns its best matches, even when the best is nothing much.
    """
    with tenant_session(_TENANT_ID) as session:
        return corpus_tools.corpus_coverage(
            session, tenant_id=_TENANT_ID, query=query, domain=domain
        )


@server.tool()
def corpus_provenance(document_id: str) -> dict:
    """Full provenance for one document (from a corpus_search result's document_id):
    source, authority tier, domain, publication date and how precisely it's known,
    and transcript origin — whether it's an auto-generated (ASR) transcript or a real
    caption track, and how confident that signal itself is.
    """
    with tenant_session(_TENANT_ID) as session:
        return corpus_tools.corpus_provenance(
            session, tenant_id=_TENANT_ID, document_id=document_id
        )


if __name__ == "__main__":
    server.run(transport="stdio")
