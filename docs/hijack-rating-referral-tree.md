# HI, JACK! rating + referral tree

## Rating imports

Master admin page: `/master/hijack-rating`.

Each XLSX tournament upload requires:

- tournament name;
- tournament date;
- Excel columns `Phone`, `ИГР Рейт`, `ИГР Кил`.

Rows are stored by normalized Russian 10-digit phone. A row does not need an existing member account: when the matching client appears later, historical rating rows are relinked automatically.

The rating manager also supports a single accumulated baseline. The baseline is the club's starting global score and does not count as a tournament. Re-uploading the baseline replaces it atomically. Existing tournament imports can be fully replaced or individual player rows can be corrected by phone.

Member HI, JACK! rating has three views:

- Global — accumulated baseline plus all subsequently imported tournaments;
- Month — sum of tournament rating points in the current calendar month;
- Last tournament — the most recently uploaded tournament, including its name/date.

Tie-break: rating points descending, kills descending, tournaments played descending, client id.

## HI, JACK! title conditions

The title editor exposes additional conditions for global/year/month/latest rating and kills, tournaments played, top-3 finishes, wins, and best single-tournament rating.

Existing JACKSIDE title conditions and behavior remain unchanged.

## Hi, Titles!

Profile uses one collection named **Hi, Titles!**.

- The separate current/temporary-title banner is removed.
- Earned and currently active titles are placed first.
- All enabled title and achievement definitions are visible in the collection.
- Items that are not currently earned/active are rendered as fully grey locked cards.
- An expired temporary title returns to the locked part of the collection.
- The collection scrolls horizontally in two rows; on mobile the cards retain the large-emblem treatment.
- Permanent earned titles can still be selected as the member's primary title.

## Profile settings depth

The main Profile screen no longer shows editing controls and personal account data inline. The former `Профиль игрока` eyebrow becomes the **Настройки аккаунта** action. It opens `/account?tab=profile&view=settings`, where the existing profile editor and the account/personal-data block are shown. The main profile keeps club-facing content such as Hi, Titles!, statistics and referrals.

## Referral tree

The tree is derived from `referral_qualification_progress(referrer_client_id -> invited_client_id)`.
It includes all known descendants up to a defensive depth of 20, protects against cycles, and shows direct line, total descendants, maximum depth and each descendant's JACKSIDE referral qualification progress.

## UI polish

- My Cards keeps the dark reward artwork surface and receives a subtle gold tint.
- Rating hero helper copy is removed.
- Profile avatar is shown in the top-right account button across member sections.
- Phone collection emblems use large artwork with narrow internal padding.
- Master admin navigation is grouped by tasks without changing existing backend forms/API routes.
