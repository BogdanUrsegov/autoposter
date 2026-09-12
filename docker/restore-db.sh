#!/bin/sh
set -eu

DUMP_FILE="/backup/clickbot_dump.dump"

if [ ! -f "$DUMP_FILE" ]; then
  echo "[db_restore] Dump not found: $DUMP_FILE"
  echo "[db_restore] Skipping restore; PostgreSQL will be initialized normally."
  exit 0
fi

echo "[db_restore] Checking whether the application database is already initialized..."

USERS_TABLE="$(psql -tAc "SELECT to_regclass('public.users');" | tr -d '[:space:]')"

if [ "$USERS_TABLE" = "users" ]; then
  echo "[db_restore] Existing users table found. Restore skipped."
  exit 0
fi

echo "[db_restore] Empty application database detected. Restoring $DUMP_FILE ..."
pg_restore \
  --no-owner \
  --clean \
  --if-exists \
  --exit-on-error \
  -d "$PGDATABASE" \
  "$DUMP_FILE"

echo "[db_restore] Restore completed successfully."
