# Entity extraction: rubric and scoring

Step 4's actual gate (see the build plan): measure the extractor's precision/recall
on hand-labeled documents before trusting it in production. This exists because it
was never done — `nightly_entities.py` had been running against real data with zero
measurement of whether its output is any good.

## Why this exists now, specifically

Two things converged on 2026-08-24: the first real backfill (3,344 documents) landed,
and a 5-document test run of the Claude-based extractor took ~75s/document — a full
run would take ~70 hours. That's expensive enough, in both time and Claude usage, to
be worth checking against a free, fast local alternative (GLiNER) before committing to
either at scale, rather than relying on the untested assumption (from a different
project's experience) that the LLM approach is better *for this corpus's specific
content*.

## Rubric

For every candidate entity mention an extractor produces, classify it as one of:

| Verdict | Definition |
|---|---|
| **Correct** | The surface form genuinely appears in the transcript, names a real vendor/person/technique/regulation/product/organization, and the `kind` label is reasonable. |
| **Wrong kind** | The entity is real and correctly named, but misclassified (e.g. "Claude Code" tagged `person`). Tracked separately from a clean miss — it's a different failure mode (ontology confusion, not hallucination). |
| **Hallucinated** | Doesn't correspond to anything real — fabricated outright, or an ASR mis-transcription treated as if it were a real name (e.g. "Codeex" for "Codex", "CMX Conductor" for something unclear). This is the failure mode auto-generated captions specifically invite. |
| **Missed** | A real, salient entity clearly present in the transcript that the extractor didn't surface at all. Ground truth records these; no extractor output corresponds to them. |

## Metrics, per extractor per document

- **Precision** = correct / (correct + hallucinated)
- **Recall** = correct / (correct + missed)
- **F1** = harmonic mean of precision and recall
- **Kind accuracy** = correct / (correct + wrong-kind), over true positives only
- **Latency** — seconds per document
- **Determinism** — same input, same output on a rerun? (Claude is stochastic; GLiNER is deterministic at a fixed threshold)
- **Cost** — Claude: subscription time/quota, no per-token metering here; GLiNER: local CPU/GPU only, zero marginal cost

## Ground truth process, and its real limitation

Ground truth per document is compiled by reading the transcript directly and
cross-referencing both extractors' candidate outputs (pooled-candidate review, same
spirit as the retrieval eval's pooled-recall approach elsewhere in this project) —
accepting or rejecting each candidate on independent judgment, then adding anything
both extractors missed.

**Named limitation, not glossed over:** one of the two extractors under test is
Claude-based, and the ground truth is also compiled by Claude (in this analysis
context, not through the automated `entities.py` pipeline). That's the same
LLM-assisted-labeling bias the project's own eval-cost section already flags for
retrieval ground truth — a genuine conflict of interest, not a hypothetical one.
Mitigated by treating every Claude-extractor candidate with default skepticism
(reject unless independently verifiable in the transcript's own text) rather than
accepting it uncritically, but this is not a substitute for an actually-independent
human or third-party check. Worth a blind audit pass later if this result ends up
load-bearing for a big decision.

## Sample

5 documents so far (the first batch `nightly_entities.py` processed against real
backfilled content), chosen only by being first in the queue, not curated for
difficulty — extend to ~20 per the plan's original target before treating this as
final. See `scripts/eval_entity_extraction.py` for the scoring implementation and
`/tmp/eval_docs.json` for the raw transcript text used (scratch, not committed).

## Results so far (2026-08-24)

| Extractor | Avg F1 (3 real docs) | Speed | Notable failure mode |
|---|---:|---|---|
| Claude (haiku, headless) | ~0.90 | ~75s/doc | Occasional minor hallucination (e.g. "AIO"), otherwise strong |
| GLiNER (`urchade/gliner_base`, local, zero-shot) | ~0.20 | <1s/doc | Extracts generic noun phrases as named entities ("AI tools", "page template"); no ASR-error correction (kept "Stamford" for "Stanford") |
| Qwen2.5-7B-Instruct (local, via Ollama) | ~0.41 | 2-10s/doc | **Outright fabrication** — invented "Snowflake" as an entity in two documents where the word never appears in the source text at all. Also missed obvious entities GLiNER caught ("Andrew Huberman"). Confidence poorly calibrated (~1.0 on nearly everything, including fabrications). |

Claude remains the clear quality leader. Both local alternatives tested so far have
real, disqualifying problems for this specific task — GLiNER over-extracts generic
phrases and can't correct ASR errors; the 7B local LLM fabricates entities outright,
which is arguably worse for a research corpus's integrity than GLiNER's more
"grounded" mistakes.

## Qwen2.5-14B-Instruct (local, via Ollama) — 2026-08-24

Same 5 documents, same prompt, `qwen2.5:14b-instruct` (already pulled locally, no
download needed). 3-12s/doc — far faster than Claude, and than the 7B/32B runs above.
Answers the "next" question from the row above: capability scaling helps, but does
not solve the core problem.

**Real improvement over the 7B result:**
- No outright fabrication analogous to the 7B's invented "Snowflake" — every entity
  extracted corresponds to something actually in the source text.
- Genuine ASR-correction, done correctly: canonicalized "Stamford School of Medicine"
  (the literal mistranscribed text) to "Stanford School of Medicine" while preserving
  the ASR error as the surface form for exact-match search — exactly the split
  `entities.py`'s design calls for, done right without being told to.
- Tied Claude at 0 entities on the one near-empty document in the sample (119
  characters of actual transcript) — a fair tie, not a miss by either extractor.

**Not fixed:**
- Reproduced two of the *exact* hallucinations already named as illustrative bad
  examples in `entities.py`'s own docstring — "Codeex" (for Codex) and "CMX
  Conductor" — on the Spotify document. This is the precise ASR-mistranscription-
  treated-as-a-real-name failure mode the rubric exists to catch, still present at
  14B, just no longer in its most extreme "invented from nothing" form.
- Some generic-phrase over-extraction ("Marketing Studio", "AI coding blocks") — the
  same failure mode that disqualified GLiNER, present here at a lower rate, not absent.
- At least one wrong-kind error: "FBA" (Fulfillment by Amazon) tagged `regulation`
  instead of a product/vendor-adjacent kind.

**Verdict:** meaningfully better than 7B, still not production-safe for a research
corpus whose whole value proposition is trustworthy provenance. The specific failure
mode this project's own rubric was built to catch (ASR errors laundered into
apparent real names) survived scaling from 7B to 14B unchanged in kind, only reduced
in frequency and severity. Claude remains the extractor of record; local models are a
faster, free alternative with a still-open trust problem, not yet a substitute.
