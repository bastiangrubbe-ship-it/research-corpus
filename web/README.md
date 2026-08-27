# research-corpus dashboard

Local dashboard over the whole corpus: pipeline health, search and provenance,
synthesis, analytics, coverage, eval history, the MCP tool catalogue, enrichment
triggers, and the original ingestion controls.

Day-to-day querying now happens inside Claude over MCP; what remains most useful here
is the operations half — ingesting, enriching, and knowing whether any of it finished. Talks to the FastAPI backend in `src/corpus/web/` (this
project's Python side) over HTTP + SSE.

Built on [`@bastiangrubbe/ui-kit`](https://github.com/bastiangrubbe-ship-it/ui-kit) —
`ProgressBar` and `StatTile` are components from there, not local to this app.

## Run it

Two processes, both local-only:

```bash
# from the repo root
uv run uvicorn corpus.web.app:app --host 127.0.0.1 --port 8420

# from web/
pnpm install
pnpm dev
```

Open <http://localhost:5173>. The backend binds to `127.0.0.1` only and the two
ports are hardcoded on both sides (CORS in `corpus/web/app.py`, `server.port` in
`vite.config.ts`) — this is a local single-operator tool, not meant to be
network-reachable.

## What it does

Fourteen panels. `App.tsx` is a shell; each panel is its own component under
`src/components/`, sharing one `App.module.css`.

### Operations

- **Pipeline health** — which stages actually ran over the whole corpus, and what is
  silently degraded while one is partial. Free and read-only, so refresh freely. Check
  it before believing a "nothing found" result: the recurring failure here is not a
  stage breaking, it is a stage running over part of the corpus while everything
  downstream keeps working confidently over whatever fraction exists.

### Reading the corpus

- **Search** — the full hybrid stack: lexical + document-dense + chunk-dense, RRF-fused
  and reranked. Rows expand to show provenance, and `document_id` is copyable because
  the restore panel takes one.
- **Synthesize** — the opposite trade to search. Reads *every* document matching a
  filter, in full, and answers from all of them with citations. This is the only panel
  that spends real quota per use (one LLM call per matched document, minutes not
  seconds), so it prices the filter as you type and will not run unfiltered. A capped
  run says how many documents it did not read, right next to the answer — a partial
  read presented as a complete one is the specific failure this panel is built to
  avoid.
- **Coverage** — how well the corpus covers a topic (none/thin/partial/good) and what
  would improve it. It always reports how much of the corpus is actually indexed;
  read that number before believing a low grade.
- **Analytics** — velocity, emergence, saturation, drift, diffusion. No embeddings and
  no LLM behind these, so they are the cheapest answers here.
- **Eval history** — past runs, per-lane precision/recall, and run-vs-run diffs. Note
  that the query set is *not* the build plan's specification; `src/corpus/eval/queries.yaml`
  explains what that means before you read a score as a passing grade.
- **MCP tools** — a catalogue and tester for the four MCP tools. A catalogue, not a
  monitor: the MCP server is a stdio subprocess per client, not a daemon, so there is
  no running process to watch.

### Changing the corpus

- **Enrichment** — triggers entity backfill, speaker attribution, and transcript
  restoration. These are unbounded by design, matching what the CLI already allows: a
  full-corpus backfill is one click and spends real subscription quota. Re-running is
  idempotent, which is why nothing here asks you to confirm.
- **RSS feeds** — preview a feed, then add and ingest. The preview step is load-bearing:
  `feedparser` returns zero entries for a dead or mistyped URL rather than raising, so
  without it a broken feed adds cleanly and then reports "0 fetched", which looks
  exactly like a feed with nothing new.

### Ingestion (the original set)

- **Run a channel** — starts an ingestion run against one seed-table entry, streams
  live progress (discovering → fetching → fetched/skipped/failed → done) over SSE.
- **Add a channel** — paste a channel URL, a bare `@handle`, or a single video URL
  (auto-resolves to its parent channel). Appends to `seeds/youtube_channels.yaml`
  with `domain`/`authority_tier` set to `unknown`, pending review — never bypasses
  that file.
- **Watch a folder** — configures a server-side `watchdog` observer on a path you
  type in. Browsers cannot continuously watch a filesystem path themselves; see
  `docs/DECISIONS.md` in the repo root for why this couldn't be a browser directory
  picker.
- **Seed table** — a live read of the same YAML file everything else writes to.

## Develop

```bash
pnpm typecheck
pnpm lint
pnpm build
```
