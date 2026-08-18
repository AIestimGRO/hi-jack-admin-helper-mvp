# Telegram Notifications Integration Contract

## Purpose

This document fixes the service boundary for Hi, Jack Telegram notifications so tournament, JACKSIDE, rewards and member-portal work can continue in parallel without direct dependencies on Telegram availability.

## Ownership

### hi-jack-admin-helper-mvp

Owns notification product state:

- Telegram notification preferences and category opt-outs;
- manual campaigns and future scheduled campaigns;
- templates and automatic notification rules;
- audience resolution;
- notification outbox and idempotency keys;
- delivery journal shown in admin UI;
- audit trail for admin actions.

It does not own the Telegram Bot Token and must not call the Telegram Bot API from tournament/JACKSIDE/reward transactions.

### hi_jack_club

Owns Telegram delivery transport:

- the existing Telegram bot;
- the existing Celery Telegram sender;
- Bot Token and Telegram transport configuration;
- Telegram callback handling;
- Telegram identity to application-user resolution;
- transport retries and Telegram-specific error interpretation.

Existing transport entry points:

- `celery_app.tasks.telegram.send_telegram`
- `celery_app.tasks.telegram.broadcast_messages`

The current bot already has an admin broadcast flow in `telegram_bot/handlers/callbacks/spamming.py`. It supports preview and mass delivery through the Celery transport. The notification project should reuse that sender rather than introduce another Telegram API client.

Existing tournament registration API:

`POST /api/tournaments/{id}/register/`

The current public API requires an authenticated application user. A Telegram callback must therefore resolve the Telegram user to the application user and call trusted internal registration logic; it must not impersonate a user based only on an ID supplied by the callback payload.

### hi-jack-timer

The timer does not own Telegram delivery. It may later publish domain events, for example `tournament_started` or `tournament_level_changed`, but a Telegram failure must never affect the timer state transition.

## Foundation flow

1. Admin or product event creates a notification intent.
2. Notification rules and user preferences decide whether it is eligible.
3. Admin Helper writes one row per recipient into `telegram_notification_outbox`.
4. `idempotency_key` prevents duplicate delivery intent for the same event/campaign and recipient.
5. A future transport adapter claims queued outbox rows only after both the environment feature flag and the internal sending switch are enabled.
6. The adapter hands the payload to the existing `hi_jack_club` Celery Telegram transport.
7. Delivery result is written to `telegram_notification_deliveries` and the outbox status becomes `sent`, `failed` or `skipped`.

No product transaction waits for steps 5-7.

## Outbox payload v1

The foundation stores JSON with this shape:

```json
{
  "text": "Message text",
  "category": "tournaments",
  "button_text": "Участвовать",
  "button_url": ""
}
```

The transport adapter may translate this to Telegram `sendMessage` parameters. New payload fields must be additive and backward-compatible.

The existing Celery sender currently accepts Telegram Bot API method arguments and can already retry transient failures. The adapter should translate the outbox payload to that existing task instead of copying its retry/rate-limit logic into Admin Helper.

## Recipient identity

Admin Helper currently supports both fields present in the existing client database:

1. `telegram_user_id` — preferred when present;
2. `telegram_id` — legacy fallback.

Audience selection and outbox creation must use the same fallback order. This prevents already-linked legacy users from silently disappearing from campaigns.

The main `hi_jack_club` user model already uses a unique `telegram_id`, which makes Telegram callback identity resolution deterministic once the same account mapping is available across the two systems.

## Subscription defaults

When Telegram becomes linked to an account for the first time, or is linked again after a full unlink, Telegram notifications are enabled by default as required by the product flow.

Foundation categories:

- `tournaments`
- `jackside`
- `rewards`
- `club_updates`
- `marketing`

The user can disable the whole Telegram channel or individual categories later.

## Transactional vs promotional notifications

Categories and templates must preserve enough metadata to distinguish service/transactional messages from promotional messages. The transport adapter must not collapse that distinction because consent and future frequency controls may differ.

## Tournament action button

The intended tournament notification action is an inline Telegram button:

`Участвовать`

Recommended callback contract:

`hj:tournament:join:<signed-action-token>`

The signed token should identify the action and tournament, be time-bounded where appropriate, and be verified server-side. Do not trust raw `user_id`, registration status or privileges from callback data.

Callback flow:

1. Telegram bot receives the callback query.
2. Bot identifies the Telegram sender from Telegram's update object.
3. Backend resolves that Telegram identity to a Hi, Jack application user.
4. Trusted tournament registration service performs the same validation/locking used by the application registration endpoint.
5. Bot answers the callback and updates the button/message to a state such as `✅ Вы участвуете` or shows the waitlist state.
6. Repeated callback execution must be idempotent.

A later `Отменить участие` action should use the same pattern and the existing unregister business rules.

## Minimal transport patch in hi_jack_club

The next implementation in the main repository should stay deliberately small:

1. add a service-authenticated internal endpoint or worker ingress that accepts one normalized outbox message;
2. validate a versioned payload and idempotency key;
3. enqueue the existing `send_telegram` task for one recipient, or reuse `broadcast_messages` only when the upstream audience has not already been expanded;
4. support Telegram reply markup for action buttons without replacing the existing sender;
5. return/record a stable delivery acknowledgement so Admin Helper can update the journal;
6. add an aiogram callback handler for signed tournament actions;
7. resolve `call.from_user.id` to the existing `User.telegram_id` and execute trusted tournament registration logic;
8. cover registration success, waitlist, already registered, registration closed and invalid/expired token cases with tests.

Admin Helper already expands audiences into one outbox row per recipient, so the preferred transport path is the single-recipient `send_telegram` task. This preserves per-user idempotency and delivery status. The existing mass `broadcast_messages` task remains useful for legacy bot-admin broadcasts.

## Failure semantics

Telegram failures never roll back product actions.

Suggested mapping:

- blocked user / deactivated user / chat not found -> permanent `skipped` or `failed`, no repeated product-side retry;
- timeout / network / 5xx / rate limit -> transient retry handled by transport;
- duplicate notification intent -> ignored by outbox idempotency;
- invalid campaign payload -> permanent `failed` with journal entry.

## Safety gates

Live delivery remains OFF until all of the following are deliberately completed:

1. isolated foundation tests are green;
2. admin UI is manually checked in staging;
3. a transport adapter to `hi_jack_club` is implemented with service authentication;
4. test-to-self works end-to-end;
5. delivery result callback/journal works;
6. environment feature flag is enabled;
7. internal sending switch is enabled.

Automatic notification rules remain disabled until manual delivery is proven stable.

## First live scenario

The first automatic scenario should be a tournament reminder, for example two hours before start. It exercises scheduling, audience selection, template rendering and delivery without changing tournament or JACKSIDE business state.
