#!/usr/bin/env python
"""Resolve candidate YouTube handles against Supadata and record what is real.

Web lists go stale and repeat each other's errors. This turns a scraped candidate
list into verified rows: canonical channel id, current subscriber count, video count,
lifetime views. Roughly 1 credit per handle.

Output lands in $PROJECT_DATA_DIR/bronze/channels/ — it is a raw API response and
belongs in bronze like any other.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

import httpx

from corpus.config import get_settings

# Candidates gathered from research, grouped by the job each serves.
CANDIDATES: dict[str, list[str]] = {
    "vendor_primary": [
        "@anthropic-ai",
        "@OpenAI",
        "@GoogleDeepMind",
        "@HuggingFace",
        "@IBMTechnology",
        "@MicrosoftAzure",
        "@amazonwebservices",
        "@NVIDIADeveloper",
        "@Databricks",
        "@SnowflakeInc",
        "@MistralAI_",
        "@cohere",
        "@AIatMeta",
    ],
    "interviews_positions": [
        "@DwarkeshPatel",
        "@LatentSpacePod",
        "@MachineLearningStreetTalk",
        "@hardfork",
        "@NoPriorsPodcast",
        "@a16z",
        "@lexfridman",
        "@twimlai",
        "@CognitiveRevolutionPodcast",
        "@ycombinator",
        "@SequoiaCapital",
    ],
    "news_analysis": [
        "@aiexplained-official",
        "@AIDailyBrief",
        "@NateBJones",
        "@asianometry",
        "@lastweekinai",
        "@DrAlanDThompson",
        "@WhatsAI",
        "@aishowpod",
        "@t3dotgg",
    ],
    "engineering_practice": [
        "@AndrejKarpathy",
        "@SebastianRaschka",
        "@umarjamilai",
        "@arp_ai",
        "@ColeMedin",
        "@indydevdan",
        "@samwitteveenai",
        "@GosuCoder",
        "@ArminRonacher",
        "@YannicKilcher",
        "@bycloudAI",
        "@code4AI",
        "@donatocapitella",
        "@howiaipodcast",
        "@AZisk",
        "@DigitalSpaceport",
    ],
    "foundations_safety": [
        "@3blue1brown",
        "@statquest",
        "@WelchLabs",
        "@Eigensteve",
        "@juliaturc1",
        "@Computerphile",
        "@RobertMilesAI",
    ],
    "mixed_caution": ["@Fireship", "@mreflow", "@theAIsearch"],
    # Flagged hype-heavy by the signal ranking. Resolved anyway so the exclusion is
    # recorded as a decision rather than an omission.
    "excluded_hype": ["@matthew_berman", "@TheAiGrid", "@WesRoth", "@DaveShap"],
}


def main() -> int:
    settings = get_settings()
    if not settings.has_supadata_key:
        print("SUPADATA_API_KEY not set", file=sys.stderr)
        return 1

    out_dir = settings.bronze_dir / "channels"
    out_dir.mkdir(parents=True, exist_ok=True)

    client = httpx.Client(
        base_url=settings.supadata_base_url,
        headers={"x-api-key": settings.supadata_api_key.get_secret_value()},
        timeout=30.0,
    )
    results: list[dict] = []
    spent = 0

    with client:
        for category, handles in CANDIDATES.items():
            for handle in handles:
                response = client.get("/youtube/channel", params={"id": handle})
                spent += 1
                if response.status_code == 200:
                    data = response.json()
                    row = {
                        "category": category,
                        "requested_handle": handle,
                        "resolved": True,
                        "channel_id": data.get("id"),
                        "name": data.get("name"),
                        "handle": data.get("handle"),
                        "subscribers": data.get("subscriberCount"),
                        "videos": data.get("videoCount"),
                        "total_views": data.get("viewCount"),
                        "description": (data.get("description") or "")[:200],
                    }
                    vids = row["videos"] or 0
                    row["avg_views_lifetime"] = round(row["total_views"] / vids) if vids else None
                    print(f"  ok   {handle:32} {row['name'][:28]:30} {row['subscribers']:>10,}")
                else:
                    row = {
                        "category": category,
                        "requested_handle": handle,
                        "resolved": False,
                        "status": response.status_code,
                        "error": response.text[:120],
                    }
                    print(f"  MISS {handle:32} -> {response.status_code}")
                results.append(row)

    payload = {
        "_fetched_at": datetime.now(UTC).isoformat(),
        "_source": "supadata /youtube/channel",
        "_credits_spent": spent,
        "channels": results,
    }
    path = out_dir / "candidates.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    resolved = [r for r in results if r["resolved"]]
    print(f"\nresolved {len(resolved)}/{len(results)}; {spent} credits; -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
