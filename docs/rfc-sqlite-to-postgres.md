# RFC: SQLite → Postgres (JACKSIDE)

**Status:** stub — **not required yet** if the load harness passes under the
thresholds below.

**Related:** [jackside-launch-hardening.md](jackside-launch-hardening.md)

## When to migrate

Revisit this RFC when a reproducible run of
`python -m load.jackside_load --users 300` (or production-equivalent load)
shows either:

| Signal | Threshold |
| --- | --- |
| Quiz start/finish p99 | **> 2000 ms** |
| Database locked / busy rate | **> 1%** of requests |
| Concurrent participants | sustained **≥ 300** with the above symptoms |

Passing 50/100/300 users without crossing those signals means SQLite + WAL
remains acceptable for public launch.

## What would move

- Primary store: `club_tools.sqlite3` → managed Postgres
- Connection layer: `app.db.connect` / `transaction` → driver + pool
- Schema: tables already close to portable SQL; review:
  - partial unique indexes (`WHERE ...`)
  - `INSERT OR IGNORE` / `ON CONFLICT` idioms
  - `datetime('now', ...)` expressions
- Ops: backup/restore, migrations, connection limits, HA

Out of scope for a first cut: rewriting quiz UX, JACKCOIN rules, or final-table
mechanics.

## Risks

- Dual-write or cutover downtime during migration
- Subtle SQL dialect differences (types, constraints, JSON helpers)
- Connection pool misconfiguration under burst (worse than SQLite if wrong)
- Need for new backup/monitoring runbooks
- Cost and ops complexity vs current single-file DB

## Decision note

Until load evidence trips the thresholds, prefer:

1. WAL + busy timeout + launch indexes
2. Structured ops logging and richer `/health`
3. Online SQLite backups

Escalate to a full Postgres design only with measured lock/latency data.
