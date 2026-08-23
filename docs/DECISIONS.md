# Decisions

Newest first. One entry per decision that would be expensive to reverse, or that would
look arbitrary to someone arriving later.

The rejected options are the valuable part. They are what stops the same debate from
being had twice, and what tells a future reader whether the alternative was considered
and dismissed or simply never thought of.

---

## 2026-08-23 — No "credits remaining" figure, on request

**Chose:** Report only `used_today` / `used_this_month` / `used_last_30_days` /
`avg_per_day_last_30_days` — all four provable facts about what this tool has
itself recorded spending.

**Rejected:** `remaining_estimate = budget - used_this_month`, built and shipped
earlier the same day.

**Why:** Flagged directly: it would be wrong most of the time. It can only ever
be correct if every credit Supadata has ever charged went through this tool —
one manual API test, one other integration touching the same key, or spend
predating this table's existence, and the number is silently false with no way
to detect it. The `used_*` figures don't have that failure mode: they are
exactly what got logged, true regardless of anything happening elsewhere.
Removed from the summary dataclass, the CLI, the API response, and the
dashboard panel, rather than left computed-but-hidden in only some of them.

## 2026-08-23 — Credit usage is a persisted event log, not a bigger in-memory counter

**Chose:** `credit_usage_event` — one row per spend, with `endpoint` and
`external_id` kept alongside the count, written the moment `CreditLedger.reserve()`
succeeds via an `on_spend` callback (same pattern as `IngestEvent`/`EventSink`).

**Rejected:** A single running-total row per day, incremented in place.

**Why:** Supadata has no endpoint or header reporting credit consumption back to
the caller — confirmed against the live API in step 2. `CreditLedger` is
in-memory and resets to zero every process restart, so without persistence
"credits used" and "credits left" simply didn't exist between runs. An event log
costs nothing extra at this volume and, unlike a rolling counter, can answer
"which channel caused today's spike" after the fact, not just "how much."

"Credits remaining" is reported everywhere as an estimate against the configured
budget, never as a number confirmed by Supadata — because it categorically
cannot be confirmed by them. Every surface (CLI, API, dashboard) says so
explicitly rather than presenting a guess as a fact.

## 2026-08-23 — Dashboard frontend lives in web/, its own npm-tooled subtree

**Chose:** A `web/` directory inside this repo, its own `package.json`/pnpm
lockfile, consuming `@bastiangrubbe/ui-kit` as a git dependency.

**Rejected:** A separate repo for the dashboard frontend.

**Why:** Unlike `ui-kit`, this frontend has exactly one reason to exist — it is
this project's operational dashboard, not something another project will ever
depend on. `ui-kit` earned its own repo because it's meant to be reused;
`web/` doesn't meet that bar, so it stays where the thing it operates on lives.

## 2026-08-23 — Folder-watch is server-side; a browser cannot continuously watch a path

**Chose:** The dashboard's "select a folder to monitor" control is a text path
input; the actual watching (`watchdog`) runs in the Python backend.

**Rejected:** A browser directory-picker that watches continuously.

**Why:** A plain `<input type=file webkitdirectory>` reads a directory's contents
once, at selection time — it cannot maintain an ongoing watch. The File System
Access API (`showDirectoryPicker()`) is Chrome/Edge-only, requires the tab to stay
open with a granted permission, and still only supports polling a handle
periodically, not push notification of changes. True continuous folder-watching
is an operating-system-level capability a webpage does not have; it has to live in
the server process that's actually running.

## 2026-08-23 — Seed-table writes append one entry's YAML, never re-dump the whole file

**Chose:** `_append_row()` serializes a one-item list and appends those lines to
the file, touching nothing before them.

**Rejected:** Loading the full seed list, appending in memory, and re-writing the
entire file via `yaml.safe_dump` — the first implementation, caught in testing
before it shipped.

**Why:** `yaml.safe_dump` on the full 155-row list reformats every existing row's
quoting and blank-line spacing, turning a single manual-add into a
1,000+-line diff. Verified directly: the full-rewrite version diffed the entire
file for a two-row change; the append-only version diffs exactly the 8 lines
added. The whole point of this file being git-tracked is that a reviewer can see
what changed — a formatter-driven diff the size of the file defeats that.

## 2026-08-23 — Ingestion emits typed progress events; CLI and web share one loop

**Chose:** `ingest_source()` takes an `on_event: EventSink` callback, emitting an
`IngestEvent` at each step (discovering, discovered, fetching, fetched, skipped,
failed, budget_exceeded, done). The CLI passes a sink that prints; the web
dashboard passes one that pushes onto an SSE queue.

