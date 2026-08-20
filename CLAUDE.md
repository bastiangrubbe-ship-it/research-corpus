# research-corpus

<!--
Keep this under 200 lines; shorter is better. It loads into every session for this
project, so anything not universally applicable dilutes the rest.

Onboarding, not a rulebook. Fill in the four sections below and delete this comment.
Do not run /init.
-->

## What this is

<!-- One or two sentences. What does it do, and what does it produce? -->

## Why it exists

<!-- The problem it solves, and what was being done before. This is the part that
     stops a future reader from "simplifying" away something load-bearing. -->

## How to work on it

Data lives at `$PROJECT_DATA_DIR` (`~/data/research-corpus` on this machine), never
in this repo. direnv sets it on `cd`; run `direnv allow` once after cloning.

```bash
uv sync              # install dependencies from uv.lock
uv run pytest        # run the tests
```

<!-- Add anything non-obvious: how to get credentials, which service to start first,
     what a full run costs in time or API quota. -->

## Reference

<!-- Task-specific instructions live in their own file under docs/ and are listed here
     with a one-line description, so they are read only when relevant.

- docs/ingestion.md — how the bronze fetch works and how to re-run it safely
- docs/schema.md — table layouts and what each column means
-->

- `docs/DECISIONS.md` — what was chosen, what was rejected, and why
