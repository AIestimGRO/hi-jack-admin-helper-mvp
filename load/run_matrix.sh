#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

FULL=0
if [[ "${1:-}" == "--full" ]]; then
  FULL=1
fi

COUNTS=(50 100)
if [[ "$FULL" -eq 1 ]]; then
  COUNTS=(50 100 300 500)
fi

REPORT_DIR="$ROOT/docs/load-reports"
mkdir -p "$REPORT_DIR"
MATRIX="$REPORT_DIR/matrix.md"

{
  echo "# JACKSIDE load matrix"
  echo
  echo "Generated: $(date '+%Y-%m-%d %H:%M:%S')"
  echo
  echo "| users | avg_ms | p95_ms | p99_ms | errors | db_locked | duplicates | lost_answers | duration_s |"
  echo "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
} > "$MATRIX"

for users in "${COUNTS[@]}"; do
  json="$REPORT_DIR/users-$users.json"
  echo "Running load with --users $users ..."
  python -m load.jackside_load --users "$users" --report "$json"
  python - <<PY >>"$MATRIX"
import json
from pathlib import Path
report = json.loads(Path(r"""$json""").read_text(encoding="utf-8"))
print(
    f"| {report['users']} | {report['avg_ms']} | {report['p95_ms']} | {report['p99_ms']} | "
    f"{report['errors_count']} | {report['database_locked_count']} | "
    f"{report['duplicate_submission_count']} | {report['lost_answers']} | "
    f"{report['duration_total_s']} |"
)
PY
  cp "$json" "$REPORT_DIR/latest.json"
done

if [[ "$FULL" -eq 0 ]]; then
  cat >>"$MATRIX" <<'EOF'

Skipped 300/500 in this run. On a server:

```bash
./load/run_matrix.sh --full
# or:
python -m load.jackside_load --users 300 --report docs/load-reports/users-300.json
python -m load.jackside_load --users 500 --report docs/load-reports/users-500.json
```
EOF
fi

echo "Wrote $MATRIX"
