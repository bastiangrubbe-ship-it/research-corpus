"""Unique constraint on ingest_state(tenant_id, source_id).

Needed as an ON CONFLICT target for the upsert in ingest/pipelines.py — the table
otherwise has no way to know which existing row a re-check of the same source should
update rather than duplicate.

Revision ID: 0006_ingest_state_unique
Revises: 0005_source_domain
"""

from alembic import op

revision = "0006_ingest_state_unique"
down_revision = "0005_source_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_ingest_state_tenant_source", "ingest_state", ["tenant_id", "source_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_ingest_state_tenant_source", "ingest_state", type_="unique")
