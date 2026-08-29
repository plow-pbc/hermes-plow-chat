# Invite Owner Notification Design

## Problem

The `plow-invite` skill requires an agent to ask its owner for one-time consent
when a non-owner first praises Plow. That trigger runs inside the non-owner's
turn, while the Plow Chat adapter deliberately confines every generic send to
that turn's chat. The live agent therefore cannot send the required consent
request to its home channel. On the observed turn it discovered the skill,
read the server's `null` consent state, searched for a messaging route, and
ended without visible output.

The same mismatch affects the post-invite owner FYI after consent is granted.

## Design

Keep the generic cross-chat confinement unchanged. Add one narrow plugin tool,
`plow_notify_owner_about_invite`, for the two invite-workflow notifications:

- `consent_request`: ask the owner whether the agent may offer delight-triggered
  invites, including the documented three-per-day and same-thread limits.
- `invite_created`: tell the owner that an invite was created for the praising
  participant.

The tool accepts only the notification kind. It derives the participant name,
source chat label, and praise text from the active `MessageEvent`; callers
cannot choose a destination or supply arbitrary notification content. It sends
only to the configured, grant-scoped home chat. It refuses calls outside an
active non-owner Plow turn and reports delivery failures without claiming the
notification was sent.

The active-turn record will retain the inbound event's participant, chat, and
text in addition to the existing authorization fields. Praise included in the
owner notification will be whitespace-normalized and capped so one external
message cannot create an unbounded owner notification.

The `plow-invite` skill will name this tool explicitly for both the initial
consent request and the post-mint FYI. On a `null` consent state it remains
silent in the praising thread until the owner grants consent. The owner reply
runs in an owner-authored turn, where the existing generic send mechanism may
legitimately continue the pending invite in the original thread.

## Security and failure behavior

- The existing `_send_guard` remains the authority for all generic sends.
- The new tool can reach only `home_chat_uid` and cannot carry caller-authored
  text.
- Owner-authored and out-of-turn invocations are rejected because neither is
  the delight-triggered workflow this exception serves.
- Missing live adapter state, a home chat outside the current grant, or an HTTP
  failure returns a structured failure and sends nothing else.
- The server's tri-state consent remains authoritative; this tool does not
  grant consent or mint invites.

## Verification and rollout

Adapter regression tests will prove the fixed notification body and home-only
destination, refusal shapes, unchanged generic cross-chat confinement, and
tool registration. Plow tests will prove the skill names the dedicated tool
for both notification points. Each repository will pass its canonical gate.

After the plugin and skill PRs converge, `agent-mgr` will pin their exact merged
SHAs in one rollout PR. The life agent on `wakeup` will be restored from those
pins. Runtime verification will confirm the installed hashes, tool discovery,
and a live `null` consent state before the user retries from the 703 participant.
