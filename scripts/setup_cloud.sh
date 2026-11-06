#!/usr/bin/env bash
# Postgres for agent-sql-bench on a fresh Ubuntu VM (e.g. a Claude Code cloud session),
# without Docker. Idempotent: safe to run again.
#
# Result: Postgres on 127.0.0.1:55440, superuser bench/bench, database `shop` seeded
# from db/seed.sql, pg_stat_statements enabled; Python deps installed with uv.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT=55440
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
as_pg() { if [ "$(id -u)" -eq 0 ]; then runuser -u postgres -- "$@"; else sudo -u postgres "$@"; fi; }

if ! command -v pg_ctlcluster >/dev/null 2>&1; then
  $SUDO apt-get update -y && $SUDO apt-get install -y postgresql postgresql-contrib
fi
V=$(ls /etc/postgresql | sort -V | tail -1)
[ -d "/etc/postgresql/$V/main" ] || $SUDO pg_createcluster "$V" main
CONF=/etc/postgresql/$V/main/postgresql.conf

$SUDO sed -i -E "s/^#?port = [0-9]+/port = $PORT/" "$CONF"
grep -q "^shared_preload_libraries = 'pg_stat_statements'" "$CONF" \
  || echo "shared_preload_libraries = 'pg_stat_statements'" | $SUDO tee -a "$CONF" >/dev/null
grep -q "^pg_stat_statements.track" "$CONF" \
  || echo "pg_stat_statements.track = all" | $SUDO tee -a "$CONF" >/dev/null

$SUDO service postgresql restart 2>/dev/null || $SUDO pg_ctlcluster "$V" main restart
for _ in $(seq 1 30); do as_pg pg_isready -q -p "$PORT" && break; sleep 1; done

as_pg psql -p "$PORT" -tAc "SELECT 1 FROM pg_roles WHERE rolname = 'bench'" | grep -q 1 \
  || as_pg psql -p "$PORT" -qc "CREATE ROLE bench LOGIN SUPERUSER PASSWORD 'bench'"
if ! as_pg psql -p "$PORT" -tAc "SELECT 1 FROM pg_database WHERE datname = 'shop'" | grep -q 1; then
  as_pg createdb -p "$PORT" -O bench shop
  PGPASSWORD=bench psql -h 127.0.0.1 -p "$PORT" -U bench -d shop -q -f db/seed.sql
fi

command -v uv >/dev/null 2>&1 || pip install -q uv 2>/dev/null || pip install -q --break-system-packages uv
uv sync -q
echo "postgres $V ready on 127.0.0.1:$PORT; databases: $(as_pg psql -p "$PORT" -tAc \
  "SELECT string_agg(datname, ', ') FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres'")"
