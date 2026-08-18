# Telegram Notifications Integration Contract

## Purpose

This document fixes the Telegram notification boundary for Hi, Jack so tournaments, JACKSIDE, rewards and member-portal work can continue without making product transactions depend on Telegram availability.

## Bot ownership

The Telegram bot used by the new Hi, Jack account-linking flow is also the notification bot. Its current product name is JACKSIDE Bot.

The legacy `@HJCapp_bot` used by the separate Telegram Mini App is not part of this notification transport.

The notification bot is owned and operated by the Hi, Jack project. Its Bot Token is stored only in the server environment as `HJC_TELEGRAM_BOT_TOKEN` and must never be committed to Git.

## Telegram Login

The OIDC authorization request uses:

`openid profile telegram:bot_access`

`telegram:bot_access` asks the user to allow the same bot associated with Telegram Web Login to send direct messages after login.

Existing accounts that linked Telegram before this scope was added may need to pass through Telegram authorization again to grant messaging access. Relinking must not create a second Hi, Jack account.

## Notification Center ownership

`hi-jack-admin-helper-mvp` owns:

- Telegram notification preferences and category opt-outs;
- manual campaigns and future scheduled campaigns;
- templates and automatic notification rules;
- audience resolution;
- one outbox row per recipient;
- delivery attempts and retry scheduling;
- delivery journal shown in admin UI;
- audit trail for admin actions;
- direct Telegram Bot API delivery through the JACKSIDE bot.

Tournament, JACKSIDE and reward transactions do not call Telegram directly. They only create notification intents/outbox work after their own product transaction succeeds.

## Delivery flow

1. Admin or product event creates a notification intent.
2. Notification rules and user preferences decide whether it is eligible.
3. Admin Helper writes one row per recipient into `telegram_notification_outbox`.
4. `idempotency_key` prevents duplicate enqueue for the same event/campaign and recipient.
5. The dispatcher claims due queued rows only when both safety gates are enabled.
6. The dispatcher calls Telegram Bot API `sendMessage` using `HJC_TELEGRAM_BOT_TOKEN`.
7. A successful Bot API response immediately marks the outbox row `sent` and stores Telegram `message_id` in the delivery journal.
8. Permanent Telegram errors are marked `failed`; transient network, 5xx and rate-limit failures are retried with backoff.

No product transaction waits for steps 5-8.

## Outbox payload v1

```json
{
  "text": "Message text",
  "category": "tournaments",
  "button_text": "Открыть",
  "button_url": "https://club.hijackpoker.ru/..."
}
```

For the first transport release buttons are URL buttons. Callback buttons such as tournament participation are a separate next step and require a bot update/webhook handler plus signed action tokens.

## Recipient identity

Admin Helper supports both Telegram fields in the existing client database:

1. `telegram_user_id` - preferred when present;
2. `telegram_id` - legacy fallback.

Audience selection and outbox creation use the same fallback order.

## Subscription defaults

When Telegram becomes linked to an account for the first time, or is linked again after a full unlink, notification preferences are enabled by default.

Categories:

- `tournaments`
- `jackside`
- `rewards`
- `club_updates`
- `marketing`

The user can disable the whole Telegram channel or individual categories later. Marketing must still respect the applicable marketing-consent policy; Telegram messaging permission is not a substitute for marketing consent.

## Failure semantics

Telegram failures never roll back product actions.

Mapping:

- HTTP/API 400 or 403 for a recipient/payload -> permanent failure, no retry;
- blocked/deactivated/chat-not-found -> permanent failure;
- HTTP/API 429 -> retry using Telegram `retry_after` when provided;
- timeout/network/5xx -> retry with local backoff;
- invalid/missing Bot Token -> configuration block; stop the current dispatch pass after the first affected row;
- duplicate notification intent -> ignored by outbox idempotency.

The Telegram Bot API does not expose a general idempotency key for `sendMessage`. Local enqueue and claim are idempotent, but a rare network ambiguity after Telegram accepted a message and before the response reached the server can theoretically result in a duplicate retry. This must be considered when enabling automated high-value messages.

## Safety gates

Live delivery requires all of the following:

1. `HJC_TELEGRAM_NOTIFICATIONS_ENABLED=true`;
2. internal `telegram_notification_settings.sending_enabled=1`;
3. `HJC_TELEGRAM_BOT_TOKEN` configured on the server.

Until both switches are enabled, queued messages stay inside Admin Helper.

Automatic rules remain disabled until manual test-to-self delivery is proven stable.

## Dispatcher

One safe dispatch iteration is available through:

`python scripts/telegram_dispatch_once.py`

The first staging validation should process a campaign addressed only to the operator account. A periodic service/timer should be enabled only after test-to-self succeeds.

## Tournament action button - next phase

The future interactive action is:

`Участвовать`

Recommended callback data carries only a signed, time-bounded action token, for example:

`hj:tournament:join:<signed-action-token>`

The bot must identify the Telegram sender from the callback update and resolve that identity server-side. Raw user IDs or registration status from callback data must never be trusted. The registration action must reuse the same trusted tournament validation/locking rules as the application.

A repeated callback must be idempotent and should update the message/button to a state such as `✅ Вы участвуете` or the waitlist state.
