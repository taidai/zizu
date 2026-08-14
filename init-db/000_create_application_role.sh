#!/bin/sh
# Create the login role used by the web process before schema SQL is loaded.
# PostgreSQL executes this only when a new data directory is initialized.
set -eu

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${ZIZU_APP_DB_USER:?ZIZU_APP_DB_USER is required}"
: "${ZIZU_APP_DB_PASSWORD:?ZIZU_APP_DB_PASSWORD is required}"

if ! printf '%s\n' "$ZIZU_APP_DB_USER" | grep -Eq '^[a-z_][a-z0-9_]{0,62}$'; then
  echo "ZIZU_APP_DB_USER must be a lowercase PostgreSQL role name" >&2
  exit 1
fi

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=app_user="$ZIZU_APP_DB_USER" \
  --set=app_password="$ZIZU_APP_DB_PASSWORD" <<'SQL'
SELECT format(
    'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
    :'app_user', :'app_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user')
\gexec

SELECT format(
    'ALTER ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
    :'app_user', :'app_password'
)
\gexec
SQL
