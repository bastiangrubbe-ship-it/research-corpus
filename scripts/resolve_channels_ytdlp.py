#!/usr/bin/env python
"""Resolve candidate channel handles with yt-dlp instead of Supadata.

Discovery and metadata cost nothing here. Supadata charges a credit per video for
data yt-dlp returns free, and yt-dlp returns *more*: an exact upload timestamp
(Supadata's uploadDate is optional) and, critically, `subtitles` vs
`automatic_captions` as separate keys — which is transcript provenance, the thing
Supadata cannot report at any price.

The division of labour that follows from that:
    yt-dlp   -> discovery, metadata, dates, caption provenance   (free, throttles)
    Supadata -> transcript text                                  (1 credit, reliable)
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime

from corpus.config import get_settings

CANDIDATES: dict[str, list[str]] = {
    "n8n_make_automation": [
        "@nicksaraev",
        "@nateherk",
        "@jonocatliff",
        "@LiamOttley",
        "@RoboNuggets",
        "@n8n-io",
        "@JackRobertsAI",
        "@AIAutomationSociety",
        "@productivity-ai",
        "@MakeHQ",
        "@zapier",
        "@AndyLoZero2Launch",
        "@duyettran",
        "@aiwithbrandon",
        "@TylerAI",
        "@AutomateWithMarc",
        "@leonvanzyl",
        "@MaxBrodieAI",
    ],
    "agency_business_model": [
        "@brendanjowett",
        "@HasanAboulHasan",
        "@JulianGoldieSEO",
        "@MorningsideAI",
        "@thisisjasonwest",
        "@aiagencyacademy",
        "@GregIsenberg",
        "@StartupIdeasPod",
        "@bensuiter",
        "@AIProfitBoardroom",
    ],
    "vibe_coding_build_public": [
        "@AlexFinnOfficial",
        "@rileybrownai",
        "@marclou",
        "@IndyDevDan",
        "@codewithantonio",
        "@WebDevCody",
        "@theo-t3",
        "@bearlyai",
        "@DavidOndrej",
        "@AIJason",
        "@matthewberman",
    ],
    "ai_content_business": [
        "@sandyleeai",
        "@TheAIAdvantage",
        "@SkillLeapAI",
        "@futurepedia_io",
        "@CorbinBrown",
        "@ThuVuDataAnalytics",
        "@aiexplained-official",
        "@MattWolfe",
        "@mreflow",
        "@AIFoundationsChannel",
        "@HeyItsRobbyy",
        "@IncomeStreamSurfers",
        "@AIAndyOfficial",
        "@aiadvantage",
    ],
    "agent_engineering_crossover": [
        "@ColeMedin",
        "@samwitteveenai",
        "@GosuCoder",
        "@AIDrivenDev",
        "@DecodingML",
        "@aiwithkyle",
        "@techwithtim",
    ],
}


def resolve(handle: str) -> dict:
    """Channel-level metadata. `--playlist-items 0` skips enumerating videos."""
    url = f"https://www.youtube.com/{handle}"
    try:
        proc = subprocess.run(
            [
                "yt-dlp",
                "--skip-download",
                "--dump-single-json",
                "--no-warnings",
                "--playlist-items",
                "0",
                "--socket-timeout",
                "20",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        return {"requested_handle": handle, "resolved": False, "error": "timeout"}

    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        return {
            "requested_handle": handle,
            "resolved": False,
            "error": (err[-1][:140] if err else f"exit {proc.returncode}"),
        }
    try:
        d = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"requested_handle": handle, "resolved": False, "error": "unparseable json"}

    return {
        "requested_handle": handle,
        "resolved": True,
        "channel_id": d.get("channel_id"),
        "name": d.get("channel"),
        "handle": d.get("uploader_id"),
        "subscribers": d.get("channel_follower_count"),
        "description": (d.get("description") or "")[:300],
        "tags": (d.get("tags") or [])[:12],
    }


def main() -> int:
    settings = get_settings()
    out_dir = settings.bronze_dir / "channels"
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    for category, handles in CANDIDATES.items():
        for handle in handles:
            row = resolve(handle) | {"category": category}
            if row["resolved"]:
                subs = row.get("subscribers") or 0
                print(f"  ok   {handle:26} {str(row['name'])[:30]:32} {subs:>10,}")
            else:
                print(f"  MISS {handle:26} {row['error'][:60]}")
            results.append(row)
            time.sleep(0.4)  # be polite; this is scraping, not an API

    payload = {
        "_fetched_at": datetime.now(UTC).isoformat(),
        "_source": "yt-dlp (no Supadata credits spent)",
        "channels": results,
    }
    path = out_dir / "automation_candidates.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    ok = sum(1 for r in results if r["resolved"])
    print(f"\nresolved {ok}/{len(results)}; 0 credits spent; -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
