"""Atlassian OAuth 2.0 (3LO) helpers.

The consent page at auth.atlassian.com itself offers Sign in with
Google/Microsoft — that is where those identities enter this product.
Scopes are read-only Jira + offline_access (refresh). Atlassian rotates
refresh tokens; callers must persist the new one after every refresh
(client.py's Connection handles that via refresh_fn wiring).
"""
from __future__ import annotations

from urllib.parse import urlencode

import httpx

AUTH_BASE = "https://auth.atlassian.com"
SCOPES = "read:jira-work read:jira-user offline_access"


def build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    q = urlencode({
        "audience": "api.atlassian.com",
        "client_id": client_id,
        "scope": SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
        "response_type": "code",
        "prompt": "consent",
    })
    return f"{AUTH_BASE}/authorize?{q}"


def _post_token(payload: dict, http: httpx.Client | None) -> dict:
    cl = http or httpx.Client(timeout=30.0)
    resp = cl.post(f"{AUTH_BASE}/oauth/token", json=payload)
    if resp.status_code != 200:
        raise RuntimeError(f"Atlassian token endpoint {resp.status_code}: "
                           f"{resp.text[:300]}")
    return resp.json()


def exchange_code(client_id: str, client_secret: str, code: str,
                  redirect_uri: str, http: httpx.Client | None = None) -> dict:
    return _post_token({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }, http)


def refresh_tokens(client_id: str, client_secret: str, refresh_token: str,
                   http: httpx.Client | None = None) -> dict:
    return _post_token({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }, http)


def accessible_resources(access_token: str,
                         http: httpx.Client | None = None) -> list[dict]:
    cl = http or httpx.Client(timeout=30.0)
    resp = cl.get("https://api.atlassian.com/oauth/token/accessible-resources",
                  headers={"Authorization": f"Bearer {access_token}"})
    if resp.status_code != 200:
        raise RuntimeError(f"accessible-resources {resp.status_code}: "
                           f"{resp.text[:300]}")
    return resp.json()
