# HI, JACK! rating + referral tree

## Rating imports

Master admin page: `/master/hijack-rating`.

Each XLSX upload represents one HI, JACK! tournament and requires:

- tournament name;
- tournament date;
- Excel columns `Phone`, `ИГР Рейт`, `ИГР Кил`.

Rows are linked to `clients` by normalized Russian 10-digit phone. Unmatched and invalid rows stay in the import for audit counts but do not enter member leaderboards until a future relink workflow is added.

Member HI, JACK! rating has three views:

- Global — sum of tournament rating points for the current calendar year;
- Month — sum of tournament rating points in the current calendar month;
- Last tournament — the most recently uploaded tournament, including its name/date.

Tie-break: rating points descending, kills descending, tournaments played descending, client id.

## HI, JACK! title conditions

The title editor exposes additional conditions:

- year / month / latest tournament rating;
- year / month / latest tournament kills;
- tournaments played;
- top-3 finishes;
- wins;
- best single-tournament rating.

Existing JACKSIDE title conditions and behavior remain unchanged.

## Referral tree

The tree is derived from `referral_qualification_progress(referrer_client_id -> invited_client_id)`.
It includes all known descendants up to a defensive depth of 20, protects against cycles, and shows direct line, total descendants, maximum depth and each descendant's JACKSIDE referral qualification progress.

## UI polish

- My Cards keeps the dark reward artwork surface and receives a subtle gold tint.
- Rating hero helper copy is removed.
- Phone collection emblems grow from ~40–45 px to ~82–88 px; custom artwork uses ~2 px internal padding.
