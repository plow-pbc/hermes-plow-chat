# Copyright 2026 The Plow Collective, Inc
# SPDX-License-Identifier: Apache-2.0
"""What both Plow platforms share: the API base and credential, the granted
socket and its reconnect loop, the reach and identity reads, and the roster
readers. Policy -- roster prose, trust, disclosure, tools -- stays with the
platform that owns it (design §4).
"""
import asyncio
import logging
import os

import aiohttp

BASE = os.environ.get("PLOW_API_BASE", "https://api.plow.co").rstrip("/")
RECONNECT_SECONDS = 5
log = logging.getLogger(__name__)


class _PlowAuthError(Exception):
    """The credential itself was refused (401). Terminal: every retry presents
    the same revoked token, so the caller must stop, not sleep."""


def _auth_raise_for_status(resp):
    """The one status seam for every request that presents the credential.

    Status BEFORE parse (a proxy 401 is not JSON), and 401 ONLY -- a 403 is
    resource-scoped (removed from one chat) and keeps warn-and-retry.
    """
    if resp.status == 401:
        raise _PlowAuthError
    resp.raise_for_status()


def _bearer():
    return {"Authorization": "Bearer " + os.environ["PLOW_AGENT_TOKEN"]}


async def _granted_chats(http, auth):
    """The credential's chat listing, every provider, as `GET /v1/chats` serves it."""
    async with http.get(f"{BASE}/v1/chats", headers=auth) as resp:
        _auth_raise_for_status(resp)
        body = await resp.json(content_type=None)
    if body["has_more"]:
        raise RuntimeError("the granted chat listing is truncated")
    return body["data"]


async def _read_identity(http, auth):
    """`GET /v1/agents/cloud/me`: the signup block and this agent's number.

    200 answers. 404 is the documented "this token is not one agent" -- a
    wildcard or multi-line grant -- and answers None so the caller keeps what
    it holds. Anything else is not an answer about identity: through the
    credential seam (a 401 is terminal), then fail like the grant read so the
    caller retries rather than silently running without the offer.
    """
    async with http.get(f"{BASE}/v1/agents/cloud/me", headers=auth) as resp:
        if resp.status == 200:
            me = await resp.json(content_type=None)
            return {"signup": me.get("signup"), "number": (me.get("line") or {}).get("provider_key")}
        if resp.status == 404:
            return None
        _auth_raise_for_status(resp)
        raise RuntimeError(f"the identity read returned HTTP {resp.status}")
