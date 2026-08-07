# JACKSIDE referrals, achievements and titles

Schema version: `2026.08.07.jackside-engagement`.

## Qualified referrals

JACKSIDE uses a stable referral code with scope `jackside`. The public link is `/jackside/ref/{code}` and redirects to the current featured JACKSIDE issue while preserving the referral code through login and the quiz page.

The ownership rule is strict:

- the invited client can have only one row in `referral_qualification_progress`;
- self-referral is rejected;
- once a referrer is fixed, another referral code cannot replace it;
- legacy classic referrals keep using `quiz_referrals` and their old campaign thresholds;
- legacy `daily_414` referral rows are migrated to the new fixed-owner table by taking the earliest recorded JACKSIDE relation.

Qualification requires three eligible JACKSIDE completions on three different club-local calendar dates. Multiple completed attempts on one date count as one day. Only `daily_414` submissions with `main_round_completed=1` and a published question count greater than zero are eligible.

After the third distinct date, the progress row receives `qualified_at` and `qualified_date`. This transition is idempotent. The pair is rewarded once; there is no extra “invite N people” milestone.

Referrer and invited-player material rewards are configured separately in `jackside_referral_settings`. Each side can use a different counter preference, amount and delivery mode. Empty preference or zero amount means no material reward.

## Member referral dashboard

The account shows:

- stable referral link;
- recorded link clicks;
- fixed registrations/relations;
- referrals with exactly one completed date;
- referrals with exactly two completed dates;
- referrals that reached three or more dates;
- qualified referrals;
- issued referral rewards;
- active referral-category titles;
- per-player referral history.

Click count is an operational count of visits through `/jackside/ref/{code}`. Registration count means that the JACKSIDE relation has been fixed to a member account; it is not inferred from classic campaign data.

## Achievements

`achievement_definitions` contains data-driven definitions. `member_achievements` is append-only from the product point of view: an achievement has no expiry and is unique for a client and definition.

Supported metric condition codes are:

- `completed_games`;
- `correct_answers`;
- `perfect_games`;
- `finals`;
- `wins`;
- `qualified_referrals`;
- `best_streak`;
- `current_streak`.

Definitions hold a threshold and may optionally enable a material counter reward. Material reward is disabled by default and is recorded idempotently in `engagement_reward_grants`.

## Titles

`title_definitions` contains name, description, icon, type, condition, threshold, period, priority and enabled state. The evaluator reads these rows instead of hardcoding named title thresholds.

Permanent titles are stored in `member_titles` without a temporary period. The member may select exactly one permanent title. If nothing is explicitly selected, the highest-priority earned permanent title is used.

Temporary titles use `temporary_title_periods` with a concrete week or month interval. A temporary title is never deleted when the interval ends. Its `member_titles` row remains in history, but it stops being effective after `expires_at`.

An active temporary title has display priority over the selected permanent title. Among simultaneously active temporary titles, larger `priority` wins.

A final-table winner with `daily_414_finalists.status='winner'` counts as a win regardless of whether the final outcome is `single_winner` or `co_winners`.

Initial seeded title directions:

- Stability: Сел за стол, Регуляр JACKSIDE, Железная серия, Не пропускает раздачу.
- Knowledge: Крепкий префлоп, Тёрн-мастер, Риверный хищник, Натсовый разум.
- Finals: Финалист, Хедз-ап мастер, Хозяин финального стола, Легенда JACKSIDE.
- Referrals: Привёл игрока, Собрал стол, Хозяин стола, Почётный рефовод Hi, Jack!.
- Temporary: Зазывала недели, Зазывала месяца, Шарк месяца, Натс месяца, Железная серия месяца.

The seeded values are defaults only. Admin-created and seeded rows use the same evaluator fields and can be changed without changing Python code.

## Notifications

New achievements, titles and referral qualifications create idempotent rows in `member_notifications`. The account shows unread notifications and provides an explicit mark-as-read action.

## Admin UI

Master -> “Звания и рефералы” provides:

- separate referrer/invited reward settings;
- title creation;
- title name and description;
- icon;
- permanent/temporary type;
- condition metric;
- threshold;
- week/month/all-time period;
- priority;
- enabled/disabled state;
- optional material reward preference and amount.

Achievements are deliberately not coupled to material rewards. A definition must explicitly enable its optional material reward fields.

## Additive migration

`init_db` creates these entities without deleting existing data:

- `achievement_definitions`;
- `member_achievements`;
- `title_definitions`;
- `member_titles`;
- `temporary_title_periods`;
- `referral_qualification_progress`;
- `jackside_referral_settings`;
- `jackside_referral_clicks`;
- `member_notifications`;
- `engagement_reward_grants`.

It also creates uniqueness and lookup indexes and imports the earliest legacy JACKSIDE referral relation for each invited client. Classic `quiz_referrals` rows and classic reward logic are not modified.

After deployment run the backfill once:

```bash
.venv/bin/python scripts/refresh_jackside_engagement.py \
  data/club_tools.sqlite3 --timezone Europe/Moscow
```

The script recomputes distinct referral dates for migrated relations and evaluates achievements/titles for existing clients.
