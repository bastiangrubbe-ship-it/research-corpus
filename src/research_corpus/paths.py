"""Canonical paths for this project.

Every path in the project derives from PROJECT_DATA_DIR, which direnv exports from
.envrc. Nothing here assumes a home directory, a machine, or a location relative to
the repo — that is what lets the same code run against ~/data/... on a laptop and
/srv/data/... on a server.
"""

import os
from pathlib import Path

# Deliberately os.environ[...] and not .get(..., "./data"). A missing variable should
# fail immediately and loudly. A default would silently write data into the repo, and
# nobody would notice until the repo was cloned somewhere else.
DATA_DIR = Path(os.environ["PROJECT_DATA_DIR"])

#: Raw, immutable source responses. Append-only — never edited in place.
BRONZE = DATA_DIR / "bronze"

#: Derived and disposable. `rm -rf` on this must always be safe.
CACHE = DATA_DIR / "cache"

#: Database files and container state. This is the part that needs backups.
VOLUMES = DATA_DIR / "volumes"


def ensure_dirs() -> None:
    """Create the data subdirectories if they do not exist. Safe to call repeatedly."""
    for path in (BRONZE, CACHE, VOLUMES):
        path.mkdir(parents=True, exist_ok=True)
