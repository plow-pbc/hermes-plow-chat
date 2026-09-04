# Review instructions — hermes-plow-chat

Repo-specific reviewer policy. The universal voice posture (Broken-Glass,
pro-simplification, and the don't-propose list) is supplied by the reviewers
themselves and is deliberately not restated here.

## Operating point

Pre-PMF, fewer than 10 users, often a single operator. Iteration speed beats
hardening for scale: prefer loud failures to fallbacks, pragmatic DRY
architecture to defensive layering, and don't guard edge cases that can't
trigger at this scale.

## What this repo is

**One Hermes platform plugin, and it is production.** `plow-chat-platform/` is
installed verbatim into every agent in the fleet by `plow-pbc/agent-mgr`, which
pins this repo by SHA. There is no reference/product distinction here any more —
this repo used to be a SEED, where `ref/` code was explicitly a single-operator
reference realization of a prose spec. That framing is retired along with the
`ref/` layout. The adapter is the shipped artifact.

`just test` is the gate. `README.md` is the only prose contract.

## Review priority

| DON'T (suppress / flag-as-shape) | DO (real finding) |
|---|---|
| Demand scale-hardening, extra flags, or defensive edge cases for a three-agent fleet. | Flag anything that can leave a **running agent with no working phone line** — a partial install, an unhandled reconnect path, a config the gateway reads only at boot. |
| Re-flag the missing checkpoint / history backfill. | Flag a **new** silent-loss path: an inbound message that can be dropped without a log line. |
| Treat README wording edits as churn. | Flag **README↔code drift** on the env-var contract in particular. `plugin.yaml` is the authority; the README table is a summary and the two must agree. |
| — | Flag any **literal secret**, or a probe that surfaces secret values (`env`/`printenv`, `cat` of credential files). This adapter holds the chat token. |
| — | Flag a change that alters the **plugin id** `plow-chat-platform`, or the installed file set, without a matching change in `agent-mgr`. Every agent's `config.yaml` names that id, and `agent-mgr`'s installer names that directory. |
| — | Flag **echoing a ticket, token or full frame** into logs. The ws ticket is single-use and short-lived, which is not the same as harmless. |
| — | Flag a change that a **sibling repo owns** per `plow-hermes-agent` README § The repos: the `plow-gog` / Latch tool grammar (latch owns it), per-chat state written under `$HERMES_HOME` instead of plow, boot and gateway config (the base image). The test is who else would have to change if the fact changed. |

## Product context

A Python Hermes platform adapter (`plow_chat`) plus its manifest. The suite
stubs the `gateway.*` modules Hermes supplies at runtime, so it needs no Hermes
install and touches no network.
