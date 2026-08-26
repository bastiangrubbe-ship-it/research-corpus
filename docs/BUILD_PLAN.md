<!-- Recovered 2026-08-26 from the session transcript. This document existed only
     in a scratch plan file and was overwritten during later planning; it is kept in
     the repo now because it defines the project's success criteria, not just its
     build order. Verbatim as approved — deviations are recorded in DECISIONS.md
     rather than edited in here, so the original reasoning stays legible. -->

> **Status note (2026-08-26).** Steps 1-5 and 7-10 are complete; see `CLAUDE.md` for
> current state and `DECISIONS.md` for every deviation and why. **Step 0 has never
> been done**, because the ten query texts below are referred to but never written
> out — only fragments survive (Q5 "governance as competitive advantage, not
> compliance cost"; Q8 "which vendors are discussed more now than six months ago").
>
> That gap matters more than it looks. The ten queries are described here as *the
> specification*, and the go/no-go gate in Step 0 is stated in terms of them. Until
> they exist as text, the eval harness can measure whether retrieval is *improving*
> (`docs/EVAL_RELEVANCE_GATE.md`) but cannot answer whether this corpus is *worth
> having* — which is the question Step 0's kill criterion was written to force.
> `src/corpus/eval/queries.yaml` currently carries acknowledged substitutes.

# Research corpus — build plan

## Context

A private multi-source research corpus, queried by Claude Code as an MCP tool, serving
content sourcing, vendor/market intelligence, client prep, and regulatory tracking.

The premise that shapes every decision: **the corpus's value is comparative and
temporal, not factual.** Web search already wins on any single fact. What it cannot do
is show how a view distributed across a thousand practitioners over three years, or who
said something before it was consensus. Where this plan departs from the spec, it is
almost always to move effort away from passage retrieval and toward the metadata and
analytics layers that make comparison possible.

Ten queries are the specification. Their distribution — 3 lexical, 3 semantic,
2 thematic, 2 temporal — is the budget. Exactly one category needs vectors.

---

## Environment — verified, not assumed

| | |
|---|---|
| Hardware | Apple M5 Pro, 18 cores (6P/12E), **48 GB** unified memory |
| OS | macOS 26.6.2, arm64 (Metal; no CUDA) |
| Disk | 835 GB free of 926 GB |
| Python | **3.13.15** via uv 0.12.5. System 3.9.6 is macOS's, unused |
| Docker | Engine 29.7.2, Compose v5.4.0, overlayfs. **0 images, 0 volumes, 0 containers** |
| pgvector | `pgvector/pgvector:pg17` publishes native `linux/arm64` — no emulation |
| Rosetta | Installed and mounted into the Docker VM, so amd64 images work if needed |

**Two environment problems to fix before any index work:**

1. **Docker's VM has 7.7 GB of your 48 GB.** Default allocation. A Postgres holding an
   HNSW index cannot work in that. Raise to 24–32 GB in Docker Desktop → Settings →
   Resources. This is the single highest-impact thing on the list.
2. `~/data` is currently outside Docker's file-sharing scope. Compose bind mounts will
   need it added, or Postgres will fail to start with a permissions error.

---

## Supadata — confirmed from the OpenAPI spec

Not inferred. Taken from `docs.supadata.ai/api-reference/v1-openapi.json` and the
endpoint docs.

```json
// GET https://api.supadata.ai/v1/youtube/transcript   (header: x-api-key)
{ "content": [ {"text": str, "offset": ms, "duration": ms, "lang": str?} ],
  "lang": str, "availableLangs": [str] }
```

`text=true` collapses `content` to a plain string. Metadata is a **separate** call
returning `id, title, description, duration, channel{id,name}, tags,
transcriptLanguages` plus optional `uploadDate, viewCount, likeCount`.

Batch: `POST /youtube/transcript/batch` (`videoIds` | `playlistId` | `channelId`,
`limit` ≤ 5000) → `jobId`; poll `GET /youtube/batch/{jobId}` for
`queued|active|completed|failed`. Failures return `errorCode` per video rather than
failing the job. **Cost: 1 credit for the batch + 1 per video.**

Budget: **30,000 credits/month.** At ~1 credit/video that is ~30k videos/month
ceiling — generous. The binding constraint will be local enrichment throughput, not
the API.

### What it does not give you

| Metadata pillar | Supadata | youtube-transcript-api |
|---|---|---|
| Trustworthy dates | `uploadDate`, **optional** | via separate metadata call |
| Source authority | derivable from `channel` | derivable |
| **Transcript provenance** | ❌ **absent** | ✅ `is_generated` |
| **Speaker attribution** | ❌ absent | ❌ absent |
| Entity extraction | ours to build | ours to build |

Supadata silently substitutes Whisper when captions are missing and returns the
identical shape. The only signal is HTTP 202 vs 200 and an `x-billable-requests`
header — I am **inferring** that 202 reliably means Whisper; the docs imply but do not
state it.

**Consequence: run both providers, and record which one produced each transcript.**
`youtube-transcript-api` is the only source of `is_generated`, so it should be tried
first from your laptop. But YouTube blocks cloud-provider IPs, so on the Linux server
you migrate to, it will fail and Supadata becomes the only viable path. The provider
mix is therefore expected to invert on migration — which is exactly why
`transcript_version.provider` must be a first-class column from commit one.

---

## Where I disagree with the spec

You asked for pushback. Six items, roughly in order of how much they change the build.

### 1. The third "retrieval lane" is a category error

RRF fuses *ranked lists* to produce a top-k. Map-reduce synthesis over pre-computed
summaries does not produce a ranked list — it produces an answer over a complete set.
There is no coherent way to fuse "a synthesis of 4,000 documents" with "60 passages" at
k=60.

**Recommendation:** two fused lanes (BM25 + dense) behind RRF and the reranker, and a
**separate, non-fused synthesis path**. This is not a downgrade — it matches your own
"three distinct capabilities" framing better than the "three lanes" framing does. The
synthesis path takes a SQL/lexical filter, not a query vector, and returns prose plus
citations, not a ranking.

### 2. "No LLM inference in the ingest path" contradicts "pre-computed summaries"

You correctly rejected a contextual-embedding pass as months of local compute. A
corpus-wide abstractive summarisation pass is the *same computation* with the same
price tag. The spec asks for both.

**Recommendation:** extractive summaries at ingest (TextRank/LexRank — no model,
milliseconds per document), and abstractive synthesis **on demand** over filtered sets.
The filter is what makes this affordable: you never summarise 14M chunks, you summarise
the 400 documents that survived a `WHERE`.

### 3. The dense lane should be built last, and at document granularity first

Of your three semantic queries, Q4 and Q6 genuinely need vectors. Q5 ("governance as
competitive advantage, not compliance cost") has strong lexical anchors and hybrid BM25
will likely handle it. So dense is load-bearing for **2 of 10 queries**.

More importantly, Q4/Q5/Q6 are *document-level arguments*, not passage-level facts.
"Where has anyone described buying tooling before defining the decision" is a property
of a whole discussion, not a 400-token window. Chunk-level dense retrieval is arguably
the *wrong granularity* for the only queries that need dense retrieval.

**Recommendation:** first dense implementation embeds one vector per document (or per
extractive summary), not per chunk. ~30–50× fewer vectors, fits in RAM trivially, no
HNSW build problem, and better matched to the queries. Add chunk-level dense only if
the eval harness shows document-level dense missing things. This directly answers your
"does the dense lane earn its cost" question: at document level, obviously yes; at
chunk level over 14M rows, unproven and expensive to prove.

### 4. Q8 is not a retrieval problem at all

"Which vendors are discussed more now than six months ago" is
`SELECT entity_id, date_trunc('month', published_at), count(*) ... GROUP BY 1,2`. No
embeddings, no LLM, no ranking. It is a bar chart.

This is the strongest evidence your instinct to build analytics (step 5) before
retrieval (step 8) is right, and I'd go further: **Q7 and Q8 should be answerable at
the end of step 5**, before a single vector exists. That gives you a working, useful
system months before the retrieval stack lands.

### 5. Ten queries is too small an eval set to measure recall

Recall@10 over 10 queries moves in increments of large fractions of a percent per
query. You will not be able to distinguish a real improvement from noise.

**Recommendation:** expand to 30–40 by writing 3–4 genuine paraphrases per spec query
— different vocabulary, same information need. Keep the original 10 as the headline
scorecard. This roughly triples labeling cost, which I cost out honestly below.

### 6. Model licensing will bite you

You specified open source only, zero ongoing cost. Two of the obvious picks are not:
`jina-embeddings-v3` (the canonical late-chunking model) is **CC-BY-NC** — non-commercial
only, and vendor intel plus client prep is commercial use. Some GLiNER checkpoints
carry non-commercial licenses too, though `gliner-base` is Apache-2.0.

**Recommendation:** `nomic-embed-text-v1.5` (Apache-2.0, 768-dim, 8192 context,
Matryoshka truncation) for late chunking; `bge-reranker-v2-m3` (Apache-2.0) for
reranking; `gliner-base` or a spaCy pipeline for NER. Licence is checked and recorded
in `docs/DECISIONS.md` before any model is downloaded.

### What I agree with, emphatically

- Schema and RLS first. Correct, and the only genuinely irreversible decision.
- Analytics before retrieval. Cheap, underserved, and it de-risks everything after.
- Metadata extraction over retrieval sophistication. I think this is the single best
  judgment in the spec, and the eval harness is designed below to test it rather than
  assume it.
- Bronze store as the rebuildability guarantee. Yes — and it should outlive the database.

---

## The metadata problem you have not costed: caption formatting

Auto-generated YouTube captions arrive **lowercase and unpunctuated**. NER models are
trained on well-formed text and lean heavily on casing as a signal. Running GLiNER or
spaCy directly on auto-captions will produce materially worse entity extraction — which
is the layer you correctly identified as the bottleneck for everything else.

This is documented in the ASR literature, not a guess: capitalisation and punctuation
restoration is a standard prerequisite step for downstream NER on transcripts.

**Consequence:** the pipeline needs a restoration stage (`oliverguhr/fullstop-punctuation-multilang`
or `deepmultilingualpunctuation`, both small and fast) between raw transcript and entity
extraction. It is not optional, it is not free, and the spec does not account for it.
Its output is stored as a separate `transcript_version` so the raw text is never
mutated.

---

## Module structure

```
~/Projects/<name>/                 code   (git)          ~/data/<name>/    data (never git)
  CLAUDE.md  README.md                                     bronze/         raw API responses, write-once
  pyproject.toml  uv.lock  .envrc  .env.example            volumes/        postgres, prefect
  docker-compose.yml                                       cache/          models, HF hub, derived
  docker/postgres/Dockerfile       pgvector + pg_cron, multi-stage
  alembic.ini  migrations/versions/
  src/corpus/
    config.py                      pydantic-settings; env only, no paths in code
    db/  engine.py session.py models.py rls.py
    sources/
      base.py                      SourceAdapter protocol — the step-2 contract
      youtube/  supadata.py  ytapi.py  metadata.py  adapter.py
      rss/      adapter.py         step 10, the real test of base.py
    bronze/store.py                content-addressed write-once, SHA-256
    ingest/  pipelines.py  keys.py  credits.py
    enrich/  restore.py  entities.py  speakers.py  dates.py  summarize.py
    analytics/  velocity.py  emergence.py  saturation.py  drift.py  diffusion.py
    chunking/late.py
    embedding/  encode.py  registry.py
    retrieval/  lexical.py  dense.py  fusion.py  rerank.py
    synthesis/  filter.py  mapreduce.py        <- NOT an RRF lane
    eval/  queries.yaml  pool.py  judge.py  run.py  report.py  baseline.py
    mcp/  server.py  tools.py
    ops/  heartbeat.py  backup.py  purge.py  reembed.py
  flows/  ingest_youtube.py  nightly_analytics.py  reembed.py
  tests/  unit/  integration/  fixtures/
  docs/  DECISIONS.md  SCHEMA.md  SUPADATA.md  EVAL.md  OPERATIONS.md
```

`docs/SUPADATA.md` holds the confirmed schema above so it is never re-derived.

---

## Schema and RLS

Gates everything. Detail here is deliberate.

### Tenancy

Decision: **one real tenant; RLS structure from commit one.** Every table carries
`tenant_id uuid not null`. Nothing else branches on it. Your retrofit argument is
correct — backfilling a tenant column and policies across millions of rows later is
genuinely painful — and the present cost is close to zero.

The bronze store is **not** under RLS. It is a filesystem, it is the rebuild
guarantee, and putting an authorization boundary on it adds risk without benefit.

### Core tables

```
tenant(id, slug, name, created_at)

source(id, tenant_id, kind, external_id, title, url,
       authority_tier,            -- enum, curated by hand; small N
       status,                    -- active | deprecated | purged
       deprecated_at, created_at)

document(id, tenant_id, source_id, external_id, url, title, description,
         duration_s, published_at, published_at_precision,   -- exact|date|month|inferred|unknown
         published_at_source,                                -- api|parsed|inferred
         ingested_at, content_hash, raw_ref, status)

transcript_version(id, tenant_id, document_id,
         provider,                 -- supadata | ytapi | whisper_local | restored
         is_auto_generated,        -- boolean NULL; NULL means "unknown", not "false"
         provenance_confidence,    -- known | inferred | unknown
         lang, available_langs, derived_from_id, created_at)

segment(id, tenant_id, transcript_version_id, idx, text, offset_ms, duration_ms)

speaker(id, tenant_id, document_id, label, canonical_person_id,
        attribution_method,        -- diarized | channel_default | title_parsed | description_parsed | unknown
        confidence)

utterance(id, tenant_id, document_id, speaker_id, start_ms, end_ms, text)
                                   -- populated only for the diarized tier

chunk(id, tenant_id, document_id, transcript_version_id, idx,
      text, start_ms, end_ms, token_count, speaker_id)

chunk_embedding(chunk_id, tenant_id, model_version, embedding halfvec(768))
                                   -- PARTITION BY LIST (model_version)

entity(id, tenant_id, kind, canonical_name, aliases[], external_ids jsonb)
                                   -- kind: vendor | person | technique | regulation | product
entity_mention(id, tenant_id, entity_id, document_id, chunk_id,
               surface, start_char, end_char, confidence, extractor_version)

document_summary(document_id, tenant_id, method, text, embedding halfvec(768))
                                   -- extractive at ingest; the first dense lane

ingest_state(source_id, tenant_id, cursor, updated_at)     -- DLT
heartbeat(flow_name, tenant_id, last_success_at, last_status, detail)
purge_log(id, tenant_id, source_id, reason, rows_deleted, performed_at)
eval_query / eval_judgment / eval_run / eval_result
```

**Design notes that matter:**

- `is_auto_generated boolean NULL` — this is your "store the fact of its absence".
  Supadata rows get `NULL` + `provenance_confidence='unknown'`. ytapi rows get a real
  boolean + `'known'`. Never coalesce `NULL` to `false`.
- `published_at_precision` — separating "we know the day" from "we inferred the month"
  is what stops trend analysis quietly lying. Your spec asked to distinguish
  publication from ingest date; this goes one step further and records *how well* you
  know it.
- `transcript_version.derived_from_id` — punctuation restoration produces a new row
  pointing at its parent. Raw text is never mutated; both are queryable.
- `chunk_embedding` **partitioned by `model_version`** — this is the re-embedding plan.
  You build a new partition alongside the live one and swap, rather than `UPDATE`ing
  14M rows in place. Multi-day becomes multi-day-but-online.
- `halfvec` not `vector` — half the storage (2 bytes/dim), negligible recall loss, and
  HNSW indexes `halfvec` to 4,000 dims vs 2,000 for `vector`. pgvector 0.8.6.

### RLS

```sql
ALTER TABLE document ENABLE ROW LEVEL SECURITY;
ALTER TABLE document FORCE  ROW LEVEL SECURITY;   -- owner is subject too; easy to forget

CREATE POLICY tenant_isolation ON document
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
```

- `current_setting(..., true)` + `NULLIF` so an unset parameter yields *no rows* rather
  than an error. Fail closed, quietly.
- `FORCE` matters: without it, the table owner bypasses the policy and your test
  passes for the wrong reason.
- Three roles: `corpus_migrate` (owner, DDL), `corpus_app` (RLS-bound), `corpus_ingest`
  (RLS-bound — **no `BYPASSRLS`**; ingestion sets `app.tenant_id` like everything else).
- Session parameter set via `SET LOCAL` inside a transaction, in
  `db/session.py`, never by callers. A `SET` without `LOCAL` leaks across pooled
  connections — that is the classic RLS bug and it is a security bug, not a correctness one.
- **Never** treat an MCP tool argument or an LLM system prompt as the tenant boundary.
  The MCP server resolves tenant from its own config, not from anything the model says.

### Vector search under RLS

Pre-filter, never post-filter — agreed, and your recall-starvation reasoning is right.
Mechanism: `hnsw.iterative_scan = 'relaxed_order'` with `hnsw.max_scan_tuples` tuned,
so a selective tenant/date predicate does not return three rows from a top-100 walk.
Added in pgvector 0.8.0; we are on 0.8.6.

At single-tenant scale this is nearly free. **I am flagging that I have not measured
it** — the interaction between HNSW graph walks and a selective `WHERE` is workload-
specific, and the eval harness is where it gets measured rather than assumed.

---

## Build steps

Each: what gets written, what it needs, how you know it works before moving on.

### Step 0 — Baseline capture (new; do it first)

**Write:** `eval/baseline.py`, `eval/queries.yaml` (10 spec queries + paraphrases),
`docs/EVAL.md`.
**Depends on:** nothing. This is why it goes first.
**Do:** run all 10 queries through (a) Claude + web search, (b) Claude with no tools.
Store answers verbatim with timestamps under `~/data/<name>/eval/baseline/`.
**Verify:** 10 frozen baseline answers on disk, scored on the rubric below.

Kill criterion, stated now so it cannot be rationalised later: **web search will win on
Q1–Q3 and that is expected and fine.** The corpus must win decisively on Q7–Q10 — the
thematic and temporal ones search structurally cannot do. If after step 8 it does not
beat baseline on ≥6 of 10 including ≥2 of {7,8,9,10}, stop and reconsider.

### Step 1 — Schema, migrations, RLS

**Write:** `docker-compose.yml`, `docker/postgres/Dockerfile`, `alembic.ini`, first
migration, `db/models.py`, `db/session.py`, `db/rls.py`.
**Depends on:** Docker memory raised; `~/data` shared with Docker.
**Verify:** two synthetic tenants, as you specified. Concretely —
`tests/integration/test_rls.py` asserts: tenant A cannot select/update/delete tenant B's
rows; unset `app.tenant_id` returns zero rows rather than erroring; `FORCE` holds for
the owner role; a vector query with a tenant predicate returns only A's rows; `alembic
downgrade base && alembic upgrade head` is clean.

### Step 2 — Source adapter interface + YouTube adapter

**Write:** `sources/base.py` (Protocol: `discover() → ids`, `fetch(id) → RawResponse`,
`normalize(raw) → Document + Segments`), `sources/youtube/{supadata,ytapi,metadata,adapter}.py`.
**Depends on:** step 1.
**Verify:** golden-file tests against **recorded real responses** (VCR cassettes) for:
captions present, captions absent → Whisper 202, video unavailable, no transcript in
requested language. Provider failover asserted: ytapi tried first, Supadata on failure,
`provider` and `is_auto_generated` recorded correctly in each case.

### Step 3 — Ingestion

**Write:** `ingest/pipelines.py` (DLT, incremental, merge disposition), `ingest/keys.py`
(SHA-256 over `provider|external_id|lang|content`), `bronze/store.py`,
`ingest/credits.py`, `flows/ingest_youtube.py`.
**Depends on:** step 2.
**Verify:** ingest one channel twice — second run fetches zero new videos and spends
zero credits (cursor works). Bronze files are byte-identical across runs and
write-once (a second write to an existing key raises). Kill the flow mid-run; rerun;
no duplicates, no gaps. Credit preflight refuses a batch that would exceed the
configured monthly budget.

### Step 4 — Metadata, restoration, entities, speakers

**Write:** `enrich/restore.py` (punctuation + truecasing), `enrich/entities.py`
(GLiNER), `enrich/speakers.py` (two-tier), `enrich/dates.py`, `enrich/summarize.py`
(extractive).
**Depends on:** step 3.
**Verify:** hand-label entities in 20 documents; measure precision/recall of the
extractor **with and without** the restoration stage. This is the experiment that
justifies your "extraction quality over retrieval sophistication" bet — and it is where
you find out whether restoration earns its runtime. Speaker tier-1 attribution asserted
against 20 known-guest episodes.

Two-tier speakers, as chosen: bulk gets `channel_default` / `title_parsed` /
`description_parsed` with a confidence and a single-vs-multi-speaker flag; a curated
subset gets `diarized` via yt-dlp + WhisperX + pyannote in a **separate opt-in flow**,
never in the main ingest path.

### Step 5 — Analytics

**Write:** `analytics/{velocity,emergence,saturation,drift,diffusion}.py`, materialised
views, `flows/nightly_analytics.py`.
**Depends on:** step 4.
**Verify:** **Q7 and Q8 answerable here, with no embeddings and no LLM.** That is the
milestone. Compare Q8's output against hand-counted mentions in a 50-document sample.

### Step 6 — Chunking and embedding

**Write:** `chunking/late.py`, `embedding/{encode,registry}.py`.
**Depends on:** step 5.
**Verify:** chunk boundaries respect utterance boundaries where attribution exists;
measured throughput on MPS recorded in `docs/DECISIONS.md` (this is the number that
makes re-embedding plannable); `model_version` partition round-trips.

### Step 7 — Eval harness

**Write:** `eval/{pool,judge,run,report}.py`.
**Depends on:** step 6 (needs candidates to pool).
**Verify:** one command runs the full set against a corpus snapshot and writes
comparable, timestamped results. Re-running an unchanged corpus reproduces scores exactly.

### Step 8 — Retrieval

**Write:** `retrieval/{lexical,dense,fusion,rerank}.py`, `synthesis/{filter,mapreduce}.py`.
**Depends on:** step 7 — each layer is measured in isolation before it is fused, and
kept only if it moves the number.
**Order:** BM25 alone → document-level dense → RRF(k=60) → reranker → chunk-level dense
*only if* document-level proves insufficient.
**Verify:** pooled recall@10/@50 per lane per query category. A layer that does not
improve its category gets deleted, not kept for symmetry.

### Step 9 — MCP surface

**Write:** `mcp/{server,tools}.py`. Tools mirror the three capabilities, not the lanes:
`corpus_search`, `corpus_analytics`, `corpus_synthesize`, plus `corpus_provenance`.
**Verify:** Claude Code calls each; tenant resolved from server config; a malicious
tool argument cannot escape the tenant.

### Step 10 — RSS adapter

The real test of step 2. **Verify:** no changes to `sources/base.py` were required. If
the interface has to change, that is the finding, and it is cheaper now than at source five.

### Step 11 — Linux dry run

**Verify:** `docker compose up` on a clean Linux host from the same images; volumes
mount from `$PROJECT_DATA_DIR`; no Darwin-linked binaries; ytapi fails and Supadata
takes over as predicted; a restore from backup produces a working corpus.

---

## Eval ground truth — the honest cost

Pooled judgments over the union of top-20 per lane, as you proposed. The arithmetic:

| | 10 queries | 35 queries (recommended) |
|---|---|---|
| Raw candidates (3 sources × 20) | 600 | 2,100 |
| Unique after dedup (~70%) | ~420 | ~1,470 |
| **Manual, 30–60s each** | **3.5–7 hrs** | **12–24 hrs** |
| **LLM-assisted, human confirms** | **1.5–3 hrs** | **5–10 hrs** |

LLM-assisted means Claude proposes a label plus a one-line reason and you confirm or
override. It roughly halves the time. **It also biases the ground truth toward what an
LLM finds plausible** — which is precisely what you are trying to evaluate. Mitigation:
audit a 10% random sample blind, and if your override rate exceeds ~15%, the assisted
labels are not trustworthy and you label by hand.

Re-pooling: each new lane contributes ~30–40% previously unseen candidates. Budget
**1–2 hours per significant retrieval change**, indefinitely. This is a standing cost,
not a one-off.

**The limitation you must not paper over:** pooled recall is recall *relative to the
pool*. A document no lane ever surfaces cannot enter the pool and never counts against
you. Report the metric as "pooled recall@k" everywhere, never as recall. It is a good
tool for comparing lanes against each other and a bad one for claiming absolute coverage.

---

## Operations

**Backup.** Bronze is the rebuild guarantee and gets the aggressive treatment: `restic`
to an external disk, append-only, daily, verified monthly by restoring a random file.
Postgres gets pgBackRest in Compose — WAL archiving plus weekly base backups; you are
right that `pg_dump` alone is inadequate here. Note the tension with zero-cost: offsite
means either a disk you already own or storage you already pay for.

**Observability.** Dead-man's switch, not log-watching. Every flow writes
`heartbeat(flow_name, last_success_at)` on success. A separate launchd job queries for
stale heartbeats and pushes to `ntfy.sh` (free, self-hostable). **A job that fails
silently and a job that never ran both look identical to a stale heartbeat** — which is
the property you want, and log-scraping does not have it.

**Curation.** `source.status` → `deprecated` hides from retrieval while keeping rows;
`purge` cascades by `source_id` and writes `purge_log`. Operational reality: pgvector
deletes are tombstones until `VACUUM`, so a large purge needs an explicit vacuum step
or the index keeps paying for rows that are gone.

**Re-embedding.** Build the new `chunk_embedding` partition alongside the live one,
verify on the eval harness, then swap. Never `UPDATE` in place.

---

## Inference register

Flagged as you asked. Everything here is a guess until measured.

| Claim | Status |
|---|---|
| Supadata transcript/metadata/batch schemas, credits, auth | **Confirmed** — OpenAPI spec |
| `youtube-transcript-api` exposes `is_generated`; cloud IPs blocked | **Confirmed** — project docs |
| pgvector 0.8.6; halfvec 4,000-dim HNSW; iterative scans in 0.8.0 | **Confirmed** — project docs |
| NER degrades on lowercase/unpunctuated ASR text | **Confirmed** — ASR literature |
| HTTP 202 reliably signals the Whisper path | **Inferred** — docs imply, don't state |
| Supadata requests/sec rate limit | **Unknown** — plan-dependent, undocumented |
| ~33 chunks per 1-hour video | **Inferred** — assumes 400-token chunks |
| Embedding throughput on M5 Pro MPS | **Unmeasured** — step 6 records the real number |
| Diarization ~5–15 min per audio-hour | **Inferred** — not benchmarked on M5 |
| 14M chunks ≈ 200 GB | **Your figure**; my arithmetic gives 100–150 GB with halfvec |
| HNSW build at 14M won't fit in memory | **Inferred** — 43 GB float32 vs 48 GB total |
| GLiNER accuracy on this domain | **Unknown** — step 4 measures it |
| RRF k=60 is right here | **Unverified default** — the harness should test it |

---

## Open questions

1. **Project name.** Everything above says `<name>`. Needed before step 1.
2. **Diarized subset criteria.** Which few hundred episodes earn the expensive path —
   by channel, by entity density, by hand?
3. **Backup destination.** External disk, or existing cloud storage?
4. **Whether Q9 survives.** Tier-1 attribution may prove too weak for position
   tracking. Step 4's measurement decides it, and the answer may be to expand the
   diarized subset rather than accept the degradation.
