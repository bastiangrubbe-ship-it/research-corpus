# Scheduling

`com.bastiangrubbe.research-corpus.nightly.plist` runs [scripts/nightly.sh](../scripts/nightly.sh)
once a day: ingest, then extract entities from whatever's new. It is not installed
automatically — installing it turns this from "a script you run by hand" into a
standing job that runs unattended every night, spending Supadata credits and Claude
usage on its own, so that's a deliberate step you take, not something done for you.

## Before installing

1. Install the CLI: `npm install -g @anthropic-ai/claude-code` (the plist's `PATH`
   already points at `/opt/homebrew/bin`, where npm links it on this machine).
2. **Log in.** A fresh install is unauthenticated — `claude -p` fails with
   `Not logged in · Please run /login` until this is done, and it has to happen
   in your own terminal (an OAuth consent flow tied to your account, not
   something that can run unattended). Then generate the long-lived token the
   launchd job actually uses and add it to `.env`:
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
   launchd does not create intermediate directories; already done on this machine).

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
