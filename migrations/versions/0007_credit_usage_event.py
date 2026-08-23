"""Credit usage event log, with RLS.

Supadata has no endpoint reporting credit consumption back to the caller (confirmed
against the live API during step 2 — no billing header, nothing in any response
body). This table is the only durable record that exists; `CreditLedger` is
in-memory only and resets to zero on every process restart. `endpoint` and
`external_id` are kept so a spend spike can be traced to its cause, not just counted.

Revision ID: 0007_credit_usage_event
Revises: 0006_ingest_state_unique
"""

import sqlalchemy as sa
from alembic import op

from corpus.db.rls import POLICY_ROLES, enable_rls_sql

revision = "0007_credit_usage_event"
down_revision = "0006_ingest_state_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "credit_usage_event",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("endpoint", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_credit_usage_event_occurred_at", "credit_usage_event", ["occurred_at"])
    op.create_index("ix_credit_usage_event_provider", "credit_usage_event", ["provider"])
    op.create_index("ix_credit_usage_event_tenant_id", "credit_usage_event", ["tenant_id"])

    for stmt in enable_rls_sql("credit_usage_event"):
        op.execute(stmt)
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON credit_usage_event TO {', '.join(POLICY_ROLES)}"
    )


def downgrade() -> None:
    op.drop_index("ix_credit_usage_event_tenant_id", table_name="credit_usage_event")
    op.drop_index("ix_credit_usage_event_provider", table_name="credit_usage_event")
    op.drop_index("ix_credit_usage_event_occurred_at", table_name="credit_usage_event")
    op.drop_table("credit_usage_event")
