# Channel seeds

`youtube_channels.yaml` is the ingestion seed list: 336 channels, verified against
the live Supadata and yt-dlp APIs (never guessed from a display name or a listicle),
deduplicated by handle, classified by `domain` and `authority_tier`.

## Cost

Full backfill is **~63,300 credits — 2.1 months** at the 30,000/month budget.
`phase` encodes a deliberate ramp rather than an arbitrary label:

| Phase | Channels | Credits | Cumulative |
|---|---:|---:|---:|
| `research-p1` | 23 | 9,053 | 0.30 mo |
| `subscriptions` | 50 | 12,607 | 0.72 mo |
| `automation` | 46 | 11,925 | 1.12 mo |
| `research-p2` | 36 | 29,712 | 2.11 mo |
| `research-p3-deferred` | 1 (AWS) | — | not counted |
| `subs5-ai-p1` | 29 | ~7,800 | — |
| `subs5-ai-p2` | 34 | ~6,000 | — |
| `subs5-entrepreneurship` | 58 | ~17,900 | — |
| `subs5-personal` | 41 | ~9,500 | — |

The `subs5-*` phases (2026-08-27) are 162 channels classified `recommend: true` in
round four and left out then purely on cost. Adding them here spends nothing — `phase`
is the ramp. Uncapped they are 58,519 credits; capping the 15 channels over 800 videos
at 400 most recent brings it to ~41,200. Ordered so the domains this corpus exists for
come first: AI research/automation by classifier confidence, then entrepreneurship,
then personal development.

Ingest in phase order. `research-p3-deferred` (AWS, 18,000 videos) is never ingested
by channel — pull specific re:Invent or product-announcement playlists instead, or it
alone costs 60% of a month for the lowest signal density of any source here.

## Caps

Two channels are large enough relative to their own tier that a full backfill would
be mostly repetition: `@bigthink` (9,603 videos → cap at 300 most recent) and
`@asapguide` (2,366 → cap at 200). The cap is recorded in each row's `note`; the
ingestion pipeline (step 3) must read and respect it, not just `videos_at_survey`.

The `subs5-*` round adds 15 more over 800 videos, capped at 400 most recent on the
same reasoning — they carry 40% of that round's cost between them. Same rule applies:
the cap lives in `note` and the pipeline must honour it.

## Regenerating

The file is generated, not hand-assembled, from three rounds of bronze data plus the
classification decisions made in review:

```
uv run python scripts/build_channel_seeds.py
```

`domain` and `authority_tier` are the two columns meant to be hand-tuned afterward —
edit the YAML directly for those. Everything else (name, survey counts) is a
point-in-time fact and will drift; re-run the script against fresh bronze data rather
than hand-editing it.

## Domain, not just tier

`domain` (`ai_research` / `ai_automation` / `entrepreneurship` /
`personal_development`) is orthogonal to `authority_tier`. It exists so
term-velocity and saturation analytics don't blend an entrepreneurship channel's
vocabulary with an AI vendor's — see `docs/DECISIONS.md`.