**Rejected:** Building the dashboard's live-progress view by having the web layer
re-implement the discover/fetch/persist loop with its own instrumentation.

**Why:** Two copies of "what ingesting a channel means" will drift — a fix or a
new event kind in one won't reach the other. One loop, two sinks, is the same
shape as the source-adapter split between transcript providers: swap what
consumes the events, never duplicate what produces them.

## 2026-08-23 — dlt rejected; ingestion is hand-rolled against the existing schema

**Chose:** A plain `ingest_source()` function using the adapter contract, the bronze
store, and `document`'s own unique constraint for dedup.

**Rejected:** `dlt` (data load tool), named in the original build plan for "DLT
incremental fetch with state cursors."

**Why:** Inspecting the installed package (`dlt.pipeline.run(resource,
destination=...)`) confirmed it is designed to own a destination's schema and
normalization — directly in tension with a schema already owned by Alembic, with RLS
forced on every table. Its `Incremental` cursor primitive stores state separately
from `ingest_state`, which already exists for exactly that job. Using dlt only for its
extract stage while discarding the reason it exists (destination/schema/load
management) would add a real dependency surface for one feature already covered:
dedup rides on `document`'s unique constraint, which cannot drift out of sync with
what was actually persisted the way a hand-maintained cursor could.

## 2026-08-23 — .gitignore's data patterns were unanchored, silently excluding a source module

**Chose:** `/data/`, `/bronze/`, `/cache/`, `/volumes/` — anchored to the repo root.

**Rejected:** The unanchored `bronze/` (no leading slash), inherited from the scaffold
template.

**Why:** An unanchored gitignore pattern matches at any depth. It was written to catch
an accidental `./bronze/` mirror at the repo root, but it also matched
`src/corpus/bronze/` — the actual bronze-store *module* — which meant `store.py` was
never tracked despite `git add -A` reporting nothing wrong, all the way through the
step-2 commits. Found only because a fresh `git status` after writing the ingestion
pipeline showed the file simply absent. Fixed in this repo and in
`~/Projects/claude-config/templates/project/dot-gitignore` so no project scaffolded
from it repeats the mistake.

## 2026-08-21 — `domain` as a column separate from `authority_tier`

**Chose:** A `domain` enum on `source` (`ai_research` / `ai_automation` /
`entrepreneurship` / `personal_development` / `regulatory`), orthogonal to
`authority_tier`.

**Rejected:** Folding domain into `authority_tier`, or leaving it implicit in which
seed list a source came from.

