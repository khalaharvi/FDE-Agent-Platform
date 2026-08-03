#!/usr/bin/env bash
# Drop and rebuild the whole schema from migrations, then run the smoke test.
# This is the CI gate: migrations must apply cleanly in order on an empty DB.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
usage: rebuild.sh [DB] [--with-demo]

Drops DB, recreates it, applies every migration in order, then runs the
smoke test.

  DB            database to rebuild (default: fde)
  --with-demo   also load db/seed_demo.sql, which leaves a merged commit and
                three pending proposals in the review console. Demo data for
                local evaluation only -- never a production database.
  -h, --help    show this message
EOF
}

DB=""
WITH_DEMO=0
for arg in "$@"; do
  case "$arg" in
    --with-demo) WITH_DEMO=1 ;;
    -h|--help)   usage; exit 0 ;;
    -*)          echo "rebuild.sh: unknown option: $arg" >&2; usage >&2; exit 2 ;;
    *)
      if [ -n "$DB" ]; then
        echo "rebuild.sh: unexpected argument: $arg" >&2; usage >&2; exit 2
      fi
      DB="$arg" ;;
  esac
done
DB="${DB:-fde}"

dropdb --if-exists "$DB"
createdb "$DB"

for f in "$HERE"/0*.sql; do
  echo "==> $(basename "$f")"
  psql -d "$DB" -q -v ON_ERROR_STOP=1 -f "$f"
done

echo "==> smoke test"
psql -d "$DB" -v ON_ERROR_STOP=1 -f "$HERE/tests/smoke_test.sql"

if [ "$WITH_DEMO" -eq 1 ]; then
  echo "==> demo seed (local evaluation data)"
  psql -d "$DB" -v ON_ERROR_STOP=1 -f "$HERE/seed_demo.sql"
fi
