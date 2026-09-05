# Using this corpus

A self-contained brief. Paste it into any chat that has the `research-corpus` MCP
server connected, or read it to understand what the corpus can and cannot answer.

Claude Code sessions **inside this repo** already load `CLAUDE.md` automatically, and
an MCP client already receives the server instructions and tool descriptions (~1,500
tokens) at connect. This file is for everything else: a fresh chat elsewhere, a
colleague, or a model that needs the picture in one place.

---

## What it is

A private corpus of **YouTube/podcast transcripts**, ingested from channels the owner
subscribes to, enriched with entity extraction and searchable three ways.

**Its value is comparative and temporal, not factual.** Any single fact is better
retrieved by web search. What search cannot do is show how a view spread across a
thousand practitioners over three years, or who said something before it was consensus.

Ask it *"how did the argument for X change"* or *"which vendors are discussed more now
than six months ago"*. Do not ask it *"what is X"*.

## What is actually in it

As of 2026-08-29, mid-expansion — these numbers grow nightly, so treat them as scale,
not truth:

| | |
|---|---:|
| Documents | 6,471 |
| With transcripts | 6,319 |
| Sources (channels + feeds) | 388 |
| Distinct entities | 36,207 |
| Entity mentions | 460,027 |
| Transcript chunks | 109,015 |
| Date range | 2018-12 → 2026-08 |

By domain: `ai_automation` 2,120 · `performance_marketing` 1,473 · `ai_research` 1,322 ·
`entrepreneurship` 778 · `general` 279 · `personal_development` 260 ·
`marketing_measurement` 239.

By authority: `practitioner` 3,844 · `established_media` 1,641 · `vendor_official` 890 ·
`aggregator` 71 · `unknown` 25. **Practitioner content is the majority.** That is a
strength for "what are people actually doing" and a weakness for anything needing an
audited number — see the warning about creator claims below.

The subject matter it genuinely covers, measured by how many *independent* sources
discuss each entity: Google (224), Claude (212), ChatGPT (207), YouTube (198), OpenAI
(172), Instagram (160), Anthropic (156), Gemini (154), Slack (151), Meta (150).

**So: AI tooling and AI research; performance marketing and marketing measurement; and
the creator/founder economy around all of it.** It is thin on everything else, and
empty on most of it. In particular it holds no regulatory-domain sources at all, despite
regulatory tracking being one of its stated jobs.

## The five tools, and when each is right

| Tool | Use for | Cost |
|---|---|---|
| `corpus_coverage` | **Call this first when unsure.** Can the corpus answer this at all? | free |
| `corpus_search` | Find specific documents/passages | ~14s |
| `corpus_analytics` | Rising, emerging, saturated entities; co-occurrence drift; diffusion timeline | free, SQL only |
| `corpus_synthesize` | Read *every* document matching a filter, answer with citations | **one LLM call per document, minutes** |
| `corpus_provenance` | Where one document came from, how reliable | free |

`corpus_analytics` is the underrated one — it answers the comparative and temporal
questions with no embeddings and no LLM, which is what the corpus exists for.

## Six things that will mislead you if you do not know them

**1. A coverage grade of `none` or `thin` means go elsewhere.** It is a router, not a
diagnosis. Reporting what little the corpus holds as if it were the picture is the
main failure mode.

**2. Coverage has a shape in time.** A `good` grade with `pattern: burst` or `faded`
means the corpus covers *a period* well and can say nothing about now. Read
`temporal.pattern`, `temporal.peak_period` and `temporal.quiet_months` before treating
a grade as an answer about the present. A trend drawn across quiet months is
interpolation.

**3. `corpus_search` always returns its best matches**, even when the best is nothing
much. Ten weak results look identical to ten strong ones. That is what
`corpus_coverage` is for.

**4. `corpus_synthesize` spends real money** — one LLM call per matched document, and a
broad filter matches hundreds. Always `dry_run: true` first to see the count. A capped
run reports `capped` and `dropped_by_cap`; a partial read is not a complete answer.

**5. Check `indexed_documents` against `total_documents` on every answer.** A partially
built index does not look broken, it looks decisive — it returns confident rankings over
whatever fraction happens to be searchable. **Right now that gap is real:** the corpus is
mid-expansion, and as of 2026-08-29 roughly 91% of documents have a summary embedding
(the only thing the dense lane searches), 92% are chunked, 93% have entity extraction,
and 67% have speaker attribution. Analytics counts are computed over the extracted
fraction and will read as the whole picture unless you check. `flows/doctor.py` reports
all of it.

**6. Revenue and outcome figures in this corpus are claims, not audited results.** The
practitioner majority is largely creators demonstrating success in order to sell
something — a course, a community, a white-label tool. Processor screenshots show gross
volume, never profit and never churn. Price points are reliable evidence of what a
market has been trained to expect; the outcomes attached to them are marketing. Treat
"$X/month" as a stated offer, not a measured result, unless a second independent source
says otherwise.

## Worked examples

Good, in decreasing order of what only this corpus can do:

- *"Which vendors are discussed more now than six months ago?"* → `corpus_analytics`,
  `rising_entities`
- *"Which source mentioned Claude Code first, and in what order did others pick it up?"*
  → `corpus_analytics`, `diffusion_timeline`
- *"How has the argument for local models developed over the last year?"* →
  `corpus_coverage` first, then `corpus_synthesize` with a date filter
- *"Find where anyone discusses RAG evaluation"* → `corpus_search`

Bad, and why:

- *"What is the latest Claude model?"* → a current fact; web search is better and this
  corpus may be weeks stale
- *"What is everyone saying about the announcement today?"* → recent reaction; the
  corpus ingests nightly at best and holds no forums
- *"Summarise the state of quantum computing"* → outside its subject matter; coverage
  will grade `none`

## If the corpus cannot answer

Say so, and say which source you used instead. The corpus is one source among several.
The owner runs it alongside general web search and `/last30days` (Reddit, X, HN) for
the last-30-days question this corpus structurally cannot answer — see
`docs/FEDERATION.md`.

## For someone changing the code

`docs/PIPELINE.md` — the stage graph, the ordering constraints that have caused real
defects, and how to verify a change. `docs/DECISIONS.md` — every deviation and why.
Start with `flows/doctor.py`, which reports whether the pipeline is actually complete.
