# Decisions

Newest first. One entry per decision that would be expensive to reverse, or that would
look arbitrary to someone arriving later.

The rejected options are the valuable part. They are what stops the same debate from
being had twice, and what tells a future reader whether the alternative was considered
and dismissed or simply never thought of.

---

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
