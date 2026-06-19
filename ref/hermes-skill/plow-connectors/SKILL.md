---
name: plow-connectors
description: Use the owner's Plow-connected Google (Gmail + Google Calendar) and Slack accounts. Trigger when the user asks to read/search/send email, check or create calendar events, check free/busy, or read/search/post Slack messages and list Slack channels/users. Runs through the Plow connector REST API with the gateway's existing Bearer token.
allowed-tools: Bash(python3 /opt/data/skills/plow-connectors/plow_connector.py:*)
---

# Plow connectors (Gmail · Google Calendar · Slack)

The owner has connected Google and/or Slack to their Plow account. You act on
those accounts through one helper that calls the Plow connector REST API with
the gateway's existing user token — there is nothing to log in to.

```bash
python3 /opt/data/skills/plow-connectors/plow_connector.py <connector> <action> '<json>'
```

`<connector>` is `gmail` or `slack`. `status` is the only GET; every other
action takes a JSON body. The helper prints the JSON response and exits
non-zero on an API error (read stderr).

## Pagination

List actions (`messages.list`) return one page plus a `meta.next_cursor` when
more results exist. Pass `--paginate` before the connector to walk that opaque
cursor to completion and emit one merged `{"status":"ok","data":[...]}` with
every page concatenated:

```bash
python3 .../plow_connector.py --paginate gmail messages.list '{"query":"is:unread"}'   # walks all pages
```

The cursor is opaque — the helper echoes it back untouched and stops when the
cursor is exhausted. A 50-page safety cap bounds the walk; hitting it means the
set is larger than expected, so the helper **exits non-zero** rather than return
a partial result as success. Without `--paginate` you get a single page (pass the
previous response's `meta.next_cursor` as `"cursor"` to page by hand).

## First: check what's connected

```bash
python3 .../plow_connector.py gmail status     # {"connected":true,"account":"me@example.com",...}
python3 .../plow_connector.py slack status     # {"connected":false} means Slack isn't linked yet
```

If a connector reports `connected:false`, tell the user it isn't linked to
their Plow account yet — do not attempt actions on it. Send/modify actions
require the connector `account` from `status` (Gmail: the email address; Slack:
the workspace/team id).

## Gmail (Google)

| Action | Body fields |
| --- | --- |
| `messages.list` | `query`, `after_date`, `before_date`, `from_addresses[]`, `max_results`, `account?` |
| `messages.get` | `id`, `account?` |
| `messages.send` | `to[]`, `subject`, `body`, `cc[]?`, `bcc[]?`, `account` (required) |
| `messages.reply` | `id`, `body`, `account` |
| `messages.forward` | `id`, `to[]`, `body?`, `account` |
| `labels.list` | `account?` |

```bash
python3 .../plow_connector.py gmail messages.list '{"query":"is:unread","max_results":5}'
python3 .../plow_connector.py gmail messages.send '{"to":["a@b.com"],"subject":"Hi","body":"Hello","account":"me@example.com"}'
```

## Google Calendar (under the `gmail` connector)

| Action | Body fields |
| --- | --- |
| `calendar.list` | `account?` |
| `calendar.events.list` | `calendar_id?`, `time_min`, `time_max`, `query?`, `max_results`, `account?` |
| `calendar.events.create` | `calendar_id?`, `summary`, `start`, `end`, `attendees[]?`, `account?` |
| `calendar.freebusy` | `time_min`, `time_max`, `account?` |

```bash
python3 .../plow_connector.py gmail calendar.events.list '{"time_min":"2026-06-14T00:00:00Z","time_max":"2026-06-21T00:00:00Z","max_results":10}'
```

## Slack

| Action | Body fields |
| --- | --- |
| `channels.list` | `account` (required), `limit?` |
| `users.list` | `account`, `limit?` |
| `messages.list` | `account`, `channel_id`, `limit?` |
| `messages.search` | `account`, `query` |
| `messages.send` | `account`, `channel_id`, `text`, `thread_ts?` |

```bash
python3 .../plow_connector.py slack channels.list '{"account":"T0123"}'
python3 .../plow_connector.py slack messages.send '{"account":"T0123","channel_id":"C0123","text":"deploy is green"}'
```
