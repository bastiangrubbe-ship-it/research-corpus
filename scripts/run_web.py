#!/usr/bin/env python
"""Run the dashboard's backend API.

    uv run python scripts/run_web.py

Bound to 127.0.0.1 only — this is a local, single-operator dashboard, never meant
to be reachable from the network.
"""

from __future__ import annotations

import uvicorn


def main() -> int:
    uvicorn.run("corpus.web.app:app", host="127.0.0.1", port=8420, reload=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
