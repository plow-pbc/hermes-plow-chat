---
name: google-workspace
description: "Gmail and Google Calendar through the owner's Mac."
version: 2.4.0
---

# Google Workspace — through the owner's Mac

This agent holds no Google credential. There is no local OAuth token
(no `google_token.json` exists in this home), and local Google OAuth
scripts from older copies of this skill do not work here. Never run
them and never start a local Google OAuth setup flow.

Gmail and Google Calendar are reached through the owner's Mac, over
the Plow relay MCP server — the configured server whose tools start
with `plow_` (its name varies by install: `plow` here, `latch` on
Mac-managed instances):

1. Call `plow_list_skills`. If it lists `google-workspace`, read it
   with `plow_read_skill` and follow it exactly. That skill is the
   only source for the command and its arguments — do not carry a
   spelling from memory or from this file. The Mac mints its own
   short-lived Google token; you never see or need one.
2. Sending an email is stopped first in the owner's direct chat with
   you: the gateway posts the command and waits for the owner to reply
   `/approve`. Compose the whole message — recipients, subject, body —
   in the one `gmail send` command; that command is all the owner sees.
   A draft sent by id is refused, because the owner would see only the
   id. A refusal there means the owner declined; nothing was sent. From
   any other chat a send is blocked outright and nothing is sent — ask
   the owner to repeat the request in their direct chat. Any other
   command may show the owner an approval card on their Mac; if it
   hangs, it is waiting there, and a refusal there is a denial on the
   Mac.
3. A calendar conflict comes back to you rather than to an
   approval card on the Mac — but whether to book over it is the
   owner's call, never yours alone. A calendar create that
   overlaps an existing commitment is refused, never queued for
   approval: the check covers every connected account, and the
   refusal comes back to you. Tell the owner what the slot
   overlaps and ask; a plain "yes" is enough. Up-front consent
   counts only when they show they know about the overlap ("book
   it regardless", "I know it overlaps, hold those exact dates").
   A bare preferred time is not consent: tell them what it
   overlaps and ask. Once the owner has said go ahead for that
   time, book it even if something else has appeared since; name
   whatever it overlaps in your reply. Re-send the complete
   original command with `--confirm-conflict` added; never rebuild
   a smaller create or drop its attendees or calendar. When you do
   book over one, say so in the reply and name the overlap.
   Whether you are asking about a conflict or reporting one you
   booked over, in a shared room the overlap is "an existing
   commitment" and never the other event's name. Only the owner
   can fix a time, so from any other chat the override is blocked
   and nothing is booked — ask the owner to repeat the request in
   their direct chat.
4. If a connected MCP server lists no `google-workspace` skill, Google is
   not available to this agent. Say exactly that — do not fall back to
   local OAuth. If no MCP server with `plow_*` tools is connected at all,
   that is not the same thing: Latch is unreachable, so say the owner's Mac
   has to be awake with Latch running. Do not fall back to local OAuth in
   either case.
