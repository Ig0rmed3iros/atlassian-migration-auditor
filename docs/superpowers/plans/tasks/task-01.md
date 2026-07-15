### Task 1: `auditor/client.py` — Jira client (PAT + OAuth behind one API)

**Files:**
- Create: `auditor/client.py`
- Test: `tests/test_client.py`

This ports the reference pipeline's `lib.py` to httpx with: PAT (Basic, direct site base) vs OAuth (Bearer, `https://api.atlassian.com/ex/jira/{cloudId}` base), 429 Retry-After, 5xx backoff, proactive + on-401 token refresh with **rotating refresh-token persistence**, cursor pagination (`/search/jql`), startAt pagination, servicedeskapi start/limit pagination, `adf_text`, `h16`.

- [ ] **Step 1: Write the failing tests**

`tests/test_client.py`:
```python
import json, httpx, pytest
from auditor.client import Connection, JiraClient, ClientError, adf_text, h16


def mk_pat(handler):
    conn = Connection(auth_type="pat", site_url="https://src.atlassian.net",
                      email="a@b.c", api_token="tok")
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return JiraClient(conn, http=http, sleeper=lambda s: None)


def mk_oauth(handler, **kw):
    conn = Connection(auth_type="oauth", site_url="https://src.atlassian.net",
                      cloud_id="cid-1", access_token="at-1", refresh_token="rt-1",
                      expires_at=9e12, **kw)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return JiraClient(conn, http=http, sleeper=lambda s: None), conn


def test_pat_base_and_basic_auth():
    seen = {}
    def handler(req):
        seen["url"] = str(req.url); seen["auth"] = req.headers.get("authorization", "")
        return httpx.Response(200, json={"ok": True})
    st, d = mk_pat(handler).req("/rest/api/3/myself")
    assert st == 200 and d == {"ok": True}
    assert seen["url"] == "https://src.atlassian.net/rest/api/3/myself"
    assert seen["auth"].startswith("Basic ")


def test_oauth_base_and_bearer():
    seen = {}
    def handler(req):
        seen["url"] = str(req.url); seen["auth"] = req.headers.get("authorization", "")
        return httpx.Response(200, json={})
    cl, _ = mk_oauth(handler)
    cl.req("/rest/api/3/myself")
    assert seen["url"] == "https://api.atlassian.com/ex/jira/cid-1/rest/api/3/myself"
    assert seen["auth"] == "Bearer at-1"


def test_429_honors_retry_after_then_succeeds():
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"}, json={})
        return httpx.Response(200, json={"done": 1})
    st, d = mk_pat(handler).req("/x")
    assert st == 200 and d == {"done": 1} and calls["n"] == 2


def test_5xx_retries_then_returns_error_dict():
    def handler(req):
        return httpx.Response(503, text="boom")
    st, d = mk_pat(handler).req("/x", tries=2)
    assert st == 503 and "_error" in d


def test_4xx_returns_immediately_no_retry():
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        return httpx.Response(404, text="nope")
    st, d = mk_pat(handler).req("/x")
    assert st == 404 and calls["n"] == 1


def test_oauth_refresh_on_401_persists_rotated_tokens():
    persisted = []
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        auth = req.headers.get("authorization")
        if auth == "Bearer at-1":
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"fresh": True})
    def refresh(rt):
        assert rt == "rt-1"
        return {"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600}
    cl, conn = mk_oauth(handler)
    conn.refresh_fn = refresh
    conn.on_tokens_refreshed = lambda c: persisted.append((c.access_token, c.refresh_token))
    st, d = cl.req("/rest/api/3/myself")
    assert st == 200 and d == {"fresh": True}
    assert conn.access_token == "at-2" and conn.refresh_token == "rt-2"   # ROTATED
    assert persisted == [("at-2", "rt-2")]


def test_search_jql_cursor_pagination():
    pages = [
        {"issues": [{"key": "A-1"}, {"key": "A-2"}], "nextPageToken": "t2"},
        {"issues": [{"key": "A-3"}], "isLast": True},
    ]
    def handler(req):
        body = json.loads(req.content)
        return httpx.Response(200, json=pages[1] if body.get("nextPageToken") else pages[0])
    keys = [i["key"] for i in mk_pat(handler).search_jql("project=A", ["summary"])]
    assert keys == ["A-1", "A-2", "A-3"]


def test_paginate_start_at_handles_values_envelope_and_plain_list():
    def handler(req):
        if "plain" in str(req.url):
            return httpx.Response(200, json=[{"name": "x"}])
        start = int(dict(req.url.params).get("startAt", 0))
        if start == 0:
            return httpx.Response(200, json={"values": [{"n": 1}], "isLast": False, "total": 2})
        return httpx.Response(200, json={"values": [{"n": 2}], "isLast": True, "total": 2})
    cl = mk_pat(handler)
    out, err = cl.paginate_start_at("/envelope")
    assert err is None and [r["n"] for r in out] == [1, 2]
    out2, err2 = cl.paginate_start_at("/plain")
    assert err2 is None and out2 == [{"name": "x"}]


def test_sd_list_start_limit_pagination():
    def handler(req):
        start = int(dict(req.url.params).get("start", 0))
        if start == 0:
            return httpx.Response(200, json={"values": [{"name": "q1"}], "isLastPage": False})
        return httpx.Response(200, json={"values": [{"name": "q2"}], "isLastPage": True})
    rows = mk_pat(handler).sd_list("/rest/servicedeskapi/servicedesk/1/queue")
    assert [r["name"] for r in rows] == ["q1", "q2"]


def test_approx_count_and_error_form():
    def handler(req):
        if b"bad" in req.content:
            return httpx.Response(400, text="bad jql")
        return httpx.Response(200, json={"count": 42})
    cl = mk_pat(handler)
    assert cl.approx_count("project=A") == 42
    assert cl.approx_count("bad") == "ERR400"


def test_myself_raises_client_error_on_failure():
    def handler(req):
        return httpx.Response(401, text="no")
    with pytest.raises(ClientError) as ei:
        mk_pat(handler).myself()
    assert ei.value.status == 401


def test_adf_text_and_h16():
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "Hello "},
            {"type": "mention", "attrs": {"text": "Igor"}},
            {"type": "hardBreak"},
            {"type": "inlineCard", "attrs": {"url": "https://x"}},
        ]}]}
    assert adf_text(doc) == "Hello @Igor\nhttps://x"
    assert len(h16("abc")) == 16 and h16("abc") == h16("abc") and h16(None) == h16("")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_client.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'auditor.client'`.

