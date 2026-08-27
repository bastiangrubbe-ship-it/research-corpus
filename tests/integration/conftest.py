"""Fixtures for integration tests.

Everything here connects as `corpus_app`, the RLS-bound role. The migration role is a
Postgres superuser, and superusers bypass row-level security unconditionally — running
isolation assertions as that role would produce green tests that prove nothing.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from corpus.config import get_settings


def _require(url: str | None, name: str) -> str:
    if not url:
        pytest.skip(f"{name} not set; integration tests need a running Postgres")
    return url


@pytest.fixture(scope="session")
def migrate_engine() -> Iterator[Engine]:
    """Superuser connection. Used only to create and tear down fixture data."""
    engine = create_engine(str(get_settings().database_url), future=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def app_engine() -> Iterator[Engine]:
    """RLS-bound connection. Every isolation assertion runs through this."""
    settings = get_settings()
    url = _require(
        str(settings.app_database_url) if settings.app_database_url else None,
        "APP_DATABASE_URL",
    )
    engine = create_engine(url, future=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def tenants(migrate_engine: Engine) -> Iterator[dict[str, uuid.UUID]]:
    """Two synthetic tenants, as the build plan requires before any real data."""
    a, b = uuid.uuid4(), uuid.uuid4()
    suffix = os.urandom(4).hex()
    with migrate_engine.begin() as conn:
        for tid, slug in ((a, f"test-a-{suffix}"), (b, f"test-b-{suffix}")):
            conn.execute(
                text("INSERT INTO tenant (id, slug, name) VALUES (:id, :slug, :name)"),
                {"id": tid, "slug": slug, "name": slug},
            )
    yield {"a": a, "b": b}
    with migrate_engine.begin() as conn:
        conn.execute(text("DELETE FROM tenant WHERE id = ANY(:ids)"), {"ids": [a, b]})


@pytest.fixture
def seeded(migrate_engine: Engine, tenants: dict[str, uuid.UUID]) -> Iterator[dict]:
    """One source + document + chunk + embedding row per tenant.

    Seeded as the superuser deliberately: the fixture needs to write rows for *both*
    tenants, which is precisely what the policies forbid the app role from doing.
    """
    ids: dict[str, dict[str, uuid.UUID]] = {}
    with migrate_engine.begin() as conn:
        for key, tid in tenants.items():
            src, doc, chunk = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO source (id, tenant_id, kind, external_id, title) "
                    "VALUES (:id, :t, 'youtube_channel', :ext, :title)"
                ),
                {"id": src, "t": tid, "ext": f"chan-{key}", "title": f"channel {key}"},
            )
            conn.execute(
                text(
                    "INSERT INTO document (id, tenant_id, source_id, external_id, title) "
                    "VALUES (:id, :t, :src, :ext, :title)"
                ),
                {"id": doc, "t": tid, "src": src, "ext": f"vid-{key}", "title": f"doc {key}"},
            )
            tv = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO transcript_version "
                    "(id, tenant_id, document_id, provider, is_auto_generated, "
                    " provenance_confidence, lang) "
                    "VALUES (:id, :t, :doc, 'supadata', NULL, 'unknown', 'en')"
                ),
                {"id": tv, "t": tid, "doc": doc},
            )
            conn.execute(
                text(
                    "INSERT INTO chunk (id, tenant_id, document_id, transcript_version_id, "
                    " idx, text) VALUES (:id, :t, :doc, :tv, 0, :txt)"
                ),
                {"id": chunk, "t": tid, "doc": doc, "tv": tv, "txt": f"chunk text {key}"},
            )
            # query_log carries what the operator searched for, which is more sensitive
            # than any transcript in the corpus. Seeded per tenant so the isolation
            # test asserts against real rows rather than passing on an empty table.
            conn.execute(
                text(
                    "INSERT INTO query_log (tenant_id, tool, surface, query_text, "
                    " coverage_grade) VALUES (:t, 'coverage', 'mcp', :q, 'thin')"
                ),
                {"t": tid, "q": f"confidential query {key}"},
            )
            conn.execute(
                text(
                    "INSERT INTO chunk_embedding (chunk_id, tenant_id, model_version, embedding) "
                    "VALUES (:c, :t, 'test-model', :emb)"
                ),
                {"c": chunk, "t": tid, "emb": "[" + ",".join(["0.1"] * 768) + "]"},
            )
            conn.execute(
                text(
                    "INSERT INTO entity_extraction_run "
                    "(id, tenant_id, document_id, extractor_version, mention_count) "
                    "VALUES (:id, :t, :doc, 'test-version', 0)"
                ),
                {"id": uuid.uuid4(), "t": tid, "doc": doc},
            )
            ids[key] = {"source": src, "document": doc, "chunk": chunk, "transcript": tv}
    yield ids
    with migrate_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM source WHERE id = ANY(:ids)"),
            {"ids": [v["source"] for v in ids.values()]},
        )


def app_session(engine: Engine, tenant_id: uuid.UUID | None) -> Session:
    """A session as corpus_app with `app.tenant_id` bound, or deliberately unbound."""
    session = sessionmaker(bind=engine, future=True)()
    session.begin()
    if tenant_id is not None:
        session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
    return session
