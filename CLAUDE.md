# research-corpus

## What this is

A private multi-source research corpus that Claude Code queries as an MCP tool.
YouTube transcripts first, then RSS, arXiv, and other text. It serves four jobs:
content sourcing, vendor and market intelligence, client and prospect prep, and
regulatory tracking.

## Why it exists

**Its value is comparative and temporal, not factual.** Any single fact is better
retrieved by web search — do not build toward beating search on lookup. What search
cannot do is show how a view distributed across a thousand practitioners over three
years, or who said something before it became consensus.

That premise decides everything else. Effort goes into the metadata and analytics
layers that make comparison possible, not into passage-retrieval sophistication. The
system serves three distinct capabilities, and only the first is retrieval:

1. **Retrieval** — find the specific passage. Evidence, provenance, entity lookup.
2. **Aggregate analytics** — SQL over metadata and counts. No embeddings, no LLM.
   Term velocity, entity emergence, saturation, sentiment drift. Cheap and the most
   underserved.
3. **Filter-then-synthesize** — filter via SQL or lexical search, then reason over
   *all* of it, map-reduce style. Chunk retrieval is the wrong primitive here: the
   task needs everything on the topic, not the top ten.

## How to work on it

Data lives at `$PROJECT_DATA_DIR` (`~/data/research-corpus` here), never in this repo.
direnv sets it on `cd`; run `direnv allow` once after cloning.

```bash
docker compose up -d          # Postgres 17 + pgvector 0.8.6
uv run alembic upgrade head
uv run pytest                 # integration tests need the database up
```

Connection roles matter. `corpus_migrate` is a **superuser and bypasses RLS
unconditionally** — anything asserting tenant isolation must connect as `corpus_app`
via `APP_DATABASE_URL`, or it passes for the wrong reason.

### Rules that are load-bearing

- **No data in this repo, ever.** Paths come from `$PROJECT_DATA_DIR`, never
  hardcoded, never relative to the checkout. A hardcoded path is a bug even when it
  works — it is what would turn the eventual Linux server move into a rewrite.
- **Bronze is immutable.** Raw API responses are written once and never edited. If a
  parse was wrong, fix the parser and re-derive. Re-fetching is often impossible:
  rate limits, expired credentials, deleted upstream records, spent quota.
- **Never coalesce unknown metadata to a value.** `is_auto_generated IS NULL` means
  "the provider did not tell us", which is a different claim from "human-authored".
  The same applies to `published_at_precision` and `attribution_method`. Recording
  what is unknown is the point, not an oversight to tidy up.
- **No LLM inference in the ingest path.** At corpus scale a full pass is months of
  local compute. Summaries at ingest are extractive; abstractive synthesis happens on
  demand over a filtered set.
- **RLS is the authorization boundary.** An MCP tool argument or a model's output is
  never the tenant. The server resolves tenant from its own config.

## Current state

Steps 1-5 and 7-10 complete. The corpus holds **3,349 documents** from 169 YouTube
channels plus RSS, fully enriched: 292k entity mentions, 3,267 document summaries,
**76k transcript chunks** with embeddings, and speaker attribution over every document.

Punctuation restoration was dropped on 2026-08-27: 94% of transcripts already arrive
adequately punctuated, its justification was falsified twice by measurement, and it
cost half the segments in the database (`docs/DECISIONS.md`).

Speaker attribution leaves 54% of documents at `attribution_method='unknown'`. That
is the designed outcome for tier-1 heuristics over metadata, not a shortfall — "we
could not tell" is a different claim from a guess, and the column records which.

- **Retrieval** is three lanes fused with RRF then reranked: lexical (Postgres FTS),
  document-level dense (summary embeddings), and chunk-level dense (transcript
  windows). Measured lane precision/recall is in `docs/EVAL_RELEVANCE_GATE.md`.
- **Analytics** (velocity, emergence, saturation, drift, diffusion) answer Q7/Q8
  with no embeddings and no LLM.
- **Synthesis** (`synthesis/mapreduce.py`) reads *every* document matching a SQL/
  full-text filter and reduces them to cited prose. Unlike the other three, it costs
  one LLM call per matched document — always `dry_run`/`/api/synthesis/plan` first, and
  note that a capped run reports what it dropped rather than presenting a partial read
  as complete.
- **MCP** exposes all five tools: `corpus_search`, `corpus_analytics`,
  `corpus_coverage`, `corpus_synthesize`, `corpus_provenance`.
- **`flows/doctor.py`** reports per-stage completeness across the corpus and names
  what silently degrades while a stage is partial. Free and read-only; exits 1 when
  anything is incomplete. Run it before believing a "nothing found" result.
- **Query logging** (`query_log`, on by default) records what was asked and how
  coverage graded it, so repeated weak coverage becomes a sourcing backlog rather than
  evaporating per response — `analytics/query_insights.py`. It is the most sensitive
  table here; `CORPUS_LOG_QUERIES=false` disables it.
- **The dashboard** (`web/`, backed by `src/corpus/web/`) has 14 panels covering all
  of the above plus the original ingestion controls — see `web/README.md`.

Not done: step 0 (baseline capture), step 6's chunk-level *re-embedding* tooling, and
step 11 (Linux dry run).

**Two standing cautions, learned the hard way** (both in `docs/DECISIONS.md`):
a partially-built index does not look broken, it looks decisive — assert index
completeness before trusting a "nothing found" result; and "confirmed N ways" is
worth little when every confirmation runs through the same substrate.

The first caution has now cost three separate bugs (a 3.9% index, a mislabelled date
range, and a near-miss that would have zeroed every chunk timestamp). When a pass
writes something every later stage reads, check what it wrote before trusting what it
reports.

The full build order and the reasoning behind every deviation from it are in
`docs/DECISIONS.md`.

## Reference

Read when the work calls for them, not by default.

- `docs/BUILD_PLAN.md` — the original 11-step plan, recovered 2026-08-26. Read it for the
  *criteria*, not the build order: it defines the ten spec queries and the kill criterion
  that decide whether this corpus is worth keeping. Those ten queries no longer exist as
  text, so step 0 is still blocked — see the header there
- `docs/FEDERATION.md` — how this corpus relates to realtime search (web, Reddit, X) when
  driven from inside Claude, and why the fan-out layer is deliberately *not* built here
- `docs/SUPADATA.md` — the confirmed Supadata API schema and what it does *not* provide
- `docs/USING_THE_CORPUS.md` — a self-contained brief to paste into a clean chat: what
  the corpus holds, which tool answers what, and the five things that mislead a caller
  who does not know them
- `docs/PIPELINE.md` — the stage graph, the ordering constraints that have actually
  caused outages, and how to verify a change. Start here to fix or extend the pipeline
- `docs/DECISIONS.md` — what was chosen, what was rejected, and why
- `docs/EVAL.md` — entity extraction: rubric, scoring, and extractor comparison results
- `docs/EVAL_RELEVANCE_GATE.md` — first-pass precision check on the local embedding
  relevance gate that decides which documents get a `claude -p` call
- `web/README.md` — running the dashboard (14 panels: pipeline health, search,
  synthesis, coverage, analytics, eval runs, MCP tools, enrichment triggers, RSS feeds,
  plus the original ingestion set)
