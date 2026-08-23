# Scheduling

`com.bastiangrubbe.research-corpus.nightly.plist` runs [scripts/nightly.sh](../scripts/nightly.sh)
once a day: ingest, then extract entities from whatever's new. It is not installed
automatically — installing it turns this from "a script you run by hand" into a
standing job that runs unattended every night, spending Supadata credits and Claude
usage on its own, so that's a deliberate step you take, not something done for you.

## Before installing

1. Fill in the real `claude` CLI directory in the plist's `PATH` — it wasn't
   discoverable from this session. Find it with `command -v claude` in your normal
   shell and use its directory (not the file path itself).
2. Generate a long-lived token and add it to `.env`:
   ```bash
   claude setup-token
   ```
   ```
   # in .env
   CLAUDE_CODE_OAUTH_TOKEN=...
   ```
   This is a subscription login, not a metered API key — see
   [docs/DECISIONS.md](../docs/DECISIONS.md).
3. `mkdir -p ~/data/research-corpus/cache/logs` (the plist's log paths must exist —
   launchd does not create intermediate directories).

## Install

```bash
cp launchd/com.bastiangrubbe.research-corpus.nightly.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.bastiangrubbe.research-corpus.nightly.plist
```

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.bastiangrubbe.research-corpus.nightly.plist
rm ~/Library/LaunchAgents/com.bastiangrubbe.research-corpus.nightly.plist
```

## Verify without waiting for 3am

```bash
launchctl start com.bastiangrubbe.research-corpus.nightly
tail -f ~/data/research-corpus/cache/logs/nightly.log
```

## Linux migration

Per the project's portability rule, this becomes a systemd timer + service unit
that invokes the same `scripts/nightly.sh` unchanged — no logic lives in the plist
itself for exactly this reason.
