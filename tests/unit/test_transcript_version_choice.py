"""Reading a document and indexing it need different transcript versions.

Conflating them caused two defects on this corpus. Both stages resolved "newest by
created_at", which is correct until restoration writes a newer version and then
silently wrong forever after — the index gets rebuilt from measurably worse text and
nothing fails.

These are structural assertions on the queries. They do not need a database: what
matters is that the indexing query filters on provider at all, and that the reading
query does not.
"""

from __future__ import annotations

import uuid

from corpus.db.enums import TranscriptProvider
from corpus.db.transcript_versions import index_versions, latest_versions

TENANT = uuid.uuid4()


def sql(subquery) -> str:
    """Compiled against the Postgres dialect specifically. `DISTINCT ON` is a Postgres
    extension and renders as a plain `DISTINCT` under the default dialect, which would
    make the "newest per document" assertion below pass vacuously."""
    from sqlalchemy.dialects import postgresql

    return str(
        subquery.element.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


class TestIndexVersionsExcludesDerived:
    def test_query_filters_on_provider(self):
        assert "provider" in sql(index_versions(TENANT)).lower()

    def test_it_names_the_derived_providers(self):
        from corpus.db.transcript_versions import DERIVED_PROVIDERS

        compiled = sql(index_versions(TENANT))
        for provider in DERIVED_PROVIDERS:
            assert provider.value in compiled

    def test_it_is_an_exclusion_not_an_allowlist(self):
        """`NOT IN (derived)`, never `IN (fetched)`. A future transcript source must
        be indexable by default rather than silently vanishing from the index because
        nobody updated a list."""
        compiled = sql(index_versions(TENANT)).upper()
        assert "NOT IN" in compiled or "!=" in compiled


class TestLatestVersionsKeepsRestored:
    def test_reading_query_does_not_filter_provider(self):
        """Synthesis quotes and the eval judge want restored text — it is the
        readable one. Filtering here would undo the only benefit restoration has."""
        assert TranscriptProvider.RESTORED.value not in sql(latest_versions(TENANT))


class TestBothPickNewestPerDocument:
    def test_distinct_on_document(self):
        for sub in (latest_versions(TENANT), index_versions(TENANT)):
            compiled = sql(sub).upper()
            assert "DISTINCT ON" in compiled
            assert "CREATED_AT DESC" in compiled


class TestConsumersUseTheRightOne:
    """The whole point is that the two indexing stages moved and the reading stages
    did not. Asserted against the modules so a future edit that flips one is caught."""

    def test_summaries_pin_to_index_versions(self):
        import inspect

        from corpus.enrich import relevance_gate

        src = inspect.getsource(relevance_gate._latest_transcript_versions)
        assert "index_versions" in src

    def test_chunking_pins_to_index_versions(self):
        import inspect

        from corpus.chunking import backfill

        src = inspect.getsource(backfill.find_unchunked_transcript_versions)
        assert "index_versions" in src

    def test_entity_extraction_still_reads_latest(self):
        """Entity extraction hands text to a model to read, so restored is right
        there — and restore.py's own docstring notes extraction does fine either way."""
        import inspect

        from corpus.enrich import entities

        src = inspect.getsource(entities)
        assert "index_versions" not in src
