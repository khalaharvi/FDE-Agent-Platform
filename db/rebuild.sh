#!/usr/bin/env bash
# Drop and rebuild the whole schema from migrations, then run the smoke test.
# This is the CI gate: migrations must apply cleanly in order on an empty DB.
set -euo pipefail
DB="${1:-fde}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

dropdb --if-exists "$DB"
createdb "$DB"

for f in "$HERE"/0*.sql; do
  echo "==> $(basename "$f")"
  psql -d "$DB" -q -v ON_ERROR_STOP=1 -f "$f"
done

echo "==> smoke test"
psql -d "$DB" -v ON_ERROR_STOP=1 -f "$HERE/tests/smoke_test.sql"
