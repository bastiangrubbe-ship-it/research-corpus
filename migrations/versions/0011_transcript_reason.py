"""Record why a document has no transcript, instead of leaving it merely absent.

74 documents on this corpus have no transcript. Probing showed 38 are members-only,
22 carry no captions at all, 2 are removed, and only 12 could actually be fetched on a
retry. Without somewhere to record that, the pipeline doctor reports four stages as
permanently incomplete and an operator learns to ignore it.

Nullable and paired with a probe timestamp deliberately: NULL means "never checked",
which is a different claim from any of the reasons, and none of the reasons is
permanent — members-only is a credentials gap, captions can be added later.

Revision ID: 0011_transcript_reason
Revises: 0010_query_log

(Revision id kept short: alembic_version is varchar(32), and the descriptive name this
started with overflowed it — the migration applied and then failed on its own
bookkeeping row.)
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_transcript_reason"
down_revision = "0010_query_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document",
        sa.Column("transcript_unavailable_reason", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "document",
        sa.Column("transcript_probed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Partial: the overwhelming majority of documents have a transcript and never
    # carry a reason, so indexing only the rows that do keeps this small.
    op.create_index(
        "ix_document_transcript_unavailable",
        "document",
        ["transcript_unavailable_reason"],
        postgresql_where=sa.text("transcript_unavailable_reason IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_document_transcript_unavailable", table_name="document")
    op.drop_column("document", "transcript_probed_at")
    op.drop_column("document", "transcript_unavailable_reason")
