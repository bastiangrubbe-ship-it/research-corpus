# The pipeline

How a video becomes something the corpus can answer questions about — the stages, what
each one reads and writes, and the ordering constraints that are not obvious from the
code. Written for someone (or some model) arriving with no context.

`CLAUDE.md` says what this project *is*. `docs/DECISIONS.md` says *why* each choice was
made, chronologically. This file says what actually runs, in what order, and what breaks
if you get that order wrong.

## The stage graph

```
seeds/youtube_channels.yaml
        │  flows/ingest_youtube.py
        ▼
   source ──► document ──► transcript_version(raw) ──► segment
                              │
        ┌─────────────────────┬──────────────────────┐
        │                     │                      │
        ▼                     ▼                      ▼
 backfill_summaries     backfill_chunks       nightly_entities
        │                     │                      │
        ▼                     ▼                      ▼
 document_summary          chunk +               entity +
   (+ embedding)        chunk_embedding       entity_mention
                                            + entity_extraction_run
```

Speaker attribution (`corpus.enrich.speakers`) hangs off `document` independently and
reads `entity` when available, so it is better after entity extraction than before.

## Stages

| Stage | Flow | Cost | Concurrency | Idempotent? |
|---|---|---|---|---|
| Ingest | `flows/ingest_youtube.py` | Supadata credits (ytapi first, free) | `--concurrency` (needs `--metadata-source supadata`) | yes — `document`'s unique constraint |
| Probe missing | `flows/probe_missing_transcripts.py` | free (yt-dlp) | 3 | yes |
| Retry failed | `flows/retry_failed_transcripts.py` | usually free (ytapi) | serial | yes — only touches `fetch_failed` |
| Summaries | `flows/backfill_summaries.py` | free (local) | batched | yes; `--redo` overwrites |
| Chunks | `flows/backfill_chunks.py` | free (local) | batched | yes — skips documents with any chunks |
| Entities | `flows/nightly_entities.py` | subscription quota | `--concurrency` (safe to 25) | yes — `entity_extraction_run` per `extractor_version` |
| Health check | `flows/doctor.py` | free, read-only | — | n/a |

## The ordering constraints that have actually bitten

**1. Reading and indexing use different transcript versions, and that is deliberate.**
`corpus/db/transcript_versions.py` has two resolvers:

* `latest_versions` — newest whatever the provider, so restored if one exists. Used by
  synthesis quotes, the eval judge and entity extraction: these hand text to a model to
  read, and a citation pulled from unpunctuated ASR is unreadable.
* `index_versions` — newest **non-restored**. Used by summaries and chunks: these are
  embedded and BM25-indexed, never read by a person, and restored text measured worse
  as input for both.

This used to be one rule — "newest by created_at" — which is correct until restoration
runs and silently wrong forever after. Two defects came from it before the split
(2026-08-26 and 2026-08-27), both of which rebuilt an index from worse text with
nothing failing. Pinning by provider puts the choice at the query where it cannot be
forgotten. Asserted by `tests/unit/test_transcript_version_choice.py`.

**2. Never run `backfill_chunks` after restoration expecting a no-op.** It excludes per
document (fixed 2026-08-27), but before that fix it excluded per *transcript version*
and built a second chunk set against restored text while leaving the raw one live:
70,106 → 140,849 chunks, and measurably worse retrieval (P 0.413 → 0.358) because
`DISTINCT ON` picked the closer-but-less-relevant restored chunks. Use
`chunking.backfill.rechunk_document` to supersede deliberately; it deletes first.

**3. There is no restoration stage.** It was dropped on 2026-08-27 after measurement:
94% of transcripts already arrive adequately punctuated, its documented purpose was
tested and rejected twice, and it cost half the segments in the database.
`flows/restore_transcripts.py` still exists and works, deliberately unwired — see
docs/DECISIONS.md before reinstating it.

**4. Entities before speakers.** `guess_speaker` validates candidate names against the
`entity` table; a name known to be a VENDOR or PRODUCT is rejected. Run before entity
extraction and that validation has nothing to check against.

## The scheduled job

`scripts/nightly.sh`, invoked by `launchd/com.bastiangrubbe.research-corpus.nightly.plist`
at 03:00. Ingest, then entity extraction, then a heartbeat.

Three things about it are load-bearing and were each a real outage:

