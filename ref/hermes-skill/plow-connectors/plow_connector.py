#!/usr/bin/env python3
"""Plow connectors helper for Hermes — generic wrapper over the Plow connector
REST API. See `SKILL.md` for the action reference and examples.

`status` is a GET; every other action is a POST whose JSON body is the request.
Auth reuses the gateway's user Bearer token (`PLOW_CONNECTOR_TOKEN` else
`PLOW_CHAT_TOKEN`) against `PLOW_CHAT_BASE_URL` (default https://api.plow.co). A
non-2xx response is fatal: status + body to stderr, non-zero exit.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

CONNECTORS = ("gmail", "slack")
GET_ACTIONS = {"status"}
# A single connector action token (e.g. `status`, `messages.list`,
# `calendar.events.list`, `connect-code`). Rejecting anything else stops a
# prompted agent from smuggling `/`, `?`, or `..` into the URL path and reaching
# arbitrary bearer-authenticated Plow API routes through this helper.
ACTION_RE = re.compile(r"^[a-z0-9]+([._-][a-z0-9]+)*$")


def _env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def call(connector: str, action: str, body: str = "") -> str:
    if connector not in CONNECTORS:
        raise SystemExit(f"unknown connector {connector!r}; expected one of {', '.join(CONNECTORS)}")
    if not ACTION_RE.match(action):
        raise SystemExit(f"invalid action {action!r}; must be a single connector action token")

    token = _env("PLOW_CONNECTOR_TOKEN", "PLOW_CHAT_TOKEN")
    if not token:
        raise SystemExit("PLOW_CONNECTOR_TOKEN or PLOW_CHAT_TOKEN is required")
    base = _env("PLOW_CHAT_BASE_URL", default="https://api.plow.co").rstrip("/")

    method = "GET" if action in GET_ACTIONS else "POST"
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if method == "POST":
        payload = json.loads(body) if body.strip() else {}
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    url = f"{base}/v1/connectors/{connector}/{action}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=60) as resp:
        return resp.read().decode("utf-8")


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        raise SystemExit(__doc__)
    connector, action = argv[0], argv[1]
    body = argv[2] if len(argv) > 2 else ""
    try:
        resp = call(connector, action, body)
    except urllib.error.HTTPError as exc:  # fail loud — surface the API error verbatim
        detail = exc.read().decode("utf-8", "replace")[:1000]
        sys.stderr.write(f"HTTP {exc.code} {exc.reason}: {detail}\n")
        raise SystemExit(1)
    except (urllib.error.URLError, json.JSONDecodeError) as exc:  # transport / bad body — same fail-loud contract
        sys.stderr.write(f"error: {exc}\n")
        raise SystemExit(1)
    sys.stdout.write(resp if resp.endswith("\n") else resp + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
