"""Query log: what was asked of this corpus, and how well it answered.

Nothing else records that a question was asked and answered badly. `corpus_coverage`
grades a topic on demand, but the verdict evaporates with the response — so "this
corpus graded `thin` on robotics fourteen times" was a sourcing decision nobody could
make. This table makes coverage failures accumulate instead of vanishing.

The most sensitive table in the schema: the transcripts are public, these rows are what
the operator is investigating. RLS-covered like everything else, and writes are
disabled by CORPUS_LOG_QUERIES=false.

Revision ID: 0010_query_log
Revises: 0009_entity_extraction_run
"""

import sqlalchemy as sa
from alembic import op

from corpus.db.rls import POLICY_ROLES, enable_rls_sql

revision = "0010_query_log"
down_revision = "0009_entity_extraction_run"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "query_log",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tool", sa.String(length=32), nullable=False),
        sa.Column("surface", sa.String(length=16), nullable=False),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("domain", sa.String(length=32), nullable=True),
        sa.Column("result_count", sa.Integer(), nullable=True),
        sa.Column("top_document_ids", sa.ARRAY(sa.UUID()), nullable=True),
        sa.Column("coverage_grade", sa.String(length=16), nullable=True),
        sa.Column("indexed_documents", sa.Integer(), nullable=True),
        sa.Column("total_documents", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("answered_well", sa.Boolean(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_query_log_tenant_id", "query_log", ["tenant_id"])
    # The two access patterns: "recent activity" and "weak coverage by topic".
    op.create_index("ix_query_log_created_at", "query_log", ["created_at"])
    op.create_index(
        "ix_query_log_coverage_grade",
        "query_log",
        ["coverage_grade"],
        postgresql_where=sa.text("coverage_grade IS NOT NULL"),
    )

    for stmt in enable_rls_sql("query_log"):
        op.execute(stmt)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON query_log TO {', '.join(POLICY_ROLES)}")


def downgrade() -> None:
    op.drop_index("ix_query_log_coverage_grade", table_name="query_log")
    op.drop_index("ix_query_log_created_at", table_name="query_log")
    op.drop_index("ix_query_log_tenant_id", table_name="query_log")
    op.drop_table("query_log")
