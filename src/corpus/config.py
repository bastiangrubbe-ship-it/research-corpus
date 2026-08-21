"""Configuration. Environment only — no path literals, no defaults that point inside the repo.

PROJECT_DATA_DIR is required and intentionally has no default. A default would let the
system silently write data into the git checkout, which is the one failure this layout
exists to prevent. On a Linux server this is set to /srv/data/research-corpus and
nothing else changes.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- paths ------------------------------------------------------------
    project_data_dir: Path = Field(..., alias="PROJECT_DATA_DIR")

    # --- database ---------------------------------------------------------
    database_url: PostgresDsn = Field(..., alias="DATABASE_URL")
    # RLS-bound role. The migrate role is a superuser and bypasses RLS regardless
    # of policies, so anything asserting isolation must connect as this one.
    app_database_url: PostgresDsn | None = Field(None, alias="APP_DATABASE_URL")

    # --- tenancy ----------------------------------------------------------
    # Resolved from config, never from an MCP tool argument or model output.
    tenant_slug: str = Field("default", alias="CORPUS_TENANT_SLUG")

    # --- supadata ---------------------------------------------------------
    supadata_api_key: SecretStr | None = Field(None, alias="SUPADATA_API_KEY")
    supadata_base_url: str = Field("https://api.supadata.ai/v1", alias="SUPADATA_BASE_URL")
    # Plan-dependent and undocumented; both are read from the environment so the
    # fetcher never hardcodes a limit it cannot verify.
    supadata_monthly_credits: int = Field(30_000, alias="SUPADATA_MONTHLY_CREDITS")
    supadata_requests_per_second: float = Field(2.0, alias="SUPADATA_RPS")

    @field_validator("project_data_dir")
    @classmethod
    def _must_be_absolute_and_outside_repo(cls, v: Path) -> Path:
        if not v.is_absolute():
            raise ValueError(f"PROJECT_DATA_DIR must be absolute, got {v!r}")
        repo_root = Path(__file__).resolve().parents[2]
        resolved = v.expanduser().resolve()
        if resolved == repo_root or repo_root in resolved.parents:
            raise ValueError(
                f"PROJECT_DATA_DIR ({resolved}) is inside the repo ({repo_root}). "
                "Data must live outside the git checkout."
            )
        return resolved

    @property
    def has_supadata_key(self) -> bool:
        """An unset key arrives as an empty string, not None, and an empty
        string still produces a well-formed request that returns 401."""
        return bool(self.supadata_api_key and self.supadata_api_key.get_secret_value().strip())

    # --- derived data paths ----------------------------------------------
    @property
    def bronze_dir(self) -> Path:
        """Raw API responses. Write-once, append-only, the rebuild guarantee."""
        return self.project_data_dir / "bronze"

    @property
    def cache_dir(self) -> Path:
        """Derived and disposable. `rm -rf` on this must always be safe."""
        return self.project_data_dir / "cache"

    @property
    def volumes_dir(self) -> Path:
        """Container state. The part that needs backups."""
        return self.project_data_dir / "volumes"

    @property
    def eval_dir(self) -> Path:
        return self.project_data_dir / "eval"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
