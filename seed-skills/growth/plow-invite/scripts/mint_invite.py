#!/usr/bin/env python3
"""Read or record the owner's delight-invite consent.

One source, two runtimes (converge, don't fork — see the twin note in this
skill's SKILL.md): baked into the hosted-agent image here, and installed into
every Docker-fleet agent home by plow-pbc/agent-mgr at its pinned SHA
(runtime/plow-invite-skill.ref). Auth + API base come from the environment in
both — exe's EnvironmentFile loads the tenant dotenv on hosted VMs, and the
fleet gateway's terminal children inherit /opt/data/.env (verified live) — so
this script must stay env-only and stdlib-only.
Prints {"enabled": ...} on success; exits non-zero with a one-line reason on
any failure. The durable opportunity API owns invite preparation and delivery.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--consent",
        choices=["granted", "declined", "status"],
        required=True,
        help="query or record the owner's answer",
    )
    args = parser.parse_args()

    base = os.environ.get("PLOW_API_BASE")
    token = os.environ.get("PLOW_AGENT_TOKEN")
    if not base or not token:
        print("unauthorized: missing PLOW_API_BASE or PLOW_AGENT_TOKEN", file=sys.stderr)
        return 2

    if args.consent == "status":
        path, method, body = "/v1/auth/agent-invites", "GET", None
    else:
        path, method = "/v1/auth/agent-invites", "PUT"
        body = {"enabled": args.consent == "granted"}

    req = urllib.request.Request(
        f"{base.rstrip('/')}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as err:
        reason = {401: "unauthorized", 403: "not_enabled", 429: "rate_capped", 503: "no_line_available"}.get(
            err.code, "api_error"
        )
        print(f"{reason}: HTTP {err.code}", file=sys.stderr)
        return 3
    except (urllib.error.URLError, TimeoutError) as err:
        print(f"api_error: {err}", file=sys.stderr)
        return 3

    print(json.dumps({"enabled": data["enabled"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
