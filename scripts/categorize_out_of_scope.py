#!/usr/bin/env python
"""Assign a topical category + one-line description to each out_of_scope channel.

    uv run python scripts/categorize_out_of_scope.py

Follow-up to classify_subscriptions.py: that pass only recorded *why* a channel
was excluded from the corpus, not *what it actually is*. This groups the 138
out-of-scope channels into natural topical buckets (cooking, gaming, tech
reviews, etc.) for review — same batching approach, same reason: judgment over
static text, so a handful of large calls beats 138 individual ones.
"""

from __future__ import annotations

import json
import re
import subprocess

import structlog
from pydantic import BaseModel, ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from corpus.config import get_settings

log = structlog.get_logger(__name__)

BATCH_SIZE = 25
MODEL = "haiku"
TIMEOUT_S = 180.0

_PROMPT_HEADER = """\
For each YouTube channel below, give a short topical category (2-3 words, e.g. \
"cooking/food", "tech reviews", "gaming", "K-pop/music", "fashion/beauty", \
"fitness/sports", "entertainment/vlogs" -- pick whatever's accurate, these are \
examples not a fixed list) and a one-sentence description of what the channel \
actually covers (not why it's excluded from anything -- just what it is).

Respond with ONLY a JSON array, no markdown fence, no prose before or after it, \
one object per channel in the exact order given, each shaped:
{"handle": str, "category": str, "description": str}

Channels:
"""


class ChannelCategory(BaseModel):
    handle: str
    category: str
    description: str


class ClaudeCodeCallError(Exception):
    pass


class CategorizationError(Exception):
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
            [
                "claude", "-p", _PROMPT_HEADER + prompt_body,
                "--model", MODEL, "--output-format", "json", "--allowedTools", "",
            ],
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


def categorize_batch(channels: list[dict]) -> list[ChannelCategory]:
    lines = []
    for c in channels:
        desc = (c.get("desc") or "").replace("\n", " ")[:200]
        tags = ", ".join(c.get("tags") or [])
        lines.append(
            f"- handle: {c['handle']} | name: {c.get('name')} | desc: {desc} | tags: {tags}"
        )
    prompt_body = "\n".join(lines)

    stdout = _run_claude_code(prompt_body)
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise CategorizationError(
            f"claude CLI did not return a JSON envelope: {stdout[:300]!r}"
        ) from exc

    result_text = _strip_markdown_fence(envelope.get("result", ""))
    try:
        payload = json.loads(result_text)
        return [ChannelCategory.model_validate(row) for row in payload]
    except (json.JSONDecodeError, ValidationError) as exc:
        raise CategorizationError(
            f"model output did not match the category schema: {result_text[:300]!r}"
        ) from exc


def _chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def main() -> int:
    settings = get_settings()
    bronze = settings.bronze_dir / "channels"
    classified = json.loads((bronze / "subscriptions_classified.json").read_text())["channels"]
    oos = [c for c in classified if c["domain"] == "out_of_scope"]

    out_path = bronze / "subscriptions_out_of_scope_categorized.json"
    done: dict[str, dict] = {}
    if out_path.exists():
        done = {r["handle"]: r for r in json.loads(out_path.read_text())["channels"]}
        print(f"resuming: {len(done)} already categorized")

    todo = [c for c in oos if c["handle"] not in done]
    results = list(done.values())

    for batch_num, batch in enumerate(_chunk(todo, BATCH_SIZE), 1):
        print(f"batch {batch_num} ({len(batch)} channels)...")
        try:
            categorized = categorize_batch(batch)
        except (ClaudeCodeCallError, CategorizationError) as exc:
            log.warning("batch_failed", batch_num=batch_num, error=str(exc))
            print(f"  ! batch {batch_num} failed: {exc}")
            continue

        by_handle = {c.handle: c for c in categorized}
        for c in batch:
            cat = by_handle.get(c["handle"])
            if cat is None:
                print(f"  ! no category returned for {c['handle']}")
                continue
            row = {**c, "category": cat.category, "description": cat.description}
            results.append(row)
            done[c["handle"]] = row

        out_path.write_text(json.dumps({"channels": results}, indent=2, ensure_ascii=False))
        print(
            f"  {len(categorized)}/{len(batch)} categorized; "
            f"{len(results)}/{len(oos)} total so far"
        )

    print(f"\ndone: {len(results)}/{len(oos)} categorized -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