- [ ] **Step 3: Write the implementation**

`auditor/client.py`:
```python
"""Jira Cloud client used by every audit stage.

One Connection abstraction, two auth modes:
  - pat:   Basic email:api_token against https://<site>.atlassian.net
  - oauth: Bearer access token against https://api.atlassian.com/ex/jira/{cloudId}
           with proactive + on-401 refresh. Atlassian uses ROTATING refresh
           tokens: every refresh returns a NEW refresh_token which MUST be
           persisted (on_tokens_refreshed) or the connection dies.

Retry posture (ported from the reference pipeline's lib.py): 429 honors
Retry-After (capped 30s); 5xx/transport retried with linear backoff; other
4xx return immediately. `sleeper` is injectable so tests never sleep.
"""
from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

import httpx

GATEWAY = "https://api.atlassian.com"


class ClientError(RuntimeError):
    def __init__(self, msg: str, status: int = -1):
        super().__init__(msg)
        self.status = status


@dataclass
class Connection:
    auth_type: str                      # "pat" | "oauth"
    site_url: str
    email: str | None = None
    api_token: str | None = None
    cloud_id: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: float = 0.0
    on_tokens_refreshed: Callable[["Connection"], None] | None = field(
        default=None, repr=False, compare=False)
    refresh_fn: Callable[[str], dict] | None = field(
        default=None, repr=False, compare=False)

    @property
    def api_base(self) -> str:
        if self.auth_type == "oauth":
            return f"{GATEWAY}/ex/jira/{self.cloud_id}"
        return self.site_url.rstrip("/")

    def browse_url(self, key: str) -> str:
        return f"{self.site_url.rstrip('/')}/browse/{key}"


class JiraClient:
    def __init__(self, conn: Connection, http: httpx.Client | None = None,
                 sleeper: Callable[[float], None] = time.sleep):
        self.conn = conn
        self.http = http or httpx.Client(timeout=60.0)
        self.sleep = sleeper

    # ---------------------------------------------------------------- auth
    def _refresh(self) -> bool:
        c = self.conn
        if c.auth_type != "oauth" or not c.refresh_fn or not c.refresh_token:
            return False
        tok = c.refresh_fn(c.refresh_token)
        c.access_token = tok["access_token"]
        # Rotating refresh tokens: persist the NEW one every time.
        c.refresh_token = tok.get("refresh_token", c.refresh_token)
        c.expires_at = time.time() + float(tok.get("expires_in", 3600))
        if c.on_tokens_refreshed:
            c.on_tokens_refreshed(c)
        return True

    def _auth_header(self) -> str:
        c = self.conn
        if c.auth_type == "oauth":
            if c.expires_at and time.time() > c.expires_at - 60:
                self._refresh()
            return f"Bearer {c.access_token}"
        raw = f"{c.email}:{c.api_token}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    # ----------------------------------------------------------------- req
    def req(self, path: str, method: str = "GET", body=None, params=None,
            tries: int = 6) -> tuple[int, dict | list]:
        url = self.conn.api_base + path
        refreshed_once = False
        last_status, last_err = -1, "exhausted retries"
        for attempt in range(tries):
            headers = {"Authorization": self._auth_header(),
                       "Accept": "application/json"}
            try:
                resp = self.http.request(method, url, params=params,
                                         json=body, headers=headers)
            except httpx.HTTPError as ex:
                last_status, last_err = -1, str(ex)
                self.sleep(3 * (attempt + 1))
                continue
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "5")) + 1
                self.sleep(min(wait, 30))
                continue
            if resp.status_code == 401 and self.conn.auth_type == "oauth" \
                    and not refreshed_once:
                refreshed_once = True
                if self._refresh():
                    continue
            if resp.status_code in (500, 502, 503, 504):
                last_status, last_err = resp.status_code, resp.text[:400]
                self.sleep(3 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                return resp.status_code, {"_error": resp.text[:400]}
            if not resp.content or not resp.content.strip():
                return resp.status_code, {}
            return resp.status_code, resp.json()
        return last_status, {"_error": last_err}

    # ----------------------------------------------------------- paginators
    def search_jql(self, jql: str, fields: list[str], expand=None,
                   page: int = 100) -> Iterator[dict]:
        token = None
        while True:
            body = {"jql": jql, "maxResults": page, "fields": fields}
            if expand:
                body["expand"] = expand
            if token:
                body["nextPageToken"] = token
            st, d = self.req("/rest/api/3/search/jql", "POST", body)
            if st != 200:
                raise ClientError(f"search/jql {st}: {d.get('_error', '')}", st)
            yield from d.get("issues", [])
            token = d.get("nextPageToken")
            if d.get("isLast") or not token:
                break

    def paginate_start_at(self, path: str, params=None, key=None,
                          cap: int = 20000) -> tuple[list, str | None]:
        st, d = self.req(path, params={**(params or {}),
                                       "startAt": 0, "maxResults": 50})
        if st != 200:
            return [], f"ERR{st}:{str(d.get('_error', ''))[:60]}"
        if isinstance(d, list):
            return d, None
        arrkey = key or ("values" if "values" in d else next(
            (k for k in ("permissionSchemes", "notificationSchemes") if k in d),
            None))
        if arrkey is None:
            return [], None
        out = list(d.get(arrkey, []))
        is_last, total, start = d.get("isLast"), d.get("total"), len(out)
        while (not is_last and (total is None or start < total)
               and start < cap and d.get(arrkey)):
            st, d = self.req(path, params={**(params or {}),
                                           "startAt": start, "maxResults": 50})
            if st != 200:
                break
            chunk = d.get(arrkey, [])
            out += chunk
            start += len(chunk)
            is_last = d.get("isLast")
            if not chunk:
                break
        return out, None

    def sd_list(self, path: str) -> list:
        out, start = [], 0
        while True:
            st, d = self.req(path, params={"start": start, "limit": 50})
            if st != 200:
                return out
            vals = d.get("values", [])
            out += vals
            if d.get("isLastPage", True) or not vals:
                break
            start += len(vals)
            if start > 50000:
                break
        return out

    # ----------------------------------------------------------- shortcuts
    def approx_count(self, jql: str):
        st, d = self.req("/rest/api/3/search/approximate-count", "POST",
                         {"jql": jql})
        return d.get("count") if st == 200 else f"ERR{st}"

    def all_projects(self) -> tuple[list, str | None]:
        return self.paginate_start_at(
            "/rest/api/3/project/search",
            params={"expand": "description,lead,insight"})

    def myself(self) -> dict:
        st, d = self.req("/rest/api/3/myself")
        if st != 200:
            raise ClientError(f"/myself failed: {st} {d.get('_error', '')}", st)
        return d


# ------------------------------------------------------------------ helpers
def adf_text(node) -> str:
    out: list[str] = []

    def walk(n):
        if isinstance(n, dict):
            t = n.get("type")
            if t == "text":
                out.append(n.get("text", "") or "")
            elif t == "hardBreak":
                out.append("\n")
            elif t == "mention":
                out.append("@" + ((n.get("attrs") or {}).get("text", "") or ""))
            elif t == "emoji":
                out.append((n.get("attrs") or {}).get("shortName", "") or "")
            elif t == "inlineCard":
                out.append((n.get("attrs") or {}).get("url", "") or "")
            for c in (n.get("content") or []):
                walk(c)
        elif isinstance(n, list):
            for c in n:
                walk(c)

    walk(node)
    return "".join(out)


def h16(s: str | None) -> str:
    return hashlib.sha1((s or "").encode("utf-8", "replace")).hexdigest()[:16]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_client.py -q`
Expected: `12 passed`.

- [ ] **Step 5: Commit**

```bash
git add auditor/client.py tests/test_client.py
git commit -m "feat: Jira client with PAT/OAuth modes, rotating-refresh persistence, audit-grade retries"
```

---

## Post-review amendments (applied)

- sd_list fails loud (raises ClientError on any non-200 — a JSM outage must never read as an empty queue list)
- Retry-After date fallback (HTTP-date values fall back to 5s default instead of crashing with ValueError)
- Single-shot guarded refresh (proactive and reactive token refresh share one `refreshed_once` flag per req() call; failed refreshes degrade gracefully to the normal 401 path via `_refresh_safe()`)
- paginate_start_at ignores server total (loop condition now uses `isLast` as authoritative; stale server-reported totals no longer cause silent under-reads)
- 6 new tests: test_persistent_429_exhausts_and_fails_loud, test_transport_error_retries_then_fails_loud, test_401_refresh_is_single_shot_when_token_stays_bad, test_retry_after_http_date_falls_back_to_default_wait, test_failed_proactive_refresh_degrades_not_raises, test_sd_list_raises_on_error
