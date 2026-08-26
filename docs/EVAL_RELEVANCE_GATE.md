## ✅ Fourth update (2026-08-25): re-measured properly — full index, full transcripts, four lanes

Everything below is superseded by this run
(`run_20260825T223834.json`, 12 queries, top_k=10), the first measurement in this
file taken under conditions that make its numbers mean anything:

* the whole corpus is indexed (3,267 summaries + **70,107 chunks**, not 130 summaries);
* the judge reads **entire transcripts**, no character cap, escalating to a
  1M-context model for documents that need it;
* the **chunk lane is measured separately**, so its contribution is visible rather
  than hidden inside a fused score.

| lane | mean precision | mean recall | queries competed |
|---|---:|---:|---:|
| lexical | 0.61 | 0.24 | 10 / 12 |
| dense (summary) | 0.42 | 0.48 | 12 / 12 |
| **chunk_dense** | **0.50** | **0.54** | 12 / 12 |
| **reranked** | **0.62** | **0.76** | 12 / 12 |

**Four things this actually establishes:**

1. **The chunk lane earns its place.** It beats summary-dense on *both* precision
   (0.50 vs 0.42) and recall (0.54 vs 0.48). The build plan made chunk-level dense
   conditional on evidence that document-level was insufficient; this is that
   evidence, measured rather than argued.
2. **Reranking is the strongest single lane** (P 0.62, R 0.76) and the full pipeline
   is the right default — consistent with the earlier reranker findings, now on a
   real index.
3. **Lexical is precise but narrow** — 0.61 precision, 0.24 recall, and it returns
   *nothing at all* for 2 of 12 queries. `plainto_tsquery` ANDs its terms, so a
   multi-concept query matches only documents containing every term. Worth keeping
   for exact-name queries; never sufficient alone. This is also half of why the
   robots null result looked so convincing.
4. **`q_robots_hardware` is settled.** 8 relevant documents in its pool; reranked
   precision **0.70**, recall **0.88**. The earlier 0.00-across-all-lanes was an
   artifact of an empty index, full stop.

Across all 12 queries the pool contains **114 relevant documents**. The previous run
found zero for the robots family.

## ⚠ Third superseding update (2026-08-25): every measurement in this file ran against 3.9% of the corpus

**Read this before trusting any number below.**

