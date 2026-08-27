#!/usr/bin/env python
"""Resolve subscription display names to verified channel handles.

    uv run python scripts/resolve_new_subscriptions.py NAMES_FILE

One-time survey script, same category as build_channel_seeds.py and
resolve_subscriptions_full.py. Free — yt-dlp only, no Supadata credits.

Exists because a YouTube subscription export gives display *names*, and
`seeds/README.md` is explicit that channels are "verified against the live Supadata
and yt-dlp APIs (never guessed from a display name or a listicle)". So this proposes
a handle from the name and then **checks what the live channel actually calls
itself** before accepting it. A candidate whose real name does not match the
subscription name is reported as unresolved, not quietly seeded.

Deliberately does not classify. Domain and authority_tier are judgment calls that
`scripts/classify_subscriptions.py` already makes with an LLM over real channel
metadata; guessing them from a name here would be the same error this script exists
to prevent.

MAX_WORKERS is small on purpose: this is yt-dlp against YouTube with no API key, and
a burst of ~90 calls demonstrably trips its bot detection (docs/DECISIONS.md,
2026-08-27).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from corpus.config import get_settings

MAX_WORKERS = 3
TIMEOUT_S = 60


def norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", value.lower())


def candidate_handles(name: str) -> list[str]:
    """Handle guesses, most likely first. Each is verified before being accepted."""
    base = unicodedata.normalize("NFKD", name)
    base = "".join(c for c in base if not unicodedata.combining(c))
    # Display names often carry a suffix after | or - that is not part of the handle.
    trimmed = re.split(r"[|(–—]| - ", base)[0].strip()
    out = []
    for variant in (trimmed, base):
        squashed = re.sub(r"[^A-Za-z0-9]", "", variant)
        if squashed:
            out.append("@" + squashed)
    if name.startswith("@"):
        out.insert(0, name)
    seen, unique = set(), []
    for h in out:
        if h.lower() not in seen:
            seen.add(h.lower())
            unique.append(h)
    return unique


def probe(handle: str) -> dict | None:
    """Live channel metadata, or None if the handle does not resolve."""
    try:
        proc = subprocess.run(
            [
                "yt-dlp",
                "--no-warnings",
                "--flat-playlist",
                "--playlist-end",
                "1",
                "--print",
                "%(channel)s\t%(channel_id)s\t%(channel_follower_count)s",
                f"https://www.youtube.com/{handle}/videos",
            ],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return None
    line = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0 or not line:
        return None
    parts = line[0].split("\t")
    if len(parts) < 2 or not parts[1].startswith("UC"):
        return None
    subs = parts[2] if len(parts) > 2 else "NA"
    return {
        "handle": handle,
        "channel_name": parts[0],
        "channel_id": parts[1],
        "subscribers": int(subs) if subs.isdigit() else None,
    }


def resolve(name: str) -> dict:
    for handle in candidate_handles(name):
        found = probe(handle)
        if not found:
            continue
        exact = norm(found["channel_name"]) == norm(name)
        return {
            "subscription_name": name,
            **found,
            "verified": exact,
            "note": "" if exact else f"live channel is named {found['channel_name']!r}",
        }
    return {"subscription_name": name, "handle": None, "verified": False,
            "note": "no candidate handle resolved"}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    with open(sys.argv[1]) as fh:
        names = [ln.strip() for ln in fh if ln.strip()]
    print(f"resolving {len(names)} names at concurrency {MAX_WORKERS} (free, yt-dlp only)\n")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(resolve, names))

    verified = [r for r in results if r.get("verified")]
    mismatched = [r for r in results if r.get("handle") and not r.get("verified")]
    unresolved = [r for r in results if not r.get("handle")]

    for r in results:
        mark = "OK  " if r.get("verified") else ("HUH " if r.get("handle") else "MISS")
        subs = r.get("subscribers")
        subs_s = f"{subs:,}" if isinstance(subs, int) else "-"
        print(f"  [{mark}] {r['subscription_name'][:34]:<34} {str(r.get('handle') or ''):<26} "
              f"{subs_s:>10}  {r.get('note','')[:40]}")

    out = get_settings().bronze_dir / "channels" / "new_subscriptions_resolved.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"resolved_at": datetime.now(UTC).isoformat(), "channels": results}, indent=2))
    print(f"\n  verified {len(verified)}, name-mismatch {len(mismatched)}, "
          f"unresolved {len(unresolved)}")
    print(f"  -> {out}")
    print("  Mismatches and misses need a human eye; nothing here is seeded automatically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
