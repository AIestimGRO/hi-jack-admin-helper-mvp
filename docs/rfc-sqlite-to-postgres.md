# RFC: SQLite → Postgres (JACKSIDE)

**Status:** stub — **not required for public launch** after the 2026-08-05
ASGI load matrix (50/100/300/500 users: 0 errors, 0 `database is locked`,
0 duplicate submissions, 0 double JACKCOIN, 0 lost answers). See
`docs/load-reports/matrix.md`.

**Related:** [jackside-launch-hardening.md](jackside-launch-hardening.md)

## Measured baseline (single-process ASGI + SQLite WAL)

| users | journey p99 | errors | locked |
| ---: | ---: | ---: | ---: |
| 100 | ~38 s | 0 | 0 |
| 300 | ~121 s | 0 | 0 |
| 500 | ~225 s | 0 | 0 |

Journey latency scales with concurrency because writers serialize on one DB
file; integrity stayed clean. That is acceptable for launch if nginx keeps
request timeouts high enough and workers are not over-parallelized against
one SQLite file.

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

Passing 50–500 users without integrity or lock failures means SQLite + WAL
remains acceptable for public launch; Postgres is the next step when locks or
integrity break, not merely when wall-clock journey time grows linearly.

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
