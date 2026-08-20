"""Server-side defaults for enum columns that carry one.

SQLAlchemy's `default=` is applied in Python, so it only exists for callers going
through the ORM. Anything writing raw SQL — fixtures, DLT's own inserts, psql — hits
a NOT NULL violation instead. A server default makes the behaviour a property of the
schema rather than of one client library.

Revision ID: 0004_server_defaults
Revises: 0003_secure_parts
"""

from alembic import op

revision = "0004_server_defaults"
down_revision = "0003_secure_parts"
branch_labels = None
depends_on = None

DEFAULTS = [
    ("source", "authority_tier", "unknown", "authority_tier"),
    ("source", "status", "active", "source_status"),
    ("document", "published_at_precision", "unknown", "date_precision"),
    ("document", "status", "pending", "document_status"),
    ("transcript_version", "provenance_confidence", "unknown", "provenance_confidence"),
    ("speaker", "attribution_method", "unknown", "attribution_method"),
]


def upgrade() -> None:
    for table, column, value, enum_type in DEFAULTS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT '{value}'::{enum_type}")


def downgrade() -> None:
    for table, column, _, _ in DEFAULTS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT")
