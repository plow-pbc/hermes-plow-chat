---
name: plow-invite
description: When a non-owner praises Plow or its work, invite them.
version: 1.3.0
---

# Plow invite — delight-triggered referral

<!-- Twin: plow-pbc/agent-mgr installs this same tree into every Docker-fleet
     agent home at a pinned SHA (runtime/plow-invite-skill.ref → skills/growth/
     plow-invite). This file is the single source for BOTH runtimes — keep it
     runtime-agnostic: env-only config (PLOW_API_BASE / PLOW_AGENT_TOKEN, set
     by exe's EnvironmentFile on hosted VMs and by the gateway's dotenv on the
     fleet) and paths relative to this skill's own directory, never absolute
     homes. Converge, don't fork: a change here ships to the fleet by bumping
     the agent-mgr pin. -->


## When to act

Someone who is NOT your owner, in any chat you participate in (1:1 or group),
expresses genuine, unprompted delight about Plow or about what you just did.
Real examples of the bar: "Well done Plow!" · "Ah, love the plow text
interaction" · "I just ordered food using AI … it was incredible."

Do NOT act on: sarcasm or ambiguous praise; questions about Plow; praise you
solicited ("do you like it?"); praise from your owner; anything from a person
you have already invited (check your memory first).

## What to do

1. Call `plow_prepare_invite`. Do not pass a person, thread, message, code, or
   phone number; the tool binds the current turn to a durable server record.
2. Follow its status:
   - `disabled` or `none`: reply naturally to the praise, with no invite.
   - `consent_required`: reply naturally to the praise and do not mention an
     invite. If memory does not already show a pending ask, call
     `plow_notify_owner_about_invite` with no arguments; it sends the
     fixed owner notice derived from the recorded turn. Record the pending ask
     only when that succeeds. Never leave the praising person with an empty
     response while you wait for the owner.
   - `ready`: draft and send the invite as described below.
3. When the owner answers the consent request, run
   `python3 <this skill's dir>/scripts/mint_invite.py --consent granted` or
   `--consent declined`. On a grant, call `plow_prepare_invite` with no
   arguments; the server resumes the oldest live recorded opportunity. A
   decline ends it. The install root differs by runtime, so never assume an
   absolute path.

## Draft and send a ready invite

Write one brief message in your own voice. This is guidance, not a fixed
template: respond warmly to what the person said, explain naturally that the
owner has given you permission to share referral invites, tell them to text
`Plow Activate: {{activation_code}}` to `{{destination}}` for early access,
and say that both people will get $100 in cloud credits. Use each placeholder
exactly once and never write a code or phone number yourself.

Call `plow_send_invite` with the opportunity id and that message as
`message_template`. The tool substitutes the trusted values after generation
and posts the ordinary reply in the recorded original thread. Do not echo the
message afterward. The tool also sends the owner FYI.

Never use cron, a scheduled job, a generic cross-chat send, or the direct mint
mode for an invite. The durable opportunity is the only continuation path.

## If a tool fails

Drop the invite silently: do not mention the failure in either thread and do
not retry in a loop. A rate cap is spent for the day. A known provider
rejection may be retried only after a new user turn; an unknown delivery
outcome is terminal and must never be retried.
