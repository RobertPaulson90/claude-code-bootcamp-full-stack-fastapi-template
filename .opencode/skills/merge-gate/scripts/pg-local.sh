#!/usr/bin/env bash
# Throwaway PostgreSQL for the backend tests when Docker can't pull images.
# Uses the binaries bundled in the `pgserver` wheel, copied once to
# ~/.local/share/land-pg/pginstall. Trust auth on localhost:5432, database `app`,
# so the repo's .env DATABASE_URL works unchanged (its password is ignored).
#
#   pg-local.sh start   # fresh cluster, every run starts empty
#   pg-local.sh stop
set -euo pipefail

ROOT="${LAND_PG_ROOT:-$HOME/.local/share/land-pg}"
# The binaries find their shared libraries at ../../../pgserver.libs, so keep the
# wheel's layout: $ROOT/pkg/pgserver/pginstall next to $ROOT/pkg/pgserver.libs.
INSTALL="$ROOT/pkg/pgserver/pginstall"
DATA="$ROOT/data"
PORT="${LAND_PG_PORT:-5432}"

ensure_install() {
  [[ -x "$INSTALL/bin/postgres" ]] && return
  mkdir -p "$ROOT/pkg"
  local site
  site=$(cd /tmp && uv run --no-project --python 3.12 --with pgserver python -c \
    "import pgserver, os; print(os.path.dirname(os.path.dirname(pgserver.__file__)))")
  cp -r "$site/pgserver" "$site/pgserver.libs" "$ROOT/pkg/"
  # The wheel has no contrib modules. The migrations only need uuid_generate_v4(),
  # which core PostgreSQL provides as gen_random_uuid().
  local ext="$INSTALL/share/postgresql/extension"
  cat > "$ext/uuid-ossp.control" <<'EOF'
comment = 'uuid_generate_v4() shim over gen_random_uuid()'
default_version = '1.1'
relocatable = true
EOF
  cat > "$ext/uuid-ossp--1.1.sql" <<'EOF'
CREATE FUNCTION uuid_generate_v4() RETURNS uuid AS 'SELECT gen_random_uuid()' LANGUAGE SQL VOLATILE;
EOF
}

case "${1:-}" in
  start)
    ensure_install
    "$INSTALL/bin/pg_ctl" -D "$DATA" -m fast stop >/dev/null 2>&1 || true
    rm -rf "$DATA"
    "$INSTALL/bin/initdb" -D "$DATA" -U postgres --auth=trust -E UTF8 >/dev/null
    "$INSTALL/bin/pg_ctl" -D "$DATA" -l "$ROOT/postgres.log" -w \
      -o "-p $PORT -k /tmp -c listen_addresses=localhost" start >/dev/null
    "$INSTALL/bin/createdb" -h localhost -p "$PORT" -U postgres app
    echo "postgres up on localhost:$PORT (database app)"
    ;;
  stop)
    [[ -d "$DATA" ]] && "$INSTALL/bin/pg_ctl" -D "$DATA" -m fast stop >/dev/null 2>&1 || true
    echo "postgres stopped"
    ;;
  *)
    echo "usage: $0 start|stop" >&2
    exit 2
    ;;
esac
