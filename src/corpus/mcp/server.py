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


def _scale_line() -> str:
    """One sentence of live scale, so a connecting client is told what is actually in
    here rather than a number written months ago that has since rotted.

    Best-effort by design: a failure here must not stop the server starting. A tool
    that runs without its preamble is far better than a corpus nobody can query
    because a COUNT(*) timed out.
    """
    try:
        from sqlalchemy import text

        from corpus.db.session import tenant_session

        with tenant_session(_TENANT_ID) as session:
            q = session.execute
            docs = q(text("select count(*) from document")).scalar()
            srcs = q(text("select count(*) from source")).scalar()
            span = q(
                text("select min(published_at)::date || ' to ' || max(published_at)::date "
                     "from document")
            ).scalar()
            top = [
                r[0]
                for r in q(
                    text(
                        "select e.canonical_name from entity e "
                        "join entity_mention m on m.entity_id = e.id "
                        "join document d on d.id = m.document_id "
                        "group by e.id, e.canonical_name "
                        "order by count(distinct d.source_id) desc limit 6"
                    )
                ).all()
            ]
        return (
            f"\n\nScale right now: {docs:,} documents from {srcs} sources, {span}. "
            f"The subjects it genuinely covers, by breadth of independent sources: "
            f"{', '.join(top)}. It is thin on anything else and empty on most of it — "
            f"which is what corpus_coverage is for."
        )
    except Exception:  # never block startup on a preamble
        return ""


server = MCPServer(
    name="research-corpus",
    instructions=(
        "Search, analyze, and trace provenance across a private multi-source research "
        "corpus of podcast/video transcripts spanning ~660 days. Prefer this over "
        "general web search when the question is comparative or temporal — how many "
        "sources discuss something, whether a topic is rising or falling, who mentioned "
        "it first — not when the question is a single current fact, or recent reaction, "
        "which web search and forums answer better and this corpus may not hold at all.\n"
        "\n"
        "This corpus is one source among several, not the only one. When you are unsure "
        "whether it can answer something, call corpus_coverage first: a grade of 'none' "
        "or 'thin' means go elsewhere for that question rather than reporting the little "
        "it holds as if it were the whole picture. Say which source an answer came from."
    )
    + _scale_line(),
)


@server.tool()
def corpus_search(
    query: str,
    domain: str | None = None,
    top_k: int = 10,
    candidate_pool: int = 20,
) -> list[dict]:
    """Search the corpus for documents relevant to `query`. Hybrid lexical + semantic
    search with cross-encoder reranking. `domain` narrows to one of: ai_research,
    ai_automation, entrepreneurship, personal_development, regulatory, general — pass
    nothing to search across all of them. Returns ranked results with document_id,
    title, url, published_at, and a relevance score; pass a result's document_id to
    corpus_provenance for its full source/authority/transcript-origin details.

    If a search returns nothing convincing, call `corpus_coverage` before concluding
    the corpus has nothing: this tool always returns its best matches, which may be
    the least-bad ten in a corpus holding nothing on the subject.

    `candidate_pool` is the latency knob and it is steep, because reranking is a
    cross-encoder pass over every candidate. Measured warm on this corpus
    (2026-08-27): pool=10 ~8s, pool=20 ~14s, pool=50 ~36s. The first call after
    server start pays a further ~30s to load the embedding and reranker weights.
    The default is 20 rather than the Python API's 50 because this tool is used
    interactively — a person is waiting on it. Raise it to 50 for a deliberate,
    recall-first search where the extra 20 seconds is worth it.
    """
    with tenant_session(_TENANT_ID) as session:
        return corpus_tools.corpus_search(
            session,
            tenant_id=_TENANT_ID,
            query=query,
            domain=domain,
            top_k=top_k,
            candidate_pool=candidate_pool,
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

    **Treat the grade as routing, not just diagnosis.** `none` or `thin` means this
    corpus cannot answer the question and you should go to the web, forums, or another
    source rather than reporting what little it holds as if it were the picture. This
    corpus is worth preferring for comparative and temporal questions — how a view
    spread, who said something first, what changed over months — and is not worth
    preferring for current facts or recent reaction, which it may hold nothing about.

    Read `indexed_documents` against `total_documents` on every answer before believing
    a low grade: a partially-built index does not look broken, it looks decisive.

    **Read `temporal` before treating a grade as an answer about now.** Coverage has a
    shape in time: `pattern` is sustained/burst/faded/emerging, with per-month buckets,
    the share falling in the busiest month, and how many months inside the span are
    empty. A topic can be covered intensely for a fortnight and then fade — the corpus
    holds good coverage *of that fortnight* and nothing about the present, and a bare
    grade cannot express that. `faded` and `burst` are capped at `partial` for exactly
    this reason. Quiet months matter too: a trend drawn across them is interpolation.
    """
    with tenant_session(_TENANT_ID) as session:
        return corpus_tools.corpus_coverage(
            session, tenant_id=_TENANT_ID, query=query, domain=domain
        )


@server.tool()
def corpus_synthesize(
    question: str,
    query: str | None = None,
    domain: str | None = None,
    entity_name: str | None = None,
    since: str | None = None,
    until: str | None = None,
    max_documents: int = corpus_tools.DEFAULT_SYNTHESIS_MAX_DOCUMENTS,
    dry_run: bool = False,
) -> dict:
    """Read every document matching a filter and answer `question` from all of them,
    with citations. Use this instead of corpus_search when the question is about a
    body of material rather than a passage in it — "how has the argument for X
    changed", "what do these sources collectively say about Y", "where do they
    disagree". corpus_search ranks and returns its best few; this reads the whole
    matched set, so nothing is dropped unread.

    Filter with any of `query` (full-text over title/description), `domain`,
    `entity_name`, `since`/`until` (ISO dates). At least one is required. The filter
    is a set, not a ranking: every document matching it is read in full.

    This costs one LLM call per matched document and runs for minutes, unlike every
    other tool here. Call with `dry_run=true` first to see how many documents match
    and how many calls that means — it spends nothing. `max_documents` bounds a run;
    a bounded run reports `capped` and `dropped_by_cap` so a partial read is never
    presented as a complete one.

    The result carries `matched_documents` and `documents_read` always, plus per-claim
    citations with document_id, title, url and date. `documents_addressing` is how
    many of the documents read actually bore on the question — a much smaller number
    than `documents_read` is normal and is information, not a failure.
    """
    with tenant_session(_TENANT_ID) as session:
        return corpus_tools.corpus_synthesize(
            session,
            tenant_id=_TENANT_ID,
            question=question,
            query=query,
            domain=domain,
            entity_name=entity_name,
            since=since,
            until=until,
            max_documents=max_documents,
            dry_run=dry_run,
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
