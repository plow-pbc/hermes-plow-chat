# Invite Owner Notification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the delight-triggered invite workflow notify an agent owner without opening generic cross-chat sends from non-owner turns.

**Architecture:** The Plow Chat plugin exposes one fixed-purpose owner-notification tool whose destination and content come from the active inbound event. The Plow invite skill invokes that tool, and agent-mgr atomically pins the merged plugin and skill revisions for fleet rollout.

**Tech Stack:** Python 3, pytest/pytest-asyncio, Hermes plugin API, Markdown skill instructions, agent-mgr pinned artifacts.

## Global Constraints

- Preserve `_send_guard` behavior for all generic sends.
- The tool accepts no destination or free-form message body.
- The server's invite-consent state remains the only consent authority.
- Every repository change uses a feature branch, explicit staging, a pushed PR, exact-head convergence, and its canonical test gate.
- Deployment updates the `~/services` runtime clone and verifies the owning container; no service points at a `~/Hacking` checkout.

---

### Task 1: Add the fixed-purpose Plow owner-notification tool

**Files:**
- Modify: `plow-chat-platform/__init__.py`
- Test: `tests/test_adapter.py`

**Interfaces:**
- Consumes: the active Plow `MessageEvent`, `PlowChatAdapter.home_chat_uid`, and the live adapter bridge.
- Produces: `plow_notify_owner_about_invite(kind: "consent_request" | "invite_created")`.

- [ ] **Step 1: Write failing adapter tests**

Add tests that start a non-owner turn and assert `consent_request` posts one
fixed notification to the home chat using event-derived participant, chat, and
normalized/capped praise. Add focused refusal tests for owner turns, missing
active turns, and unavailable live adapters. Extend the registration assertion
to include `plow_notify_owner_about_invite` and its enum-only schema.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uvx --with pytest-asyncio --with aiohttp pytest -q tests/test_adapter.py -k 'invite or group_send_tool_registers or send_uses_the_turn_chat'`

Expected: failures because the new tool, schema, and active-event fields do not exist; the existing generic-send guard test remains green.

- [ ] **Step 3: Implement the minimal plugin behavior**

Store event-derived `user_name`, `chat_name`, and `text` in the active-turn
record. Add an adapter coroutine that formats one of two fixed messages and
posts it directly to the granted home chat. Add the handler/schema/registration
bridge with an enum-only `kind` argument and explicit refusal results.

- [ ] **Step 4: Run focused and full verification**

Run the focused command from Step 2, then `just test`.

Expected: all tests pass with the existing cross-chat refusal assertion intact.

- [ ] **Step 5: Commit, push, open the plugin PR, and converge its exact head**

Stage only the spec, plan, adapter, and adapter test paths. Push
`fix/invite-consent-owner-notification`, open the PR, monitor checks and full
review, fix valid findings, and repeat after every pushed SHA until the current
head is converged.

### Task 2: Teach `plow-invite` to use the dedicated notification tool

**Files:**
- Modify: `cloud-agents/hermes/image/seed/skills/growth/plow-invite/SKILL.md`
- Test: `api/tests/test_plow_invite_skill.py`

**Interfaces:**
- Consumes: `plow_notify_owner_about_invite` from Task 1.
- Produces: explicit skill steps for `consent_request` and `invite_created`.

- [ ] **Step 1: Create a clean Plow feature branch from current `origin/main`**

Use an existing clean checkout, fetch first, and create
`fix/invite-consent-owner-notification` without resetting or stashing any work.

- [ ] **Step 2: Write failing skill-contract tests**

Assert that the skill names `plow_notify_owner_about_invite` and both exact
notification kinds, and no longer directs a generic cross-chat home send for
those steps.

- [ ] **Step 3: Run the focused test and verify RED**

Run: `uv run pytest api/tests/test_plow_invite_skill.py -q`

Expected: failure because the current skill only says to message the owner.

- [ ] **Step 4: Update the skill instructions minimally**

Name the dedicated tool in the `null`-consent branch and post-mint FYI step,
retain the same consent/mint rules, and increment the skill patch version.

- [ ] **Step 5: Run focused and canonical Plow gates**

Run the focused test, then the repository's canonical `just check` gate.

Expected: all checks pass.

- [ ] **Step 6: Commit, push, open the Plow PR, and converge its exact head**

Stage only the skill and its test. Monitor checks and review on every head until
the exact current SHA is converged.

### Task 3: Pin and deploy the converged artifacts

**Files:**
- Modify: `runtime/plow-chat-plugin.ref`
- Modify: `runtime/plow-invite-skill.ref`

**Interfaces:**
- Consumes: merged SHAs from Tasks 1 and 2.
- Produces: one fleet rollout revision containing both compatible artifacts.

- [ ] **Step 1: Create a clean agent-mgr feature branch from current `origin/main`**

Use the clean `/Users/so/Hacking/agent-mgr7` checkout after fetching and create
`chore/pin-invite-owner-notification`.

- [ ] **Step 2: Update both exact pins**

Set `runtime/plow-chat-plugin.ref` to the merged plugin SHA and
`runtime/plow-invite-skill.ref` to the merged Plow SHA.

- [ ] **Step 3: Run the canonical agent-mgr gate**

Run: `just check`

Expected: formatting, typing, and all tests pass.

- [ ] **Step 4: Commit, push, open the rollout PR, and converge its exact head**

Stage only the two pin files. Monitor checks and review until the current head
is converged, then merge with the authorization already supplied for this
deployment workflow.

- [ ] **Step 5: Deploy from the owning runtime clone**

On `odio@wakeup`, update `/home/odio/services/agent-mgr`, restore the life agent
through agent-mgr's canonical command, and do not edit installed files directly.

- [ ] **Step 6: Verify runtime readiness**

Confirm `hermes-life` is healthy, installed plugin and skill hashes match the
merged pins, the new tool registers, the consent status is still `null`, and
startup/runtime logs contain no new Plow Chat errors. Report the exact test
sequence: praise from the 703 participant, receive the owner opt-in ask, reply
yes, then observe the activation invitation in the original thread.
