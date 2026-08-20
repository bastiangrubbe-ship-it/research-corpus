"""Tenant isolation. Verified with two synthetic tenants before any real data exists.

Every test connects as `corpus_app`. The migration role is a superuser and bypasses
RLS regardless of policy, so these assertions are only meaningful through the
application role.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from tests.integration.conftest import app_session

pytestmark = pytest.mark.integration

TENANT_TABLES = [
    "source",
    "document",
    "transcript_version",
    "chunk",
    "chunk_embedding",
]


def test_tenant_sees_only_its_own_rows(app_engine: Engine, tenants, seeded) -> None:
    s = app_session(app_engine, tenants["a"])
    try:
        rows = s.execute(text("SELECT id, tenant_id FROM document")).all()
        assert len(rows) == 1, "tenant A should see exactly its own document"
        assert rows[0].tenant_id == tenants["a"]
        assert rows[0].id == seeded["a"]["document"]
    finally:
        s.rollback()
        s.close()


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_no_cross_tenant_reads_on_any_table(app_engine: Engine, tenants, seeded, table) -> None:
    s = app_session(app_engine, tenants["a"])
    try:
        leaked = s.execute(
            text(f"SELECT count(*) FROM {table} WHERE tenant_id = :other"),
            {"other": tenants["b"]},
        ).scalar_one()
        assert leaked == 0, f"{table} leaked tenant B rows to tenant A"
    finally:
        s.rollback()
        s.close()


def test_partition_cannot_be_queried_around_the_parent(app_engine: Engine, tenants, seeded) -> None:
    """Postgres does not cascade a parent's policies to a directly-queried partition.

    Without its own policy, `SELECT * FROM chunk_embedding_default` would return every
    tenant's vectors — the application roles hold privileges on it via
    ALTER DEFAULT PRIVILEGES.
    """
    s = app_session(app_engine, tenants["a"])
    try:
        total = s.execute(text("SELECT count(*) FROM chunk_embedding_default")).scalar_one()
        assert total == 1, "querying the partition directly bypassed tenant isolation"
    finally:
        s.rollback()
        s.close()


def test_unset_tenant_returns_nothing_and_does_not_raise(app_engine: Engine, seeded) -> None:
    """Fail closed, quietly. `current_setting(..., true)` + NULLIF is what makes a
    missing session parameter yield zero rows rather than an error."""
    s = app_session(app_engine, None)
    try:
        for table in TENANT_TABLES:
            count = s.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            assert count == 0, f"{table} returned rows with app.tenant_id unset"
    finally:
        s.rollback()
        s.close()


def test_cannot_update_another_tenants_row(app_engine: Engine, tenants, seeded) -> None:
    s = app_session(app_engine, tenants["a"])
    try:
        result = s.execute(
            text("UPDATE document SET title = 'hijacked' WHERE id = :id"),
            {"id": seeded["b"]["document"]},
        )
        assert result.rowcount == 0
        s.commit()
    finally:
        s.close()

    s2 = app_session(app_engine, tenants["b"])
    try:
        title = s2.execute(
            text("SELECT title FROM document WHERE id = :id"), {"id": seeded["b"]["document"]}
        ).scalar_one()
        assert title == "doc b"
    finally:
        s2.rollback()
        s2.close()


def test_cannot_delete_another_tenants_row(app_engine: Engine, tenants, seeded) -> None:
    s = app_session(app_engine, tenants["a"])
    try:
        result = s.execute(
            text("DELETE FROM document WHERE id = :id"), {"id": seeded["b"]["document"]}
        )
        assert result.rowcount == 0
        s.commit()
    finally:
        s.close()


def test_with_check_blocks_inserting_under_another_tenant(
    app_engine: Engine, tenants, seeded
) -> None:
    """USING governs what you can see; WITH CHECK governs what you can write.

    Without WITH CHECK a tenant could insert rows it would then be unable to read —
    a silent write into another tenant's data.
    """
    s = app_session(app_engine, tenants["a"])
    try:
        with pytest.raises(ProgrammingError) as exc:
            s.execute(
                text(
                    "INSERT INTO source (id, tenant_id, kind, external_id) "
                    "VALUES (:id, :t, 'rss', 'smuggled')"
                ),
                {"id": uuid.uuid4(), "t": tenants["b"]},
            )
        assert "row-level security" in str(exc.value).lower()
    finally:
        s.rollback()
        s.close()


def test_app_role_does_not_bypass_rls(app_engine: Engine) -> None:
    """The guard against someone granting BYPASSRLS to make a failing test pass."""
    s = app_session(app_engine, None)
    try:
        row = s.execute(
            text("SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = current_user")
        ).one()
        assert row.rolbypassrls is False, (
            "app role holds BYPASSRLS; isolation tests are meaningless"
        )
        assert row.rolsuper is False, "app role is a superuser; isolation tests are meaningless"
    finally:
        s.rollback()
        s.close()


def test_every_tenant_scoped_table_has_rls_enabled_and_forced(app_engine: Engine) -> None:
    """Catches a new table being added without a policy — including new partitions.

    FORCE matters: without it the table owner is exempt from its own policies.
    """
    from corpus.db.rls import tenant_scoped_tables

    expected = {*tenant_scoped_tables(), "chunk_embedding", "chunk_embedding_default"}
    s = app_session(app_engine, None)
    try:
        rows = s.execute(
            text(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                "  (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind IN ('r','p')"
            )
        ).all()
        state = {r.relname: r for r in rows}
        for table in sorted(expected):
            assert table in state, f"{table} missing from the database"
            assert state[table].relrowsecurity, f"{table} has RLS disabled"
            assert state[table].relforcerowsecurity, f"{table} does not FORCE RLS"
            assert state[table].policies >= 1, f"{table} has no policy"
    finally:
        s.rollback()
        s.close()
