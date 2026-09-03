---
name: google-workspace
description: "Gmail and Google Calendar through the owner's Mac."
version: 2.1.0
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
2. Sending an email, or booking over a busy slot with
   `--confirm-conflict`, is stopped first in the owner's direct chat
   with you: the gateway posts the command and waits for the owner to
   reply `/approve`. Compose the whole message — recipients, subject,
   body — in the one `gmail send` command; that command is all the
   owner sees, so a draft created first and sent by id would show
   them only a draft id. A refusal there means the owner declined;
   nothing was sent. From any other chat those two commands are
   blocked outright and nothing is sent — ask the owner to repeat the
   request in their direct chat. Any other command may show the owner
   an approval card on their Mac; if it hangs, it is waiting there,
   and a refusal there is a denial on the Mac.
3. If no MCP server with `plow_*` tools is connected, or it lists no
   `google-workspace` skill, Google is not available to this agent.
   Say exactly that — do not fall back to local OAuth.
