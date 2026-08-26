#!/usr/bin/env python
"""Classify all 425 resolved subscriptions: domain, authority tier, and whether
each is worth monitoring for ingestion.

    uv run python scripts/classify_subscriptions.py

One-time curation aid, not part of the ingest pipeline (same category as
build_channel_seeds.py). Batches channels through headless Claude Code rather
than one call per channel — this is judgment over static text (name/description/
tags), not per-document work, so there's no reason to pay for 425 separate calls
when a few large ones do the same job. Cross-references the result against the
existing seed table so the report shows what's already being monitored versus
what's a new candidate, and never writes to seeds/youtube_channels.yaml itself —
that's a content decision for a human to make from the report.
"""

from __future__ import annotations

import json
import re
import subprocess

import structlog
from pydantic import BaseModel, Field, ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from corpus.config import get_settings

log = structlog.get_logger(__name__)

BATCH_SIZE = 25  # 70 truncated output on 4 of 7 batches; the CLI exposes no
# --max-tokens to raise the ceiling instead, so the fix is a smaller batch
MODEL = "haiku"
TIMEOUT_S = 180.0

DOMAINS = [
    "ai_automation",
    "ai_research",
    "entrepreneurship",
    "personal_development",
    "out_of_scope",
]
TIERS = ["vendor_official", "established_media", "practitioner", "aggregator", "unknown"]

_PROMPT_HEADER = f"""\
You are triaging a list of YouTube channels (this person's actual subscriptions) \
for a private research corpus about AI, automation/no-code tooling, tech \
entrepreneurship, and personal development/productivity content. The corpus \
ingests full video transcripts for comparative and temporal analysis (who said \
what before it was consensus, sentiment over time) — not for general knowledge.

For each channel, classify:
- domain: one of {DOMAINS}. The existing corpus already treats personal_development \
broadly — it includes health/neuroscience/longevity content aimed at self-optimization \
(e.g. Andrew Huberman, Bryan Johnson), communication/social skills coaching, and \
productivity/writing advice, not just narrow productivity-app content. Use \
"out_of_scope" for what's unrelated to any of the four even under that broader \
reading (personal vlogs, gaming, music, general entertainment, hobbies unconnected \
to self-improvement or the other three domains)
- authority_tier: one of {TIERS} — vendor_official (a company's own channel), \
established_media (a known publication/show), practitioner (an individual \
creator/expert), aggregator (reposts/compiles others' content), unknown if you \
can't tell
- recommend: true/false — should this channel actually be added for ongoing \
transcript ingestion? Weigh topical relevance AND whether there's enough real \
content to be worth it (near-zero videos, or an aggregator with no original \
insight, should usually be false even if nominally on-topic)
- confidence: 0.0-1.0 — how sure you are, given only a name/description/subs/tags
- reason: under 10 words

Respond with ONLY a JSON array, no markdown fence, no prose before or after it, \
one object per channel in the exact order given, each shaped:
{{"handle": str, "domain": str, "authority_tier": str, "recommend": bool, \
"confidence": float, "reason": str}}

Channels:
"""


class ChannelClassification(BaseModel):
    handle: str
    domain: str = Field(pattern="^(" + "|".join(DOMAINS) + ")$")
    authority_tier: str = Field(pattern="^(" + "|".join(TIERS) + ")$")
    recommend: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


class ClaudeCodeCallError(Exception):
    pass


class ClassificationError(Exception):
    pass


def _strip_markdown_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


@retry(
    retry=retry_if_exception_type(ClaudeCodeCallError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
def _run_claude_code(prompt_body: str) -> str:
    try:
        proc = subprocess.run(
            ["claude", "-p", _PROMPT_HEADER + prompt_body, "--model", MODEL,
             "--output-format", "json", "--allowedTools", ""],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCodeCallError(f"claude CLI timed out after {TIMEOUT_S}s") from exc
    except FileNotFoundError as exc:
        raise ClaudeCodeCallError("claude CLI not found on PATH") from exc

    if proc.returncode != 0:
        raise ClaudeCodeCallError(f"claude CLI exited {proc.returncode}: {proc.stderr[:500]}")
    return proc.stdout


def classify_batch(channels: list[dict]) -> list[ChannelClassification]:
    lines = []
    for c in channels:
        desc = (c.get("desc") or "").replace("\n", " ")[:200]
        tags = ", ".join(c.get("tags") or [])
        lines.append(
            f"- handle: {c['handle']} | name: {c.get('name')} | subs: {c.get('subs')} | "
            f"videos: {c.get('videos')} | desc: {desc} | tags: {tags}"
        )
    prompt_body = "\n".join(lines)

    stdout = _run_claude_code(prompt_body)
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ClassificationError(
            f"claude CLI did not return a JSON envelope: {stdout[:300]!r}"
        ) from exc

    result_text = _strip_markdown_fence(envelope.get("result", ""))
    try:
        payload = json.loads(result_text)
        return [ChannelClassification.model_validate(row) for row in payload]
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ClassificationError(
            f"model output did not match the classification schema: {result_text[:300]!r}"
        ) from exc


def _chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def main() -> int:
    settings = get_settings()
    bronze = settings.bronze_dir / "channels"
    full_raw = json.loads((bronze / "subscriptions_full_resolved.json").read_text())
    counts_raw = json.loads((bronze / "subscriptions_full_counts.json").read_text())
    full = {c["handle"]: c for c in full_raw["channels"]}
    counts = {c["handle"]: c for c in counts_raw["channels"]}

    merged = []
    for handle, meta in full.items():
        cnt = counts.get(handle, {})
        merged.append(
            {
                **meta,
                "handle": handle,
                "videos": cnt.get("videos"),
                "capped": cnt.get("capped", False),
            }
        )

    out_path = bronze / "subscriptions_classified.json"
    done: dict[str, dict] = {}
    if out_path.exists():
        done = {r["handle"]: r for r in json.loads(out_path.read_text())["channels"]}
        print(f"resuming: {len(done)} already classified")

    todo = [m for m in merged if m["handle"] not in done]
    results = list(done.values())

    for batch_num, batch in enumerate(_chunk(todo, BATCH_SIZE), 1):
        print(f"batch {batch_num} ({len(batch)} channels)...")
        try:
            classified = classify_batch(batch)
        except (ClaudeCodeCallError, ClassificationError) as exc:
            log.warning("batch_failed", batch_num=batch_num, error=str(exc))
            print(f"  ! batch {batch_num} failed: {exc}")
            continue

        by_handle = {c.handle: c for c in classified}
        for m in batch:
            cls = by_handle.get(m["handle"])
            if cls is None:
                print(f"  ! no classification returned for {m['handle']}")
                continue
            row = {**m, **cls.model_dump()}
            results.append(row)
            done[m["handle"]] = row

        out_path.write_text(
            json.dumps({"channels": results}, indent=2, ensure_ascii=False)
        )
        print(
            f"  {len(classified)}/{len(batch)} classified; "
            f"{len(results)}/{len(merged)} total so far"
        )

    print(f"\ndone: {len(results)}/{len(merged)} classified -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
