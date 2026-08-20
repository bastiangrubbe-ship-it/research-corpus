"""Alembic environment.

The database URL comes from the application settings (environment only), never from
alembic.ini — that file is committed and must not carry credentials.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from corpus.config import get_settings
from corpus.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", str(get_settings().database_url))

target_metadata = Base.metadata


def _include_object(obj, name, type_, reflected, compare_to):
    # chunk_embedding is a declaratively-partitioned table created in raw DDL.
    # Autogenerate cannot represent PARTITION BY and would try to drop it.
    return not (type_ == "table" and name and name.startswith("chunk_embedding"))


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=_include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=_include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
