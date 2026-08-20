"""The one test worth scaffolding: that data never resolves inside the repo.

If this fails, something has reintroduced a repo-relative default path, which is the
failure mode the whole ~/Projects and ~/data split exists to prevent.
"""

from pathlib import Path

from corpus import paths


def test_data_dir_is_outside_the_repo() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    assert repo_root not in paths.DATA_DIR.resolve().parents
    assert paths.DATA_DIR.resolve() != repo_root


def test_subdirectories_derive_from_data_dir() -> None:
    for path in (paths.BRONZE, paths.CACHE, paths.VOLUMES):
        assert path.parent == paths.DATA_DIR