The dense lane searches `document_summary.embedding`; the reranker reads
`document_summary.text`. Documents without a summary row are invisible to both.
Only **130 of 3,344 documents (3.9%)** had summaries when every experiment in this
file was run — `backfill_document_summaries` had only ever been invoked with small
development limits. See `docs/DECISIONS.md` (2026-08-25, "dense retrieval only ever
searched 3.9% of the corpus").

What this does and does not invalidate:

- **The `q_robots_hardware` null result is invalid — now positively disproven, not
  merely doubted.** It is described below as a real corpus gap, confirmed across four
  independent phrasings. With the full index and chunk-level retrieval built
  (docs/DECISIONS.md, same date), the same query returns **grade `good`: 20 documents
  from 14 distinct sources spanning 660 days**, best score 0.4056 against the earlier
  0.0006. Search now surfaces *I spent 3 days at MIT... the robot hype is worse than
  you think*, *Multi-robot collaboration with Gemini Robotics 2*, *Intelligent Robots
  in 2026*, and *Chelsea Finn: This is the State of the Art in Robotics* — none of
  which were reachable before. The four paraphrases varied wording, which cannot
  detect a nearly-empty index: a lesson about what "confirmed N ways" is worth when
  every confirmation shares a substrate.
- **The reranker-vs-cosine comparisons are probably still directionally sound**, since
  both lanes were handicapped identically and were compared against each other rather
  than against absolute recall. But every precision@10 figure is precision over a
  3.9% index, not over this corpus.
- **The bfloat16 collision bug and its fix are unaffected** — that was a numerical
  defect reproduced directly on given text, independent of index size.

The whole-corpus backfill (`flows/backfill_summaries.py`) is the fix. These
measurements should be re-run against the full index before being cited again.

### Second caveat, on the phrase "transcript-grounded"

This file repeatedly describes its ground truth as based on reading "the actual
transcript text" rather than titles or summaries. That is true relative to what it
was correcting, but it overstates what was actually read:

- the automated judge (`corpus.eval.judge`) is passed `text[:6000]`;
- the by-hand ground-truth passes read roughly the first 3,000–3,500 characters.

Transcripts in this corpus routinely run 30,000–130,000 characters, so both figures
represent the **opening 2–5% of a document** — its introduction. A video that
addresses the query in depth twenty minutes in, without signalling it in the first
few hundred words, was judged irrelevant on evidence that could not have shown it.

This is a ceiling on every precision figure here, and it cuts one way: it inflates
false-negative rates ("irrelevant" verdicts that a full read might overturn) while
leaving true positives largely intact — a document whose *opening* is clearly on
topic really is on topic. The judge-audit disagreement pattern recorded below (every
disagreement being `irrelevant → marginal`, never the reverse) is consistent with
exactly this bias, and is probably better explained by it than by prompt framing.

## Second superseding update (2026-08-25): reranker had a real precision bug, now fixed AND re-verified

Everything below — including the transcript-grounded numbers in the next section —
was measured before a real bug was found in the reranker itself: it loaded in
bfloat16 by default, which caused genuinely different documents to occasionally
quantize to bit-for-bit identical scores (42% collision rate in one measured batch).
Fixed by forcing float32 — full isolation trail and fix in `docs/DECISIONS.md`
(2026-08-25, "Reranker loaded in bfloat16 by default").

**Re-ran all three queries against the fixed reranker** and cross-checked the new
top-10 against the same transcript-grounded ground truth used below (a small number
of newly-surfaced candidates were judged from title context only, flagged as such,
rather than re-reading every transcript from scratch):

| Query | Pre-fix reranked | Post-fix reranked |
|---|---:|---:|
| "AI coding agents and developer tools" | 0.70 | ≈0.70-0.80 (one new, title-judged candidate) |
| "marketing, SEO, and advertising tools" | 0.50 | **0.60** |
| "humanoid robots hardware" | 0.00 | 0.00 (unchanged, correctly — the fix affects scoring, not whether real candidates exist) |

The fix did not overturn the earlier qualitative conclusion — if anything it modestly
*improved* the marketing query's precision (two clearly on-topic candidates the
buggy version hadn't surfaced in the top 10 — "Stop Buying SEO Tools!" and "I used AI
to make UGC Ads" — appeared this time), consistent with removing corrupted
tie-breaking rather than changing the model's actual judgment. The robots-query null
result staying at exactly 0.00 both times is itself a good sanity check: a scoring
bug should not be able to manufacture a match that isn't in the backlog, and it
didn't.

## Superseding update (same day): transcript-grounded re-run, reranker wins clearly

Everything below this point was judged from titles alone and is now known to be
unreliable (see the "Correction" section further down for how that was caught). This
section redoes the marketing/SEO query — the one under dispute — with real ground
truth: all 16 unique documents across the cosine-only top 10 and the
custom-instruction-reranked top 10 read in full (transcript text, not the 5-sentence
summary, not the title) and classified Relevant / Marginal / Irrelevant.

| Ranking | Precision@10 (strict) | Precision@10 (relevant+marginal) |
|---|---:|---:|
| Cosine-only | 0.20 | 0.40 |
| Reranked (custom instruction) | **0.50** | **0.80** |

Concretely, the reranker surfaced three genuinely on-topic documents that never made
the cosine-only top 10 at all: a clipping-agency video about brands paying for
social-media promotion (`this outbound strategy made me $45k last month`, a marketing
agency's outbound/CRM process), and a video whose worked example is explicitly an
"SEO agency for e-commerce businesses" scaling problem (`I Cloned Alex Hormozi in
ChatGPT`). Both are unambiguously on-topic once actually read, and cosine similarity
over the TextRank summary missed both.

**This reverses the "mixed result" and "made it worse" conclusions in the rest of this
file.** Those conclusions were artifacts of judging relevance from clickbait titles
("5 INSANE ChatGPT Work Use Cases" reads as generic; its transcript explicitly
discusses marketing outreach and SEO) — not a real reranker weakness. With real ground
truth, both the base reranker and the custom-instruction version outperform cosine
similarity alone on this query.

## All three original queries, now re-checked with real transcript reading

Extended the same rigor (all candidates read in full, both cosine-only and
custom-instruction-reranked top 10, union judged Relevant/Marginal/Irrelevant from
actual content) to the other two original queries.

| Query | Cosine precision@10 (strict) | Reranked precision@10 (strict) |
|---|---:|---:|
| "AI coding agents and developer tools" | 0.50 | **0.70** |
| "marketing, SEO, and advertising tools" | 0.20 | **0.50** |
| "humanoid robots hardware" | 0.00 | 0.00 |

Reranking wins clearly on the two queries where the unenriched backlog actually
contains good candidates — e.g. for the coding-agents query it promoted documents
about loop engineering, agentic coding discussions, and Claude Managed Agents above
generic AI-news/business-framework content that cosine similarity had ranked
alongside them.

The robots query is the instructive null result: **both rankings scored 0.00**,
confirming the "no floor" hypothesis from the original (title-only) pass, this time
with real ground truth. Reranking re-sorts a candidate pool; it can't manufacture
relevance in a pool that genuinely contains no good match (the one humanoid-robot
document in this corpus was already entity-extracted earlier in this session).
Several "hardware" candidates were AI-compute boxes (Strix Halo, DGX Spark rivals) or
edge-AI-agent devices — real hardware content, just not robots — which is why cosine
similarity pulled them in without either ranking method fixing it.

**Overall verdict, now on solid footing:** reranking with the corpus-specific
instruction is the better default when the backlog has real candidates, and neutral
(neither helps nor hurts) when it doesn't. The `max_distance`/`min_score` cutoff
question from the original pass is still open and is arguably the more useful next
lever for the robots-query failure mode specifically — no ranking method fixes "there
is nothing here," only a threshold that says so honestly instead of returning noise.

---

# Relevance gate: first-pass eval

`corpus.enrich.relevance_gate.find_relevant_unenriched_documents` ranks unenriched
documents by local (no-Claude) cosine similarity to a query, so `flows/query_entities.py`
only spends a `claude -p` call on documents actually relevant to what was asked. This
was shipped without measurement — same situation entity extraction itself was in before
`docs/EVAL.md` existed. This is that same discipline applied to the gate.

## Same limitation EVAL.md already names, inherited here

Ground truth below is my own (Claude's) relevance judgment reading titles, not an
independent human or third-party check — the same conflict-of-interest caveat
`docs/EVAL.md` logs for the entity-extraction ground truth applies here too, arguably
more sharply: I'm judging the output of a system built earlier in the same session.
Treat this as a first pass that motivated a concrete, checkable fix (below), not a
validated accuracy number.

## Method

3 queries, top_k=10 each, run against whatever the corpus's unenriched backlog
happened to be after the concurrency testing and the earlier 6+2 document extractions
in this session (not a controlled or curated sample — see EVAL.md's sampling caveat,
same story here). Each of the 30 returned (query, document title, distance) rows
classified as Relevant / Marginal / Irrelevant by reading the title alone (no
transcript read) — a real constraint on this pass, not a methodology choice: judging
from title only is weaker evidence than the transcript cross-referencing `EVAL.md`'s
entity-extraction rubric uses, and a document with a generic or clickbait title could
be misjudged in either direction.

## Results

| Query | Precision@10 (strict) | Precision@10 (relevant+marginal) | Distance range of hits |
|---|---:|---:|---|
| "AI coding agents and developer tools" | 0.40 | 0.70 | 0.249–0.359 (Claude Code / agent-specific hits mostly < 0.33) |
| "marketing, SEO, and advertising tools" | 0.10 | ~0.40 | one clean hit at 0.314, rest generic "AI + money" content |
| "humanoid robots hardware" | 0.00 | 0.00 | 0.462–0.599 — no genuine match anywhere in top 10 |

## The actual finding: the gate has no "no good match" signal

The robots query is the important result, not the weak one. It didn't fail by
returning bad-but-plausible candidates — it returned the top 10 *closest available*
documents in a backlog that likely doesn't contain a real match (the one humanoid-robot
document in this corpus, RobotEra L7, was already entity-extracted earlier in this
session and correctly excluded as "already enriched"). `find_relevant_unenriched_documents`
has no floor: asked for top_k, it always returns top_k, regardless of how weak the
best available match is.

Look at the distance bands: real hits across all three queries landed under ~0.36;
every result in the robots query — pure noise — landed at 0.46 or worse. That's a
visible gap, not a continuum, at least in this sample.

**Consequence:** the `max_distance` parameter already exists on
`find_relevant_unenriched_documents` (added speculatively, unset by default) — this
result is the first real evidence for where to set it, not just a hunch. **Not yet
changing the default** — one sample of 3 queries against one corpus snapshot is not
enough to pick a production threshold, and a cutoff that's too aggressive silently
starves genuine-but-unusually-phrased matches of enrichment, the same failure mode
`docs/EVAL.md` already tracks as "missed." Next step before trusting a hard cutoff:
run this same 3-query (or a larger) pass with `max_distance` swept across
{0.35, 0.40, 0.45} and check whether real hits above the line get cut.

## Secondary finding: precision degrades on generic-buzzword queries, not rare-topic ones

The marketing/SEO query didn't fail the way the robots query did — real marketing
content exists in the corpus and one genuine hit surfaced (`Claude Design + Claude
Skills: Automate Your Marketing`). The rest of the top 10 was generic "AI + get rich"
content that apparently embeds close to marketing-specific content under this
TextRank-summary-then-embed pipeline. Worth a second pass once there's a
larger/independent labeled set: is this a summarization artifact (TextRank pulling
generic sentences that dilute topic-specific signal) or a genuine embedding-model
limitation on this corpus's specific mix of buzzword-heavy titles.

## Correction (same day): the "mixed result" below was a bad ground-truth call, not a reranker problem

The section below concluded reranking made the marketing/SEO query *worse* because
the reranker's #1 pick, `5 INSANE ChatGPT Work Use Cases (Cowork Killer?)`, "has no
clearer claim to marketing/SEO than the one that actually is about marketing
automation." That judgment was made from the title alone — exactly the weakness this
doc's Method section already flagged ("a document with a generic or clickbait title
could be misjudged in either direction"), and it was wrong. Reading the actual
`document_summary.text` for that video: *"...I want you to go and find the top three
to five contacts at each of these companies for me to reach out to that run
influencer marketing or that are in the marketing department... in order to help them
with SEO."* That's a direct, on-topic match to "marketing, SEO, and advertising
tools" — arguably a stronger match than the video I'd assumed was the one correct
answer. The reranker's #1 pick was defensible both before and after the custom
instruction below; my ground truth was the thing that was wrong.

**What this actually shows:** the reranker isn't demonstrated broken by this test —
it's demonstrated *under-evaluated* by a ground-truth method too weak to judge it
(title-only, exactly as flagged as a risk in Method, now confirmed as a real failure
rather than a hedge). The custom-instruction comparison below (did rank drop from 1 to
5 mean the instruction hurt?) is not a valid conclusion either, since it was measured
against the same flawed ground truth — both documents may be genuinely relevant, in
which case "which one is #1" isn't the right question. This needs re-running with
transcript-text-read ground truth (the same standard `docs/EVAL.md` holds entity
extraction to) before any real conclusion about reranking, or the custom instruction,
holds up. Left the original section below unedited rather than deleting it — the
mistake and the correction are both worth keeping visible.

For the record: after adding the corpus-specific instruction (`corpus.embedding.rerank`'s
`_INSTRUCTION`), `Automate Your Marketing` dropped further, from rank 1 to rank 5
(score 0.0041), while the ChatGPT-use-cases video stayed at rank 1. Given the ground
truth this was measured against turned out to be wrong, this data point doesn't show
the instruction helped or hurt — it shows the same invalid comparison run twice. Not
re-running this yet; the next real step is re-labeling with transcript text, not
another reranker variant.

## Reranker (Qwen3-Reranker-4B) test — mixed result, not a clean win yet (see correction above)

Added `corpus.embedding.rerank` + `rerank_relevant_documents` to refine the cosine
gate's output with a cross-encoder, specifically to fix the marketing/SEO query's
precision problem above. Sanity-checked the mechanism first: the model card's own
"capital of France" example scored 0.976 (correct, high-confidence), and real-vs-nonsense
text on this corpus's own summaries showed a real 100x separation (0.0022 vs 0.000002)
— the reranker is discriminating, not broken or random.

But rerunning the actual marketing/SEO query end-to-end was a mixed result. The one
genuinely relevant document (`Claude Design + Claude Skills: Automate Your Marketing`)
was the cosine gate's #1 pick (distance 0.314) — correct. After reranking the same
50-document candidate pool, that document dropped to **#2** (score 0.0022), edged out
by `5 INSANE ChatGPT Work Use Cases (Cowork Killer?)` at #1 (score 0.0759) — a
generically-titled video with no clearer claim to "marketing, SEO, and advertising
tools" than the one that actually is about marketing automation. On this single query,
reranking did not improve the top-1 pick over cosine similarity alone.

**Working theory, not confirmed:** absolute reranker scores are heavily compressed on
this corpus's summary text (choppy TextRank sentences pulled from ASR transcripts,
nothing like the clean prose the model's public benchmarks likely use), and the
default instruction (`"Given a web search query, retrieve relevant passages that
answer the query"`) is generic — Qwen3-Reranker's own model card recommends a
task-specific instruction for better calibration, which hasn't been tried yet. Two
untested next steps, in order of cost: (1) a corpus-specific instruction string (free,
just a prompt change), (2) reranking over the full reconstructed transcript instead of
the 5-sentence TextRank summary (more signal, more latency per candidate).

**Consequence:** `--no-rerank` stays a real, load-bearing flag on
`flows/query_entities.py`, not just a fallback — on this one-query check, plain
cosine similarity was the better ranking. Reranking is not yet demonstrated to be an
improvement and should not be assumed as the default worth trusting until it's
re-tested with a tuned instruction across more than one query.

## What this doesn't tell you yet

- **Recall** — whether the gate is missing relevant documents that a lexical search
  would have caught. Not measured this pass; would need a pooled-candidate approach
  (embedding top-k ∪ keyword-search hits, judged together) like the retrieval eval
  in the build plan, not attempted here to keep this pass proportionate to what it's
  currently gating (a supplement to the nightly backfill, not the only path).
- **Whether title-only judging under- or over-counts precision** vs. reading the
  actual transcript, as `EVAL.md` does for entity extraction.
