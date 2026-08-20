"""Row-level security policy generation.

Policies are emitted as explicit DDL rather than hidden behind an ORM abstraction.
This is the authorization boundary; it should be readable as SQL in the migration.

Two details that decide whether this works:

* `FORCE ROW LEVEL SECURITY` — without it the table owner bypasses its own policies,
  and an isolation test run as the owner passes for the wrong reason.
* `NULLIF(current_setting('app.tenant_id', true), '')` — the `true` makes a missing
  parameter return NULL instead of raising, and NULLIF turns the empty string into
  NULL too. `tenant_id = NULL` is never true, so an unset parameter yields zero rows.
  Fail closed, quietly.
"""

from __future__ import annotations

TENANT_EXPR = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"

#: Roles that read and write through policies. Neither holds BYPASSRLS.
POLICY_ROLES = ("corpus_app", "corpus_ingest")


def tenant_scoped_tables() -> list[str]:
    """Tables carrying a tenant_id, derived from the models rather than hand-listed.

    Hand-maintaining this list is how a new table silently ends up without a policy.
    """
    from corpus.db.models import Base, TenantScoped

    names = [
        mapper.class_.__tablename__
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, TenantScoped)
    ]
    return sorted(set(names))


def enable_rls_sql(table: str) -> list[str]:
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"""
        CREATE POLICY tenant_isolation ON {table}
            USING      (tenant_id = {TENANT_EXPR})
            WITH CHECK (tenant_id = {TENANT_EXPR})
        """.strip(),
    ]


def disable_rls_sql(table: str) -> list[str]:
    return [
        f"DROP POLICY IF EXISTS tenant_isolation ON {table}",
        f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY",
    ]


def grant_sql(table: str) -> list[str]:
    roles = ", ".join(POLICY_ROLES)
    return [f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {roles}"]
