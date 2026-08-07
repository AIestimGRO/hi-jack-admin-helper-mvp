# JACKSIDE ratings and analytics

Schema version: `2026.08.06.jackside-ratings`.

This stage adds JACKSIDE-only ratings, compact player statistics, and cached administrative analytics. Classic quiz campaigns are never included in the calculations below.

## Common result filter

A result is eligible for score, accuracy, streak, final-rate, and personal game statistics only when all of the following are true:

- `quiz_campaigns.campaign_type = 'daily_414'`;
- `quiz_submissions.main_round_completed = 1`;
- `quiz_submissions.max_correct_count > 0`.

Incomplete attempts remain available for operational completion and timeout metrics, but they do not enter player results or leaderboards.

## Leaderboards

### Today

The Today table uses the club-local calendar date (`HJC_TIMEZONE`, normally `Europe/Moscow`). For a linked JACKSIDE issue, `jackside_issues.issue_date` is authoritative. A legacy daily campaign falls back to the local date of the submission.

Ordering:

1. correct answers, descending;
2. total completion time, ascending;
3. submission ID, ascending as the stable internal tie-breaker.

Finalists are marked from `daily_414_finalists`. A winner is a finalist whose status is `winner`; `daily_414_final_tables.outcome='co_winners'` marks a shared win.

### Month

Month preserves the existing rolling 30-day JACKSIDE rating. The window is the last 30 x 24 hours up to the snapshot time, not a named calendar month.

For completed games:

- raw accuracy = correct answers / published questions;
- confirmed accuracy = the 95% Wilson lower bound;
- activity = `min(completed games / 8, 1) * 100`;
- rating score = `0.85 * confirmed accuracy + 0.15 * activity`.

A player receives a ranked place after at least three completed games and 30 answered questions. Before that, the row is shown as calibration. Only `daily_414` is read; classic campaigns are excluded.

Ranked ordering:

1. rating score;
2. active days;
3. correct answers;
4. last result time;
5. client ID.

### All time

JACKSIDE points are a non-spendable sporting score. They are not JACKCOIN and do not affect the wallet.

- +1 point for every correct answer in a completed main game;
- +2 points for every final-table participation;
- +5 points for every single or shared final win.

Ordering:

1. JACKSIDE points, descending;
2. accuracy, descending;
3. average answer time, ascending;
4. completed games, descending;
5. client ID, ascending.

Average answer time is total completion time divided by the total number of questions in timed completed submissions.

## Player statistics

The mobile cabinet intentionally shows only player-facing metrics:

- completed JACKSIDE issues;
- correct answers and accuracy;
- average time per answer;
- best result and number of perfect results;
- final-table participations;
- wins and shared wins;
- current and best streak;
- JACKCOIN earned and spent;
- active and redeemed JACK CARDS;
- qualified referrals whose referred player completed JACKSIDE;
- current automatic title.

There is no separate “wins today” metric.

Titles are presentation labels derived from all-time JACKSIDE points:

| Points | Title |
|---:|---|
| 0 | Rookie |
| 1–29 | Participant |
| 30–99 | Regular |
| 100–249 | Strong player |
| 250–499 | Final-table regular |
| 500+ | JACKSIDE legend |

The title is not an achievement object and does not add an award or currency.

## Administrative participant analytics

Unless stated otherwise, operational rates use a 30-day window ending at snapshot time.

- Registered: active member accounts.
- Joined today: accounts created on the club-local current date.
- Completed today: unique players with an eligible completed result today.
- Activity 3/7/14/30: unique players with at least one eligible completion in the inclusive local-date window.
- Retention D1/D7/D30: active account cohorts that completed an eligible JACKSIDE exactly 1, 7, or 30 local calendar days after registration. When there is no eligible cohort, the UI displays insufficient data rather than zero percent.
- Average/median streak: current effective streak among players with completed JACKSIDE history.
- Average result: mean correct-answer count across eligible completions in the 30-day window.
- Completion rate: eligible completed main games / started JACKSIDE attempts.
- Timeout rate: submissions marked `timed_out=1` / started JACKSIDE attempts.
- Final rate: final-table submissions / eligible completed main games.
- Late rate: eligible completed games with `main_prize_eligible=0` / eligible completed main games.
- Qualified referrals: referral rows whose referred submission is an eligible JACKSIDE completion.
- Reward usage: redeemed cards / all issued cards.

## Administrative economy analytics

- Accrued: positive JACKCOIN ledger entries excluding `vault_refund`.
- Spent: absolute value of negative ledger entries.
- Refunded: positive entries with `operation_type='vault_refund'`.
- Current balance: sum of the entire JACKCOIN ledger.
- Accrual reasons: positive amounts grouped by ledger `source_type`.
- Spend by reward: purchase price grouped by catalog reward.
- Activated: cards with `activated_at`.
- Redeemed: cards with `status='redeemed'`.
- Expired: cards with `status='expired'`.
- Unused: cards currently `active`.
- Shared payouts: awarded final tables with `outcome='co_winners'`, including the JACKCOIN total recorded on those tables.
- Manual prize resolutions: completed `daily_414_master_tasks`.

## Cache and refresh architecture

Heavy aggregates are stored as one JSON snapshot in `jackside_analytics_cache` under key `jackside.analytics.v1`.

`jackside_analytics_state` contains:

- `source_version`: incremented by lightweight SQLite triggers whenever a relevant source table changes;
- `refreshed_version`: source version included in the current snapshot.

The application refreshes the snapshot:

- once during startup;
- after source data changes, no sooner than five minutes after the previous snapshot;
- after at most 15 minutes even without source writes, so local-date and rolling-window boundaries advance;
- after an explicit master action;
- through the maintenance command below.

The five-minute request hook only checks the lightweight cache/state rows. Normal page rendering reads the cached snapshot and does not execute the historical aggregate queries on every request. A burst of submissions therefore marks the cache dirty without forcing every following account page to rebuild it.

Manual/cron refresh:

```bash
.venv/bin/python scripts/refresh_jackside_analytics.py \
  data/club_tools.sqlite3 \
  --timezone Europe/Moscow
```

## Additive migration

`init_db` performs an idempotent additive migration:

- adds `quiz_submissions.timed_out INTEGER NOT NULL DEFAULT 0`;
- creates `jackside_analytics_cache`;
- creates `jackside_analytics_state`;
- creates source-version triggers for submissions, finals, progress, JACKCOIN, cards, referrals, member accounts, and clients;
- creates indexes for completed-result windows, timeout lookup, campaign type, account cohorts, ledger operations, and reward status.

No existing table or result is deleted. Old rows receive `timed_out=0`; this avoids inventing timeout history that was not previously recorded.

## Rollout checks

After deployment:

```bash
git rev-parse HEAD
.venv/bin/python -m compileall -q app scripts load
.venv/bin/python scripts/refresh_jackside_analytics.py \
  data/club_tools.sqlite3 --timezone Europe/Moscow
curl -fsS http://127.0.0.1:8091/health
```

Verify the member rating tabs Today, Month, and All time, then open Master -> JACKSIDE analytics. A code rollback can ignore the new additive tables and column. Restore the pre-update SQLite backup only for a complete data rollback.
