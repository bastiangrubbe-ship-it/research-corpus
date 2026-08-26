"""Entity extraction run log, with RLS.

`entity_mention` alone can't record "this document was checked and had zero real
entities" — no mention rows are indistinguishable from "never processed," so
`find_unenriched_documents` was re-queuing zero-entity documents on every single
scheduled run, forever (see docs/DECISIONS.md). This table is the missing completion
marker, decoupled from how many (if any) entities a document actually produced.

Revision ID: 0009_entity_extraction_run
Revises: 0008_domain_general
"""

import sqlalchemy as sa
from alembic import op

from corpus.db.rls import POLICY_ROLES, enable_rls_sql

revision = "0009_entity_extraction_run"
down_revision = "0008_domain_general"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "entity_extraction_run",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("extractor_version", sa.String(length=128), nullable=False),
        sa.Column("mention_count", sa.Integer(), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id", "extractor_version", name="uq_extraction_run_doc_version"
        ),
    )
    op.create_index(
        "ix_entity_extraction_run_document_id", "entity_extraction_run", ["document_id"]
    )
    op.create_index(
        "ix_entity_extraction_run_tenant_id", "entity_extraction_run", ["tenant_id"]
    )

    for stmt in enable_rls_sql("entity_extraction_run"):
        op.execute(stmt)
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON entity_extraction_run TO "
        f"{', '.join(POLICY_ROLES)}"
    )


def downgrade() -> None:
    op.drop_index("ix_entity_extraction_run_tenant_id", table_name="entity_extraction_run")
    op.drop_index("ix_entity_extraction_run_document_id", table_name="entity_extraction_run")
    op.drop_table("entity_extraction_run")
