# JACKSIDE package 1: product shell

This package is an additive member-portal/UI layer on top of the deployed shared-clock JACKSIDE mechanics.

## Hi, Store

- Member navigation label is `Hi, Store`.
- Store UI is split into `Market` and `My Cards`.
- `Market` contains the existing JACKCOIN catalog and purchase flow.
- `My Cards` contains existing active JackCards and activation/QR flow.
- JackCard history remains outside the two primary panels and keeps its existing behavior.
- Existing backend route names and database tables retain their historical `vault` names for compatibility. The user-facing product name is Hi, Store.

## Home cleanup

- The redundant home greeting block (`JACKSIDE`, greeting and explanatory subtitle) is removed from the member UI.
- The wallet links to Hi, Store.
- The reward preview is titled `Hi, Store` without the old `THE VAULT` copy.
- The progress block keeps `Прогресс` and removes the redundant `Статистика` heading.
- A nearest-tournament card is mounted when a published future tournament exists.

## Schedule shell

- The bottom navigation label `Квизы` becomes `Расписание`.
- The schedule page contains `Квизы` and `Турниры Hi, Jack!` tabs.
- Package 1 creates the additive `club_tournaments` table with, among other fields, `max_slots` and `registration_open`.
- Tournament registration, cancellation, seat numbering, waitlist promotion and notifications are intentionally reserved for package 2. Until tournaments are populated, the member UI shows a neutral empty schedule state.

## Member nickname and avatar

- Members can update `clients.nickname` from Profile.
- Members can upload a photo avatar or a transparent sticker avatar.
- Photos are normalized to a centered 512x512 JPEG.
- Sticker uploads accept PNG/WEBP and are normalized to a transparent 512x512 PNG canvas.
- Profile media is stored under the existing private deployment data tree in `reward-media/profile-avatars` and referenced by the additive `member_profile_media` table.

## Custom title and achievement icons

- `icon_path` is added to `title_definitions` and `achievement_definitions` additively.
- Master admins get a dedicated `/master/engagement-icons` screen.
- Uploaded images are normalized to transparent 512x512 PNG files in `reward-media/engagement-icons`.
- When `icon_path` exists it replaces the old text/emoji icon in the member collection; existing floating/levitation UI remains unchanged.
- Removing a custom image restores the existing text/emoji fallback.

## Chats shell

- Authenticated member pages contain a floating Chats launcher.
- The launcher calculates its bottom offset from the current navigation and checks visible primary actions before settling, lifting upward when necessary so it does not cover important controls.
- The quiz gameplay page is a separate template and therefore does not render the launcher. The client script also contains a defensive active-game hide condition.
- Chats opens as a full-screen `/account/chats` interface rather than a popup.
- A fixed `Вернуться в Hi, Jack!` control returns to the originating safe `/account...` state.
- Package 1 only establishes the messenger shell and unread-badge contract. Conversation storage, admin/user messaging, attachments, reactions, read receipts and moderation belong to the later chat package.

## Compatibility

- `app/main_impl.py` and shared-clock/final-race mechanics are intentionally untouched by this package.
- The product shell is installed from `app/main.py` through `app.product_shell.install_product_shell(app)`.
- Schema changes are additive and idempotent.
- Existing Vault/JackCard database names and reward routes remain valid so current rewards are not migrated or invalidated.
