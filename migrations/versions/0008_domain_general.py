"""Add 'general' to the domain enum.

seeds/youtube_channels.yaml gained 14 rows under `domain: general` (2026-08-24 —
education/documentary/business content that doesn't fit the four analytical
domains, kept on request rather than dropped as out_of_scope; see docs/DECISIONS.md
and docs/SUPADATA.md history). Nobody updated the DB enum to match at the time,
which surfaced as a hard crash the first time the backfill actually reached one
of those rows: `ValueError: 'general' is not a valid Domain`.

Revision ID: 0008_domain_general
Revises: 0007_credit_usage_event
"""

from alembic import op

revision = "0008_domain_general"
down_revision = "0007_credit_usage_event"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run in the same transaction as a statement
    # that uses the new value, but adding it alone is fine inside Alembic's
    # per-migration transaction.
    op.execute("ALTER TYPE domain ADD VALUE IF NOT EXISTS 'general'")


def downgrade() -> None:
    # Postgres has no DROP VALUE for enums. Downgrading would require rebuilding
    # the type from scratch (rename old, create new without 'general', migrate
    # column, drop old) and is not worth it for a value that's additive and
    # harmless to leave behind — matches this project's practice of not writing
    # exotic destructive downgrades for additive changes.
    pass
