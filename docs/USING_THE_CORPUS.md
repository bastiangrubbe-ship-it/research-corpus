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

As of 2026-08-27 — these numbers grow nightly, so treat them as scale, not truth:

| | |
|---|---:|
| Documents | 3,966 |
| With transcripts | 3,897 |
| Sources (channels) | 170 |
| Distinct entities | 25,166 |
| Entity mentions | 328,152 |
| Date range | 2018-12 → 2026-08 |

By domain: `ai_automation` 1,907 · `ai_research` 1,252 · `entrepreneurship` 323 ·
`general` 278 · `personal_development` 206.

The subject matter it genuinely covers, measured by how many *independent* sources
discuss each entity: Google (149), ChatGPT (141), Claude (140), OpenAI (132),
Anthropic (119), Gemini (113), Claude Code (106), GitHub (105), Microsoft (99).

**So: AI tooling, AI research, and the creator/founder economy around them.** It is
thin on everything else, and empty on most of it.

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

## Five things that will mislead you if you do not know them

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

**5. Check `indexed_documents` against `total_documents`.** A partially-built index does
not look broken, it looks decisive — it returns confident rankings over whatever
fraction happens to be searchable.

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
