"""Record why a document's metadata could not be fetched, instead of leaving it absent.

The same argument as 0011, one column over. `flows/backfill_metadata.py` repairs
documents ingested with `--metadata-source skip`, and when a fetch fails there is
nowhere to record why — so a throttled request and a members-only video both leave
`published_at IS NULL` and are indistinguishable on the next run.

That is not hypothetical. On 2026-09-02 a backfill pass reported "595 unrepairable"
when five of six sampled documents fetched fine on retry: the run had simply been
throttled after ~1,200 sequential yt-dlp calls, and every transient refusal was
recorded as permanent absence. Nothing in the schema could tell those apart.

Reuses `TranscriptUnavailableReason` rather than defining a parallel enum — the causes
are the same causes (members-only, removed, private) and its own docstring already
carries the caveat that matters: none of them is permanent, members-only is a
credentials gap, and every value should be paired with a probe timestamp and re-probed
rather than treated as settled.

Revision ID: 0013_metadata_reason
Revises: 0012_domain_marketing
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_metadata_reason"
down_revision = "0012_domain_marketing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document",
        sa.Column("metadata_unavailable_reason", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "document",
        sa.Column("metadata_probed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Partial, for the same reason as ix_document_transcript_unavailable: almost every
    # document has metadata and never carries a reason.
    op.create_index(
        "ix_document_metadata_unavailable",
        "document",
        ["metadata_unavailable_reason"],
        postgresql_where=sa.text("metadata_unavailable_reason IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_document_metadata_unavailable", table_name="document")
    op.drop_column("document", "metadata_probed_at")
    op.drop_column("document", "metadata_unavailable_reason")
