#!/bin/bash
# Three roles. The separation is the point: neither application role holds BYPASSRLS,
# so a bug in session handling fails closed rather than quietly returning every
# tenant's rows.
#
#   $POSTGRES_USER  owns the schema and runs DDL (Alembic). Superuser, so it bypasses
#                   RLS unconditionally — isolation tests must NOT use it.
#   corpus_app      read/write through RLS policies
#   corpus_ingest   read/write through RLS policies (separate role for auditability)
#
# A .sh file is used rather than .sql because the entrypoint exposes the environment
# to shell scripts; psql has no access to these variables.

set -euo pipefail

: "${CORPUS_APP_PASSWORD:?CORPUS_APP_PASSWORD must be set}"
: "${CORPUS_INGEST_PASSWORD:?CORPUS_INGEST_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    CREATE ROLE corpus_app    LOGIN PASSWORD '${CORPUS_APP_PASSWORD}'    NOBYPASSRLS;
    CREATE ROLE corpus_ingest LOGIN PASSWORD '${CORPUS_INGEST_PASSWORD}' NOBYPASSRLS;

    GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO corpus_app, corpus_ingest;
    GRANT USAGE  ON SCHEMA public              TO corpus_app, corpus_ingest;

    -- Tables created later by Alembic inherit these grants.
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO corpus_app, corpus_ingest;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT USAGE, SELECT ON SEQUENCES TO corpus_app, corpus_ingest;
SQL

echo "roles created: corpus_app, corpus_ingest (both NOBYPASSRLS)"
