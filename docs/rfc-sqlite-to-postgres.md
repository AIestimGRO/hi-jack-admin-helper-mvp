> Capacity note: the existing matrix is an in-process laboratory baseline. The 500-journey p99 of about 225 seconds does not demonstrate public readiness; validation on the actual VPS through nginx is still required.

# RFC: SQLite → Postgres (JACKSIDE)

**Status:** stub — capacity decision is still open. The 2026-08-05 ASGI
load matrix found no lock or integrity failures, but it is not a public-launch
capacity test. See `docs/load-reports/matrix.md`.

**Related:** [jackside-launch-hardening.md](jackside-launch-hardening.md)

## Measured baseline (single-process ASGI + SQLite WAL)

| users | journey p99 | errors | locked |
| ---: | ---: | ---: | ---: |
| 100 | ~38 s | 0 | 0 |
| 300 | ~121 s | 0 | 0 |
| 500 | ~225 s | 0 | 0 |

Journey latency scales with concurrency because writers serialize on one DB
file. Integrity stayed clean, but a 500-journey p99 near 225 seconds is
unacceptable as evidence of public readiness. A production-like test on the
actual VPS through nginx is required before making a capacity claim.

## When to migrate

Revisit this RFC when a reproducible run of
`python -m load.jackside_load --users 300` (or production-equivalent load)
shows either:

| Signal | Threshold |
| --- | --- |
| Single request (start/answer/finish) p99 | **> 2000 ms** under load |
| Database locked / busy rate | **> 1%** of requests |
| Integrity failures | any lost answers / double JACKCOIN / duplicate submissions |
| Sustained concurrent finishers | **≥ 300** with the latency or lock symptoms above |

Passing the in-process 50–500 journey runs only establishes an integrity
baseline for SQLite + WAL. It does not establish acceptable public latency or
capacity. The VPS/nginx run must drive the launch and migration decision.

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
