"""One place that shells out to the Claude Code CLI in headless mode (`claude -p`).

Authenticated by `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) rather than a
metered `ANTHROPIC_API_KEY`: this backs unattended batch jobs on a subscription, not
per-token billing.

This module exists because the call was written twice and the two copies drifted.
`corpus.enrich.entities` learned the hard way that the CLI reports its own failures in
the **stdout** JSON envelope, not on stderr -- a failed call routinely has empty
stderr, so reporting stderr alone yields `claude CLI exited 1:` with nothing after the
colon, which is how a context-limit failure went undiagnosed for a whole session.
That fix landed in entities.py and never reached `corpus.eval.judge`, which had the
identical bug sitting in it unnoticed until synthesis was about to become the third
copy (docs/DECISIONS.md, 2026-08-26).

`--allowedTools ""` is passed on every call and is not an argument callers can
override. Every current caller feeds untrusted external text (transcripts, or model
output derived from them) into the prompt, and none has any legitimate reason to
execute a tool, so the capability is removed rather than assumed unreachable.
"""

from __future__ import annotations

import json
import re
import subprocess

#: Above this input size, callers should escalate to `LARGE_INPUT_MODEL`.
#:
#: The default model is `haiku` (200k tokens, ~800k characters). One transcript in
#: this corpus is 1,023,757 characters (~256k tokens) and does not fit it at all.
#: Opus takes 1M tokens and fits it with room to spare. 200,000 characters covers
#: ~0.76% of documents, so escalation stays bounded while removing the
#: "permanently too large" category entirely.
LARGE_INPUT_CHARS = 200_000
LARGE_INPUT_MODEL = "opus"

#: Escalated inputs are both larger and on a slower model. Measured: the 1M-character
#: transcript took 230s of API time against a 240s default.
LARGE_INPUT_TIMEOUT_MULTIPLIER = 4

_FENCE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL)


class ClaudeCliError(Exception):
    """The CLI could not be run, timed out, or exited non-zero."""


def choose_model(text: str, *, default: str) -> str:
    """`default` for input that fits it, `LARGE_INPUT_MODEL` for input that does not."""
    return LARGE_INPUT_MODEL if len(text) > LARGE_INPUT_CHARS else default


def scale_timeout(text: str, *, base_timeout_s: float) -> float:
    """Timeout scaled for escalated input. Scaled rather than raised globally: for the
    99% of inputs that stay on the small model, a long wait means something is wrong,
    and that signal is worth keeping."""
    return base_timeout_s * (LARGE_INPUT_TIMEOUT_MULTIPLIER if len(text) > LARGE_INPUT_CHARS else 1)


def run_claude_headless(
    prompt: str,
    *,
    model: str,
    timeout_s: float,
    stdin_text: str | None = None,
) -> str:
    """Raw stdout of one `claude -p` call. Raises `ClaudeCliError` on any failure.

    `stdin_text` is how large, untrusted content should be passed -- as data on stdin,
    not concatenated into `prompt`.
    """
    argv = ["claude", "-p", prompt, "--model", model, "--output-format", "json"]
    argv += ["--allowedTools", ""]

    try:
        proc = subprocess.run(
            argv,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCliError(f"claude CLI timed out after {timeout_s}s") from exc
    except FileNotFoundError as exc:
        raise ClaudeCliError("claude CLI not found on PATH") from exc

    if proc.returncode != 0:
        # See module docstring: prefer stderr when it has content, fall back to
        # stdout, which is where the CLI actually puts its failure reason.
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise ClaudeCliError(f"claude CLI exited {proc.returncode}: {detail[:500]}")

    return proc.stdout


def strip_markdown_fence(text: str) -> str:
    """Unwrap a ```json fenced block if the model produced one."""
    match = _FENCE.match(text.strip())
    return match.group(1) if match else text.strip()


def parse_envelope(stdout: str) -> str:
    """The `result` field of the CLI's JSON envelope, fence-stripped.

    Raises `ClaudeCliError` if stdout is not the envelope we expect -- which is a
    different failure from "the model returned the wrong shape", and callers that
    validate a schema should keep the two distinguishable.
    """
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCliError(
            f"claude CLI did not return a JSON envelope: {stdout[:300]!r}"
        ) from exc
    return strip_markdown_fence(envelope.get("result", ""))
