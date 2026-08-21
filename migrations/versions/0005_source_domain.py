"""Add domain column to source.

Orthogonal to authority_tier: what a source is about, not how much to trust it.
Without this, entrepreneurship and personal-development content shares term-velocity
and saturation counts with AI-vendor content — "mentioned 200 times" becomes
meaningless once it can mean either a tool name or a self-help phrase. Analytics
filter by domain by default; cross-domain queries opt in explicitly.

Revision ID: 0005_source_domain
Revises: 0004_server_defaults
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_source_domain"
down_revision = "0004_server_defaults"
branch_labels = None
depends_on = None

DOMAIN_VALUES = (
    "ai_research",
    "ai_automation",
    "entrepreneurship",
    "personal_development",
    "regulatory",
    "unknown",
)


def upgrade() -> None:
    # op.add_column does not implicitly CREATE TYPE the way table creation does —
    # the enum type has to be created explicitly first, or this fails with
    # "type domain does not exist" against a completely accurate-looking statement.
    domain_enum = sa.Enum(*DOMAIN_VALUES, name="domain")
    domain_enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "source",
        sa.Column("domain", domain_enum, server_default="unknown", nullable=False),
    )
    op.create_index("ix_source_domain", "source", ["domain"])


def downgrade() -> None:
    op.drop_index("ix_source_domain", table_name="source")
    op.drop_column("source", "domain")
    # Autogenerate never emits this. Without it, downgrade base -> upgrade head
    # fails with "type domain already exists" the first time someone rebuilds.
    op.execute("DROP TYPE IF EXISTS domain CASCADE")
