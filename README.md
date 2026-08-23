# research-corpus

Private multi-source research corpus queried by Claude Code as an MCP tool

## Setup

Requires [uv](https://docs.astral.sh/uv/) and [direnv](https://direnv.net/).

```bash
gh repo clone <your-github-username>/research-corpus
cd research-corpus
direnv allow          # exports PROJECT_DATA_DIR
mkdir -p "$PROJECT_DATA_DIR"/{volumes,bronze,cache}
cp .env.example .env  # then fill in real values
uv sync
```

## Data

This repo contains no data. Everything it reads or writes lives under
`$PROJECT_DATA_DIR`:

| Path | Contents | Safe to delete |
|---|---|---|
| `bronze/` | raw, immutable source responses | no |
| `cache/` | derived, reconstructable from bronze | yes |
| `volumes/` | database files and container state | no |

On this machine that is `~/data/research-corpus`. Elsewhere it is wherever
`PROJECT_DATA_DIR` points — the code never assumes.

## Usage

```bash
uv run pytest

# Ingest configured YouTube seeds
uv run python flows/ingest_youtube.py [--phase research-p1] [--limit 5] [--dry-run]

# Extract entities for anything not yet processed at the current prompt version
uv run python flows/nightly_entities.py [--limit 50] [--dry-run]

# Local dashboard: uv run python scripts/run_web.py, then cd web/ && pnpm dev
```

Both flows are meant to run daily, chained, via `scripts/nightly.sh` — see
[launchd/README.md](launchd/README.md) for scheduling it (macOS launchd; a systemd
timer on the eventual Linux migration, same script either way).

## Decisions

See [docs/DECISIONS.md](docs/DECISIONS.md).
