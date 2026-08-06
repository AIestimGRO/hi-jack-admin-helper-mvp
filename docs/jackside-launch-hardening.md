# JACKSIDE public launch hardening

Operational notes for the SQLite launch path. Postgres migration is documented
separately in [rfc-sqlite-to-postgres.md](rfc-sqlite-to-postgres.md) and is
**not required yet** if the load harness passes under expected concurrency.

## WAL

`app.db.connect()` enables Write-Ahead Logging on every connection:

- `PRAGMA journal_mode=WAL`
- `PRAGMA synchronous=NORMAL`
- `PRAGMA busy_timeout=30000` (connection timeout 30s)

WAL improves concurrent readers while a writer is active. Backup with the
SQLite online backup API (or `scripts/backup.py`) — do not copy only the
`.sqlite3` file while the process is writing without also handling `-wal` /
`-shm`, or use a consistent backup API.

## Indexes

Additive indexes created in `init_db()` (safe on existing databases via
`CREATE INDEX IF NOT EXISTS`):

| Index | Purpose |
| --- | --- |
| `ix_quiz_submissions_campaign_completed` | Main-round completion lookups by campaign |
| `ix_quiz_attempts_campaign_activity` | Active / stale attempt scans |
| `ix_daily_414_finalists_client` | Finalist row by table + client |
| `ix_jackcoin_ledger_client` | Ledger history by client (ensured) |
| `ix_jackcoin_ledger_ref` | Ledger lookup by `source_type` + `source_id` |

`ix_jackside_issue_participants_issue` already exists — do not recreate.

Schema marker: `SCHEMA_VERSION = "2026.08.05.jackside-launch"`.

## Backup before migrate / pull

Before pulling launch-hardening onto a live host:

1. Stop accepting traffic if you are about to replace the binary mid-issue, or
   keep the service up and use online backup.
2. Run a DB backup (preferred):

```bash
sudo -u www-data /opt/hi-jack-admin-helper-v2/.venv/bin/python scripts/backup.py \
  --project /opt/hi-jack-admin-helper-v2 \
  --destination /opt/hi-jack-club-tools-backups/v2-manual
```

3. Or copy via SQLite backup API into a dated file under
   `/opt/hi-jack-club-tools-backups/`.
4. Record the current git SHA: `git -C /opt/hi-jack-admin-helper-v2 rev-parse HEAD`.

See also [../deploy/v2-test/BACKUP_AND_ROLLBACK.md](../deploy/v2-test/BACKUP_AND_ROLLBACK.md).

## Rollback steps

1. Stop the v2 service: `sudo systemctl stop hi-jack-admin-helper-v2`.
2. `git -C /opt/hi-jack-admin-helper-v2 checkout <previous-sha>`.
3. Restore SQLite from the backup taken before the pull (replace
   `data/club_tools.sqlite3` and remove stale `-wal`/`-shm` if present after stop).
4. Start the service and `curl --fail http://127.0.0.1:8091/health`.
5. Confirm `schema_version` / `status=ok` and smoke quiz start/finish.

## Load thresholds → Postgres RFC

Treat Postgres migration as an RFC trigger when **under ~300 concurrent**
simulated users the harness reports either:

- quiz start/finish **p99 latency > 2000 ms**, or
- **database locked / busy rate > 1%** of requests

If both stay under those thresholds, stay on SQLite + WAL. Details and risks:
[rfc-sqlite-to-postgres.md](rfc-sqlite-to-postgres.md).

## How to run the load harness

From the repo root (uses a temporary SQLite DB; does not touch production):

```bash
python -m load.jackside_load --users 50 --report docs/load-reports/latest.json
```

Supported `--users` values: `50`, `100`, `300`, `500`. Review the JSON report
for p99, error rate, and lock/busy signals before public launch.

Health check after deploy:

```bash
curl -sS http://127.0.0.1:8091/health
```

Expect `status=ok`, `db_readable=true`, `db_writable=false`, and
`schema_version` matching `SCHEMA_VERSION`. `/health` is a read-only readiness
check. Use `POST /health/deep` only for an explicit diagnostic write probe.
