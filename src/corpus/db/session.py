"""Engine and session management.

The tenant session parameter is set here and nowhere else. Two rules make this safe,
and both are easy to get wrong:

1. `set_config(..., is_local => true)` rather than a bare `SET`. A non-local `SET`
   persists on the pooled connection after the transaction ends, so the next borrower
   of that connection inherits the previous tenant. That is a security bug, not a
   correctness one, and it is the classic way RLS gets defeated in a pooled
   application.

2. The value is bound as a parameter, never interpolated. `SET` cannot take a
   placeholder, which is exactly why `set_config()` is used instead.

The tenant is resolved from configuration. It is never taken from an MCP tool
argument, a model-generated value, or anything else that crosses a trust boundary.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from corpus.config import get_settings

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            str(settings.database_url),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            future=True,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionFactory


@contextmanager
def tenant_session(tenant_id: uuid.UUID | str) -> Iterator[Session]:
    """Open a transaction with `app.tenant_id` bound for its lifetime.

    Every read and write in the application goes through this. A session opened any
    other way has no tenant set, and RLS policies will correctly return zero rows.
    """
    factory = get_session_factory()
    session = factory()
    try:
        session.begin()
        session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def untenanted_session() -> Iterator[Session]:
    """A session with no tenant parameter set.

    Deliberately provided so tests can assert that policies fail closed. Application
    code must not use this: with `app.tenant_id` unset every tenant-scoped table
    returns zero rows.
    """
    factory = get_session_factory()
    session = factory()
    try:
        session.begin()
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Drop cached engine/factory. Used by tests that switch database roles."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
