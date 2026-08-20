-- Extensions must exist before any migration runs.
CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector: vector, halfvec, HNSW
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- trigram, for fuzzy entity alias matching
CREATE EXTENSION IF NOT EXISTS btree_gin;   -- composite GIN for tenant+tsvector indexes
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

SELECT extname, extversion FROM pg_extension ORDER BY extname;
