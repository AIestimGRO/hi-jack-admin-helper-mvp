> These figures are a laboratory in-process ASGI baseline, not proof of public capacity.
> The 500-journey run has p99 around 225 seconds, which is unacceptable as a readiness claim.
> A real VPS test through nginx with production-like networking and monitoring is still required.

# JACKSIDE load matrix

Generated: 2026-08-05 (local Windows run; ASGI httpx harness)

| users | avg_ms | p95_ms | p99_ms | errors | db_locked | duplicates | lost_answers | duration_s |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 20 | 7097.84 | 7187.7 | 7189.18 | 0 | 0 | 0 | 0 | 7.22 |
| 50 | 18382.73 | 18476.46 | 18489.14 | 0 | 0 | 0 | 0 | 18.629 |
| 100 | 37290.02 | 37739.36 | 37758.9 | 0 | 0 | 0 | 0 | 37.88 |
| 300 | 118615.98 | 121195.75 | 121275.82 | 0 | 0 | 0 | 0 | 121.64 |
| 500 | 217879.95 | 224840.72 | 225108.78 | 0 | 0 | 0 | 0 | 225.857 |

Re-run matrix:

```powershell
.\load\run_matrix.ps1 -Full
# or:
python -m load.jackside_load --users 300 --report docs/load-reports/users-300.json
python -m load.jackside_load --users 500 --report docs/load-reports/users-500.json
```

```bash
./load/run_matrix.sh --full
```

Notes:
- Scenario: member-authenticated daily_414 start → 10 correct answers → finish → final-status poll.
- Temp SQLite + WAL; unique `X-Forwarded-For` per user to bypass per-IP attempt limits.
- Integrity checks after each run: duplicate submissions, double JACKCOIN ledger rows, incomplete answers.
- Wall time scales roughly linearly with concurrency on a single SQLite file; integrity stayed clean through 500.
- Postgres RFC threshold: locks / integrity failures (not linear journey time alone). See `docs/rfc-sqlite-to-postgres.md`.