**Why:** Once entrepreneurship and personal-development channels were added to the
corpus (at the user's explicit request — the corpus exists partly to develop their
own businesses, not only to track the AI industry), a channel like Alex Hormozi's and
a channel like Databricks' could carry the same authority_tier while meaning entirely
different things for analytics. "Mentioned 200 times" is a saturation signal for a
tool name and noise for a self-help phrase; blending them makes both counts
meaningless. `domain` lets analytics filter to one world by default and opt into
cross-domain queries (e.g. "which founder-tier ideas later showed up in adoption
content") explicitly rather than by accident. One column now versus a backfill across
every ingested row later.

## 2026-08-21 — yt-dlp for discovery and metadata, Supadata for transcript text only

**Chose:** Channel discovery, video metadata, publish dates, and caption provenance
(`subtitles` vs `automatic_captions`) come from yt-dlp — free, no credits. Supadata is
used only for the transcript text itself.

**Rejected:** Using Supadata's `/youtube/video` metadata endpoint, as originally
planned.

**Why:** yt-dlp returns strictly more than Supadata's metadata call — an exact
`upload_date`/`timestamp` where Supadata's `uploadDate` is optional, full tag and
chapter data, and critically the `subtitles`/`automatic_captions` split, which is the
closest thing to transcript provenance either tool provides (Supadata has none at any
price). It also halves the per-video ingest cost, since the metadata call is no
longer needed. The tradeoff, raised by the user: yt-dlp throttles under bulk load in a
way a paid API does not, which is exactly why it is *not* used for the high-volume
transcript-fetch path — that stays on Supadata.

## 2026-08-21 — Shorts excluded from every seed list, `/videos` tab only

**Chose:** All channel video counts and ingestion targets come from a channel's
`/videos` tab specifically. Shorts (`/shorts`) are never counted or ingested.

**Rejected:** Using a channel's total video count (which several providers, including
Supadata's `/youtube/channel` endpoint, report as videos + Shorts combined).

**Why:** Discovered when Supadata reported RoboNuggets at 1,400 videos and yt-dlp's
`/videos` tab returned 153 — the other 1,247 were Shorts. A 60-second Short carries no
argument, position, or datable claim worth retrieving, and Shorts share varies wildly
by channel (89% for RoboNuggets, 0% for Liam Ottley), so it cannot be estimated and
must be measured per channel. Ingesting by total-video-count would have silently
spent a large fraction of the credit budget on content that degrades every retrieval
and analytics capability in the corpus.

## 2026-08-20 — RLS applied to partitions, not just the partitioned parent

**Chose:** Explicit RLS on every partition of `chunk_embedding`, plus an event trigger
that applies it automatically to any partition created later.

**Rejected:** Relying on the parent table's policies. This was the original design and
it was wrong.

**Why:** Postgres consults a parent's policies only when the query goes *through* the
parent. `SELECT * FROM chunk_embedding_default` bypasses them entirely, and the
application roles already held privileges on the partition via `ALTER DEFAULT
PRIVILEGES`. Verified: `corpus_app` could read every tenant's vectors. It matters more
here than it normally would because the re-embedding design creates a new partition
per model version, so the same hole would reopen on every model change — precisely
when attention is elsewhere. The event trigger removes the need to remember.

## 2026-08-20 — Document-level dense retrieval before chunk-level

**Chose:** The first dense lane embeds one vector per document summary
(`document_summary.embedding`). Chunk-level dense is added only if the eval harness
shows document-level missing things.

**Rejected:** Chunk-level dense over the whole corpus from the start, as originally
specified.

**Why:** Only 2 of the 10 specification queries clearly need vectors, and both are
*document-level arguments* rather than passage-level facts — "where has anyone
described buying tooling before defining the decision" is a property of a whole
discussion, not a 400-token window. Document granularity is ~30-50x fewer vectors,
fits in memory trivially, avoids an HNSW build that would not fit in
`maintenance_work_mem` at scale, and is better matched to the queries. Revisit if the
harness shows document-level recall failing on Q4 or Q6.

## 2026-08-20 — Two transcript providers, with roles that invert on migration

**Chose:** `youtube-transcript-api` primary locally, Supadata fallback; expected to
swap on the Linux server. `transcript_version.provider` recorded per transcript.

**Rejected:** Supadata as sole primary, per the original spec.

**Why:** Supadata provides no transcript provenance — its response has three fields
and none indicates auto-generated vs human-authored.
`youtube-transcript-api` exposes `is_generated` and is the only source of it. But
YouTube blocks cloud-provider IPs, so it will fail on the server this migrates to,
where Supadata is the only thing that works. Both are needed, for different reasons,
at different times. See `docs/SUPADATA.md`.

## 2026-08-20 — Punctuation and truecasing restoration as a required pipeline stage

**Chose:** A restoration stage between raw transcript and entity extraction, stored as
a separate `transcript_version` with `derived_from_id` pointing at its parent.

**Rejected:** Running NER directly on raw captions.

**Why:** Auto-generated YouTube captions are lowercase and unpunctuated, and NER models
lean heavily on casing. This degrades the entity layer, which is the bottleneck for
every analytics capability. Documented in the ASR literature, not assumed. Cost is real
and was not in the original spec. Step 4 measures extraction precision with and without
it — if restoration does not earn its runtime, this gets reversed on evidence.

## 2026-08-20 — Nullable `is_auto_generated`, never coalesced

**Chose:** `is_auto_generated boolean NULL` with a `provenance_confidence` enum, and a
CHECK constraint tying NULL to `'unknown'`.

**Rejected:** Defaulting to `false`, or omitting the column when the provider is silent.

**Why:** "The provider did not tell us" and "human-authored" are different claims, and
conflating them silently corrupts any analysis that filters or down-weights on
transcript quality. Same reasoning drives `published_at_precision` and
`attribution_method`: the metadata layer's job includes recording its own gaps.

## 2026-08-20 — Tenant column and RLS from commit one, with one real tenant

**Chose:** `tenant_id` on all 13 domain tables, policies enforced, verified against two
synthetic tenants. Nothing else in the system branches on tenancy.

**Rejected:** Deferring multi-tenancy until a second tenant exists.

**Why:** Backfilling a tenant column and policies across millions of rows later is the
one genuinely expensive retrofit in this design. Present cost is close to zero. The
bronze store is deliberately excluded — it is a filesystem and the rebuild guarantee,
and an authorization boundary there adds risk without benefit.

## 2026-08-20 — Project layout and data separation

**Chose:** Code in `~/Projects/research-corpus`, data in `~/data/research-corpus`,
located at runtime via `PROJECT_DATA_DIR`.

**Rejected:** A `data/` directory inside the repo.

**Why:** Data inside a repo does not survive a fresh clone, a `git clean`, or a reset,
and it makes the repo grow without bound. Reading the location from the environment
means the move to a Linux server is a config change rather than a rewrite.
