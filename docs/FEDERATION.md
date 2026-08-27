# Federation: this corpus alongside realtime sources

How research-corpus relates to realtime search — web, Reddit, X, HN — given that it
is driven primarily **from inside Claude** via the MCP server rather than through the
dashboard.

Written 2026-08-26. The decision here is *not to build* a fan-out layer; the reasoning
matters more than the conclusion, because the obvious instinct is to build one.

## The question

"Any request to the corpus should also run a similar search on the web and known user
forums (Reddit, X). Which realtime sources should we add?"

## Why the answer is "none, in this repo"

**Claude already has web search and web fetch.** When the corpus is driven from inside
Claude, building a fan-out here means reimplementing a capability the client already
has, one hop further away, and taking the routing decision away from the component
best placed to make it.

The routing policy is also *already written* — it is the `instructions` string on the
MCP server (`corpus/mcp/server.py`):

> Prefer this over general web search when the question is comparative or temporal —
> how many sources discuss something, whether a topic is rising or falling, who
> mentioned it first — not when the question is a single current fact web search
> already answers well.

Claude reads that at connect time. "Search both and compare" is the client's job, and
the client has been told how to do it.

### Rejected: a fan-out layer inside the corpus

Rejected on four grounds, recorded because it is the intuitive design:

1. **Duplicates the client.** Worse general web search, further from the user.
2. **Latency compounds.** `corpus_search` at the MCP default pool is already seconds;
   serialising external calls behind it makes the corpus feel like the slow component
   even when the delay is Reddit's.
3. **Widens the untrusted-input surface.** Transcripts are untrusted but passive. Web
   and forum results are adversarial-capable — a fetched page can carry text aimed at
   whatever model reads it. Every additional fetcher inside this process is another
   place that has to get `--allowedTools ""` and stdin-not-prompt discipline right.
4. **It would dissolve the corpus's own success criterion.** If every answer is a
   blend, you can no longer tell whether the corpus earned its keep. See "Measurement"
   below.

## What already exists upstream

[`mvanhorn/last30days-skill`](https://github.com/mvanhorn/last30days-skill) (MIT) is
this idea, built and widely used: ~40 source adapters (Reddit, X, YouTube, TikTok, HN,
Polymarket, GitHub, arXiv, Bluesky, Techmeme, …), engagement-weighted scoring, and an
LLM judge that synthesises a brief. Its architecture converged independently on this
project's: `fusion.py`, `rerank.py`, `dedupe.py`, `relevance.py`.

Notes from reading it, which inform the decisions above:

- It reaches **X via browser session cookies** against X's internal GraphQL endpoints
  (vendored `@steipete/bird`), not the official API. That is free where the official
  API is ~$200/mo, and it is correspondingly fragile — the code carries retry logic
  for anti-bot interstitials — and it reads the local browser cookie store. A real
  trade, not a free lunch.
- **Reddit has genuinely keyless paths** (`reddit_public`, `reddit_rss`,
  `reddit_keyless`) alongside a paid backend.
- It ships a **`doctor`** that live-probes every source and reports what is broken or
  unconfigured. This project has no equivalent for its own pipeline stages and should
  (see Follow-ups).
- Its own `lib/corpus.py` is a **local directory scanner** — no HTTP, no database. It
  normalises files into `SourceItem` and hands them to the shared fusion pipeline.

## The complementarity is temporal, and it is the whole point

|  | last30days | research-corpus |
|---|---|---|
| Window | last ~30 days | 660+ days |
| Memory | none — refetches every run, results are ephemeral | accumulating, versioned |
| Ranking signal | engagement (upvotes, likes, money) | entity linkage, source spread, recency |
| Answers | "what are people saying now" | "how did this argument develop, and who said it first" |

Neither substitutes for the other. `/last30days` cannot answer "who said this before it
became consensus" — by construction, it only looks at the last 30 days. This corpus
cannot answer "what is the reaction today", and should stop trying.

**So: run both from Claude, and let each do what it is for.** That requires no code in
this repo.

## The integration that would be worth building

Not an adapter — an **ingestion path**, and the direction is the opposite of the
obvious one.

`/last30days` output is ephemeral: it finds high-engagement Reddit threads, HN
discussions and X posts on a topic, synthesises, and discards the evidence. This
corpus's entire value is that it *keeps* things and can compare them over time.
`SourceKind.WEB` already exists in the enum for exactly this shape of input.

So the valuable flow is **last30days → corpus**, not corpus → last30days:

1. `corpus_coverage` grades a topic `thin` or `none`.
2. Claude runs `/last30days` on it (its own decision, per the routing instructions).
3. What it surfaced becomes a *sourcing signal* — and optionally documents ingested as
   `SourceKind.WEB`.
4. Next quarter, the corpus can answer temporal questions about forum discourse that
   no realtime tool can, because it kept the evidence.

That closes the sourcing loop with a tool that already exists, and it aims the
improvement at **what the corpus knows** rather than at ranking — where, on this
corpus, the measured headroom is ±0.05 and the gaps are measured in whole topics.

Constraints that apply if this is built: bronze stays immutable, no LLM inference in
the ingest path, and forum text is untrusted external content on the same terms as
transcripts.

## Measurement, and the one thing not to lose

Federation must not quietly make this corpus a thin wrapper around web search. The
guard is **provenance labelling**: every answer records which source produced it.

With Claude as the orchestrator the comparison happens in Claude's context and is not
captured here. The cheap proxy is to **log coverage verdicts**: every `thin`/`none` on
a real query is a recorded instance of this corpus failing to earn its keep, with the
query text attached. Weaker than a true A/B, but it accumulates automatically and it
is honest — and it is the closest thing available to the build plan's step-0 criterion
while the ten spec queries remain unrecovered (`docs/BUILD_PLAN.md`).

## Follow-ups, in order

1. **Coverage as router** — state in `corpus_coverage`'s tool description that a
   `thin`/`none` grade means "this corpus cannot answer that; go to the web." One line,
   turns a diagnostic into routing.
2. **Lower the MCP `corpus_search` default pool.** 50 is documented as "right for an
   agent that can wait"; in an interactive Claude session the user is the one waiting.
3. **Log queries and coverage verdicts.** The sourcing backlog and the step-0 proxy
   both fall out of it. Note this is the most sensitive data the system would hold —
   the transcripts are public, the queries are what *you* are investigating.
4. **A `doctor` for this pipeline**, borrowing last30days' pattern: probe each stage
   (summaries built? chunks embedded? entities current?) and report completeness. This
   project's recurring failure mode is that a partially-built index does not look
   broken, it looks decisive (`docs/DECISIONS.md`).
5. **Ingest path for `SourceKind.WEB`**, only if (3) shows forums are where the gaps are.
