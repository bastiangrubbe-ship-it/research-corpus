"""Apply RLS to partitions of chunk_embedding, and keep it applied.

Postgres does not cascade a parent's row-level security to its partitions for queries
issued directly against a partition. Because `ALTER DEFAULT PRIVILEGES` had already
granted the application roles access to every new table in the schema, a partition
created without its own policy was directly readable:

    SELECT * FROM chunk_embedding_default;   -- every tenant's vectors

Policies on the parent are only consulted when the query goes through the parent.

This matters more here than it would elsewhere, because the re-embedding design
creates a *new partition per model version* — so the footgun recurs every time the
embedding model changes, which is exactly when attention is elsewhere. An event
trigger closes it permanently: any future partition of chunk_embedding gets RLS at
creation time without anyone remembering to ask.

Revision ID: 0003_secure_parts
Revises: 0002_rls_embed
"""

from alembic import op

revision = "0003_secure_parts"
down_revision = "0002_rls_embed"
branch_labels = None
depends_on = None

TENANT_EXPR = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    # 1. Close the hole on the partition that already exists.
    op.execute("ALTER TABLE chunk_embedding_default ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE chunk_embedding_default FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON chunk_embedding_default
            USING      (tenant_id = {TENANT_EXPR})
            WITH CHECK (tenant_id = {TENANT_EXPR})
        """
    )

    # 2. Make it automatic for every partition created from here on.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION corpus_secure_new_partitions()
        RETURNS event_trigger LANGUAGE plpgsql AS $fn$
        DECLARE
            obj    record;
            parent text;
        BEGIN
            FOR obj IN SELECT * FROM pg_event_trigger_ddl_commands()
                        WHERE command_tag = 'CREATE TABLE'
            LOOP
                SELECT c.relname INTO parent
                FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhparent
                WHERE i.inhrelid = obj.objid;

                IF parent = 'chunk_embedding' THEN
                    EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY',
                                   obj.object_identity);
                    EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY',
                                   obj.object_identity);
                    EXECUTE format(
                        'CREATE POLICY tenant_isolation ON %s '
                        'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
                        'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)',
                        obj.object_identity);
                    RAISE NOTICE 'RLS applied to new chunk_embedding partition %',
                                 obj.object_identity;
                END IF;
            END LOOP;
        END;
        $fn$
        """
    )
    op.execute(
        """
        CREATE EVENT TRIGGER corpus_secure_partitions
            ON ddl_command_end WHEN TAG IN ('CREATE TABLE')
            EXECUTE FUNCTION corpus_secure_new_partitions()
        """
    )


def downgrade() -> None:
    op.execute("DROP EVENT TRIGGER IF EXISTS corpus_secure_partitions")
    op.execute("DROP FUNCTION IF EXISTS corpus_secure_new_partitions()")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON chunk_embedding_default")
    op.execute("ALTER TABLE chunk_embedding_default NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE chunk_embedding_default DISABLE ROW LEVEL SECURITY")