* **It re-execs under `direnv exec`.** `PROJECT_DATA_DIR` lives in `.envrc`, not `.env`,
  and direnv only loads `.envrc` for an interactive shell. launchd does not run direnv,
  so the job died in `Settings()` validation on four consecutive nights, silently.
* **It writes a heartbeat** (`corpus.ops.heartbeat`) on success, and on failure via an
  `ERR` trap. `flows/doctor.py` reports staleness and refuses to say "all stages
  complete" while scheduled work is stalled. A corpus can be fully built and entirely
  dead; the stage counts cannot tell those apart.
* **It passes `--since-days`.** Without it, discovery re-enumerates every channel's
  entire back catalogue every night to find a handful of new videos — that is a
  multi-day full backfill, not an incremental run.

Tunables, all env-overridable so changing one is a variable and not an edit:
`NIGHTLY_SINCE_DAYS` (7), `NIGHTLY_ENTITY_CONCURRENCY` (15),
`NIGHTLY_INGEST_CONCURRENCY` (1 — raising it requires `--metadata-source supadata`,
which moves cost from free-but-rate-limited yt-dlp to metered Supadata).

## How to tell whether it worked

```bash
uv run python flows/doctor.py        # exits 1 if any stage is incomplete or a flow is stale
```

Per-stage completeness, what silently degrades while a stage is partial, and stale
scheduled work. **Run this before believing a "nothing found" result.** The recurring
failure in this project is not a stage breaking — it is a stage running over *part* of
the corpus while everything downstream keeps working confidently over whatever fraction
exists. A partially-built stage does not look broken; it looks decisive.

Documents that provably cannot reach a stage (members-only, no captions, removed) are
excluded from the denominators via `document.transcript_unavailable_reason`, and the
exclusion is always printed — a denominator that quietly shrinks is how a check starts
lying.

## Changing retrieval

Any change that could affect retrieval goes through:

```bash
uv run python scripts/paired_retrieval_ab.py --backup-table <before_table>
```

Both conditions scored against **one frozen judgment set**, with the "before" state
produced inside a transaction that is rolled back. Two rules it encodes:

* Do not run the eval harness twice and diff the results. It judges its own pool each
  run, so the relevant-set grows between runs and recall's denominator moves underneath
  the measurement. That produced a clean-looking, entirely artefactual regression.
* Name a control lane and believe it. `chunk_dense` reads no summary, so a summary-side
  change must move it by 0.000. `lexical` is **not** a control — it reads
  `document_summary.text`.

## Adding sources

`seeds/youtube_channels.yaml` is the only way a YouTube channel enters the corpus, and
`seeds/rss_feeds.yaml` the only way a feed does. Never write to the `source` table
directly: a dashboard button that did would create a second, invisible channel of truth
with no diff and no review.

Handles must be **verified against the live API**, never derived from a display name.
`scripts/resolve_new_subscriptions.py` proposes a handle and then checks what the
channel calls itself — `@Theo` is a channel named "Mr Crush" with 293 subscribers, not
Theo of t3.gg.

`phase` is the ingestion ramp, not a label. Adding a channel costs nothing; ingesting
it costs one credit per video. Large channels carry a cap in `note`, and the ingestion
pipeline is required to read it rather than `videos_at_survey`.

## Where the bodies are buried

- `docs/DECISIONS.md` — every deviation and the reasoning, newest first. Long, but the
  rejected options are the valuable part.
- `docs/BUILD_PLAN.md` — the original criteria and the kill criterion. Step 0 is still
  blocked: the ten spec queries no longer exist as text.
- `docs/FEDERATION.md` — why realtime web/forum fan-out is deliberately *not* built here.
- `src/corpus/db/models.py` — the schema is documented in the model docstrings, which
  carry the reasoning (why `is_auto_generated` is nullable, why `Heartbeat` exists).
  There is no `docs/SCHEMA.md`, despite older references to one.
- `src/corpus/db/rls.py` + `migrations/versions/0002_rls_and_embeddings.py` — the RLS
  mechanics. `corpus_migrate` is a superuser and bypasses RLS unconditionally; anything
  asserting tenant isolation must connect as `corpus_app`.
- `launchd/README.md` — installing and inspecting the scheduled job.
- `docs/EVAL.md`, `docs/EVAL_RELEVANCE_GATE.md` — extraction rubric and lane measurements.
