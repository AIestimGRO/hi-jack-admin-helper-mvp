# JACKSIDE shared 4:14 clock and final last-question race

This document describes the gameplay invariants introduced for JACKSIDE issue-backed releases (`jackside_YYYYMMDD...`).

## Main round

- The club has one authoritative main-round start time.
- The main round closes exactly 254 seconds (4:14) after that start.
- Joining late is allowed only while that shared window is still open.
- A late join never receives a fresh personal 4:14.
- The attempt deadline stored for every issue-backed player is the same absolute timestamp: `issue.starts_at + 254 seconds`.
- An attempt that does not contain all 10 saved answers before the shared deadline is not a completed main round and is not eligible for JACKCOIN/rating/final selection under completion-gated rules.
- Official completion time is elapsed time from the shared issue start, not from the player's join time. Example: join at +60 seconds and solve in 120 seconds => official completion time 180 seconds.
- Final-table ranking remains: correct answers descending, official completion time ascending, submission id ascending as deterministic final tie-break.

## Final-table start and qualification

- Up to 10 best completed/eligible main-round submissions are seeded.
- For new issue-backed releases the final table starts when the shared 4:14 main window closes.
- Historic directly-created `daily_414` campaigns keep their historical final schedule for backward compatibility; this does not apply to new `jackside_...` issues.
- A final table still requires at least two finalists to open. A single qualifier does not win automatically.

## Final questions before the last question

- Questions are synchronous and use server-controlled windows.
- A wrong answer or no answer eliminates the finalist.
- If nobody remaining answers correctly, the final ends with `no_winner`.
- If only one finalist survives an intermediate question, that player stays active but is not yet the winner. They must still play the last question.

## Last question: race rule

- The last final question is a race for one winner.
- The winner is the active finalist whose correct answer has the earliest server-recorded `answered_at` timestamp.
- If timestamps are equal, `response_time_ms` and then the answer row id provide deterministic ordering.
- This rule also applies when the entire final consists of one question.
- If several players eventually answer the last question correctly, only the earliest correct answer wins.
- If nobody answers the last question correctly, the outcome is `no_winner` and the main prize is not awarded.
- New finals do not create `co_winners`. Historical co-winner rows and prize-split code remain readable for backward compatibility only.

## Source-of-truth / anti-race guarantees

- Server timestamps are authoritative.
- Issue-backed attempt deadlines and official completion times are normalized in SQLite before ranking queries consume them.
- Final answers are unique per `(finalist_id, question_index)` and cannot be changed after save.
- The earliest correct last-question answer is selected from persisted server-side answer rows.

## Rules publication

`DEFAULT_RULES_CONTENT` reflects the new mechanics. Existing installations may already have an active versioned rules row in `jackside_rules_versions`; deployment must activate/publish the new rules version before public launch so users see and accept the changed mechanics.
