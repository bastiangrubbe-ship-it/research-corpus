# pgvector 0.8.x on Postgres 17. This image publishes a native linux/arm64 manifest,
# so it runs without emulation on Apple Silicon and unchanged on an x86 Linux server.
# Pinned by digest-able tag rather than :latest so the Mac and the server get the
# same binary.
FROM pgvector/pgvector:pg17

# initdb scripts run once, only when the data directory is empty. They create the
# extensions and the three roles that RLS depends on.
COPY initdb/ /docker-entrypoint-initdb.d/
