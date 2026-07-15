### Task 10: `webapp/oauth.py` — Atlassian OAuth 2.0 (3LO)

**Files:**
- Create: `webapp/oauth.py`
- Test: `tests/test_oauth.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_oauth.py`:
```python
import json
from urllib.parse import parse_qs, urlparse
import httpx
from webapp.oauth import (accessible_resources, build_authorize_url,
                          exchange_code, refresh_tokens)


def test_authorize_url_shape():
    url = build_authorize_url("cid", "http://localhost:8484/oauth/callback", "st8")
    p = urlparse(url)
    q = parse_qs(p.query)
    assert p.netloc == "auth.atlassian.com" and p.path == "/authorize"
    assert q["audience"] == ["api.atlassian.com"]
    assert q["client_id"] == ["cid"] and q["state"] == ["st8"]
    assert q["response_type"] == ["code"] and q["prompt"] == ["consent"]
    assert set(q["scope"][0].split()) == {"read:jira-work", "read:jira-user",
                                          "offline_access"}
    assert q["redirect_uri"] == ["http://localhost:8484/oauth/callback"]


def _http(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_exchange_code_posts_grant():
    seen = {}
    def handler(req):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"access_token": "at",
                                         "refresh_token": "rt",
                                         "expires_in": 3600})
    tok = exchange_code("cid", "sec", "the-code",
                        "http://localhost:8484/oauth/callback",
                        http=_http(handler))
    assert tok["access_token"] == "at"
    assert seen["url"] == "https://auth.atlassian.com/oauth/token"
    assert seen["body"]["grant_type"] == "authorization_code"
    assert seen["body"]["code"] == "the-code"


def test_refresh_tokens_posts_refresh_grant():
    def handler(req):
        body = json.loads(req.content)
        assert body["grant_type"] == "refresh_token"
        assert body["refresh_token"] == "rt-old"
        return httpx.Response(200, json={"access_token": "at2",
                                         "refresh_token": "rt-new",
                                         "expires_in": 3600})
    tok = refresh_tokens("cid", "sec", "rt-old", http=_http(handler))
    assert tok["refresh_token"] == "rt-new"


def test_exchange_raises_on_error():
    import pytest
    def handler(req):
        return httpx.Response(403, text="denied")
    with pytest.raises(RuntimeError):
        exchange_code("cid", "sec", "c", "r", http=_http(handler))


def test_accessible_resources_bearer():
    def handler(req):
        assert req.headers["authorization"] == "Bearer at"
        return httpx.Response(200, json=[
            {"id": "cloud-1", "url": "https://acme.atlassian.net",
             "name": "acme"}])
    sites = accessible_resources("at", http=_http(handler))
    assert sites[0]["id"] == "cloud-1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_oauth.py -q`
Expected: `ModuleNotFoundError: No module named 'webapp.oauth'`.

- [ ] **Step 3: Write the implementation**

`webapp/oauth.py`:
```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_oauth.py -q`
Expected: `5 passed`.

- [ ] **Step 5: Commit**

```bash
git add webapp/oauth.py tests/test_oauth.py
git commit -m "feat: Atlassian 3LO helpers (authorize, exchange, rotate-refresh, accessible-resources)"
```

---

