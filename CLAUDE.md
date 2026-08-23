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

Steps 1-3 of 11 complete: schema + RLS, the YouTube source adapter, and ingestion
(hand-rolled, not dlt — see `docs/DECISIONS.md`). 155 channels verified into
`seeds/youtube_channels.yaml`. A local dashboard (`web/`, backed by
`src/corpus/web/`) gives live ingestion progress, manual channel add, and
folder-watch — see `web/README.md`. Next is metadata/entity extraction (step 4).
The full build order and the reasoning behind every deviation from it are in
`docs/DECISIONS.md`.

## Reference

Read when the work calls for them, not by default.

- `docs/SUPADATA.md` — the confirmed Supadata API schema and what it does *not* provide
- `docs/SCHEMA.md` — table-by-table design notes and the RLS mechanics
- `docs/DECISIONS.md` — what was chosen, what was rejected, and why
