#!/bin/sh
# Runs after schema/migration SQL on a new cluster.  The backend login gets
# ordinary application privileges, except immutable legacy alarm history.
set -eu

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${ZIZU_APP_DB_USER:?ZIZU_APP_DB_USER is required}"

# The entrypoint has already executed every migration_*.sql as the schema
# owner. Record that fact so the production web role only verifies releases;
# it never receives DDL privileges.
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ DEFAULT now()
);
SQL
for migration in /docker-entrypoint-initdb.d/migration_*.sql; do
  [ -f "$migration" ] || continue
  version=$(basename "$migration" | sed -n 's/^migration_\([0-9][0-9]*\).*\.sql$/\1/p')
  [ -n "$version" ] || continue
  psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --set=version="$version" <<'SQL'
INSERT INTO schema_migrations (version) VALUES (:'version') ON CONFLICT (version) DO NOTHING;
SQL
done

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=app_user="$ZIZU_APP_DB_USER" <<'SQL'
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'app_user')
\gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'app_user')
\gexec
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SELECT format('GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public TO %I', :'app_user')
\gexec
SELECT format('GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO %I', :'app_user')
\gexec
SELECT format('REVOKE ALL PRIVILEGES ON TABLE public.t_alarms FROM %I', :'app_user')
\gexec
SELECT format('GRANT SELECT ON TABLE public.t_alarms TO %I', :'app_user')
\gexec
SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLES TO %I', :'app_user')
\gexec
SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO %I', :'app_user')
\gexec
SQL
