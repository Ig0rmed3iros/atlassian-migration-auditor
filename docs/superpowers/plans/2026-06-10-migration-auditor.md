# Migration Auditor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local-first FastAPI web app that audits Jira Cloud→Cloud migrations end-to-end (connect via Atlassian OAuth or PAT → scope → permission blind-spots → extract → compare → config parity) and serves the results as an in-app, multi-page interactive analysis. No PDF/static report.

**Architecture:** Pure core library `auditor/` (Jira client, scope matching, blind-spot detection, content-fingerprint extraction, fidelity compare, config audit, findings normalization — no web imports, all I/O via injected client/paths/callbacks) wrapped by `webapp/` (FastAPI, SQLite store with Fernet-encrypted secrets, threaded run engine with SSE progress, JSON analysis API, Jinja + vanilla-JS broadsheet UI). Spec: `docs/superpowers/specs/2026-06-10-migration-auditor-design.md`. Reference implementation being ported: the prior migration-audit pipeline (lib.py, extract_core.py, compare.py, config_audit.py, config_fix.py, grant_admin.py).

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, httpx, Jinja2, sqlite3 (stdlib), cryptography (Fernet), pytest. No JS build step, no chart library (CSS donuts/bars).

**Working directory for ALL tasks:** `/mnt/d/Atlassian-Products/Migration-auditor`

---

## Shared interfaces (single source of truth — later tasks MUST match these exactly)

```python
# auditor/client.py
@dataclass
class Connection:
    auth_type: str                    # "pat" | "oauth"
    site_url: str                     # https://acme.atlassian.net (always set; display + browse links)
    email: str | None = None          # pat only
    api_token: str | None = None      # pat only
    cloud_id: str | None = None       # oauth only
    access_token: str | None = None   # oauth only
    refresh_token: str | None = None  # oauth only
    expires_at: float = 0.0           # oauth only (unix ts)
    on_tokens_refreshed: "Callable[[Connection], None] | None" = None
    refresh_fn: "Callable[[str], dict] | None" = None  # refresh_token -> token dict

class JiraClient:
    def __init__(self, conn: Connection, http: httpx.Client | None = None,
                 sleeper: Callable[[float], None] = time.sleep): ...
    def req(self, path, method="GET", body=None, params=None, tries=6) -> tuple[int, dict|list]
    def paginate_start_at(self, path, params=None, key=None, cap=20000) -> tuple[list, str|None]
    #   Returns a NON-None error string on ANY page failure (first-page OR mid-loop truncation):
    #   a non-200 on page 2+ yields (rows_so_far, "ERR<status>:truncated") — never silent truncation.
    def search_jql(self, jql, fields, expand=None, page=100) -> Iterator[dict]
    def approx_count(self, jql) -> int | str        # "ERR<status>" on failure
    def all_projects(self) -> tuple[list, str|None]
    def sd_list(self, path) -> list                  # servicedeskapi pagination; raises ClientError on failure
    def myself(self) -> dict                         # raises ClientError on failure

def adf_text(node) -> str
def h16(s: str | None) -> str
class ClientError(RuntimeError): ...                 # carries .status

# auditor/scope.py
def match_projects(src_projects: list, tgt_projects: list) -> dict
# -> {"matched":[{"key","name","src_id","tgt_id","src_count","tgt_count"}],
#     "source_only":[{"key","name"}], "target_only":[{"key","name"}]}
# (counts filled by caller via approx_count; match_projects sets them to None)

# auditor/permissions.py
def detect_blind_spots(client: JiraClient, project_keys: list[str]) -> list[dict]
# -> [{"key","search_count","insight_count","blind_spot": bool,"indeterminate": bool}]
def find_admin_role_id(client) -> int | None
def apply_elevation(client, project_ids: list[str], role_id: int, account_id: str) -> list[dict]
# -> grant log rows: {"project_id","status","ok","added"[,"error"]}; undo only removes added=True
def undo_elevation(client, grants: list[dict], role_id: int, account_id: str) -> list[dict]

# auditor/extract.py
CORE_FIELDS: list[str]      # base fields, NO instance-specific customfields
def slim(issue: dict) -> dict
def extract_project(client, project_key, out_path, extra_fields=(), progress=None) -> dict
# -> {"extracted": n, "approx": ac, "verified": bool}

# auditor/compare.py
def compare_project(project: str, src_path: str, tgt_path: str) -> dict
# -> {"stats": {...}, "findings": [finding, ...]}
# finding: {"project","kind","src_key","tgt_key","field","summary","detail"}  (detail is a dict)
# kinds: missing_in_tgt | missing_in_src | tail_post_cutover | field_mismatch |
#        content_mismatch | comment_mismatch | comment_uncheckable |
#        attachment_mismatch | link_mismatch | key_collision
#        ; fidelity_pct is None when no issues were compared; tails require common>0

# auditor/config_audit.py
def audit_config(src: JiraClient, tgt: JiraClient, jsm_projects=(), progress=None) -> dict
# -> {"areas": {area: {"src":n,"tgt":n,"in_both":n,...}}, "findings":[config_finding,...]}
# config_finding: {"area","name","kind","detail"}
# kinds: missing_in_tgt | type_mismatch | option_mismatch | structure_mismatch | field_mismatch | area_error
#   area_error: a side (source|target) was unreachable/unauthorized for this area; detail={"side","error"}.
#   Fail-loud: NEVER rendered as a clean "0 issues" — verdict MUST treat it as at least GAPS_FOUND.

# auditor/findings.py
def build_run_summary(project_results: dict, config_result: dict, blind_spots: list) -> dict
# -> {"stats": {...}, "verdict": str, "headlines": [str, ...]}
# verdict: "CLEAN" | "CLEAN_WITH_TAILS" | "GAPS_FOUND" | "CRITICAL"

# webapp/store.py
class Store:                          # all methods synchronous; sqlite3 with check_same_thread=False
    def __init__(self, db_path: str, key_path: str, secret_key: str | None = None): ...
    # settings_get/settings_set, create_migration/list_migrations/get_migration,
    # save_connection/get_connection(migration_id, role)/connection_secret(conn_row),
    # create_run/update_run/get_run/list_runs/active_run(migration_id),
    # set_run_projects/get_run_projects, insert_findings_issue/insert_findings_config,
    # query_issues(run_id, project=None, kind=None, q=None, page=1, size=50) -> (rows, total),
    # config_areas(run_id), query_config(run_id, area),
    # add_event/get_events(run_id, after_id=0), encrypt(dict)->bytes, decrypt(bytes)->dict

# webapp/runs.py
PHASES = ["verify", "scope", "permissions", "extract", "compare", "config", "finalize"]
class RunEngine:
    def __init__(self, store: Store, workspace_root: str, stages: dict | None = None): ...
    def start(self, migration_id: int, params: dict) -> int     # run_id; raises if active run
    # params may carry {"reuse_extracts_from": <run_id>} -> the new run reuses that
    # run's workspace and stage_extract skips projects whose gz files already exist
    def cancel(self, run_id: int) -> None
    def mark_stale_failed(self) -> int
```

URL map (webapp): `GET /` dashboard · `GET|POST /settings` · `POST /migrations` · `GET /migrations/{id}` · `POST /migrations/{id}/connections` (PAT form) · `GET /oauth/start` + `GET /oauth/callback` (3LO) · `POST /migrations/{id}/scope` (preview) · `POST /migrations/{id}/runs` · `GET /runs/{id}` (live page) · `GET /runs/{id}/stream` (SSE) · `POST /runs/{id}/cancel` · analysis pages `GET /runs/{id}/analysis[/projects|/projects/{key}|/config|/issues|/log]` · JSON `GET /api/runs/{id}/summary|projects|issues|config|events`.

---

### Task 0: Scaffold the package

**Files:**
- Create: `pyproject.toml`, `pytest.ini`, `auditor/__init__.py`, `webapp/__init__.py`, `tests/__init__.py`, `README.md`

- [ ] **Step 1: Write the files**

`pyproject.toml`:
```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "migration-auditor"
version = "0.1.0"
description = "Audit Jira Cloud-to-Cloud migrations: issue fidelity, config parity, interactive analysis."
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.110",
  "uvicorn>=0.29",
  "httpx>=0.27",
  "jinja2>=3.1",
  "cryptography>=42",
  "python-multipart>=0.0.9",
]

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
migration-auditor = "webapp.main:cli"

[tool.setuptools]
packages = ["auditor", "webapp"]

[tool.setuptools.package-data]
webapp = ["templates/*.html", "static/*"]
```

`pytest.ini`:
```ini
[pytest]
testpaths = tests
addopts = -q
```

`auditor/__init__.py`, `webapp/__init__.py`, `tests/__init__.py`: empty files.

`README.md`:
```markdown
# Migration Auditor

Local web app that audits Jira Cloud -> Jira Cloud migrations: issue-data
fidelity (every issue, content fingerprints), config parity, permission
blind-spot detection — rendered as an in-app interactive analysis.

## Quickstart

    pip install -e .[dev]
    migration-auditor serve          # -> http://127.0.0.1:8484

Connect source and target with **Atlassian OAuth** (Settings -> register a
client first; see below) or a **PAT** (site URL + email + API token from
https://id.atlassian.com/manage-profile/security/api-tokens).

## Registering your own Atlassian OAuth app (optional, for the OAuth path)

1. Go to https://developer.atlassian.com/console/myapps -> Create -> OAuth 2.0 integration.
2. Add the **Jira API** with scopes: `read:jira-work`, `read:jira-user`, `offline_access`.
3. Set callback URL: `http://localhost:8484/oauth/callback`.
4. Copy the Client ID and Secret into the app's Settings page.

## Configuration (env)

| Var | Default | Purpose |
|---|---|---|
| `MA_DATA_DIR` | `./data` | SQLite DB + run workspaces |
| `MA_BIND` | `127.0.0.1:8484` | listen address |
| `MA_PUBLIC_BASE_URL` | `http://localhost:8484` | OAuth callback base |
| `MA_SECRET_KEY` | auto-keyfile `data/.key` | Fernet key for secrets at rest |

Extracted issue data contains customer content. It stays under `MA_DATA_DIR`
(gitignored). Do not commit or share it.
```

- [ ] **Step 2: Configure git identity + install**

```bash
cd /mnt/d/Atlassian-Products/Migration-auditor
git config user.name "Igor Medeiros" && git config user.email "dev@example.com"
python3 -m pip install -e .[dev] 2>&1 | tail -2
```
Expected: `Successfully installed migration-auditor-0.1.0` (deps may already be satisfied).

- [ ] **Step 3: Sanity check pytest collects nothing but runs**

Run: `python3 -m pytest`
Expected: `no tests ran` (exit code 5 is fine at this point).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml pytest.ini auditor/__init__.py webapp/__init__.py tests/__init__.py README.md
git commit -m "chore: scaffold migration-auditor package"
```

---

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

### Task 2: `webapp/config.py` — environment configuration

**Files:**
- Create: `webapp/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_config.py`:
```python
from webapp.config import load_config


def test_defaults(tmp_path, monkeypatch):
    for v in ("MA_DATA_DIR", "MA_BIND", "MA_PUBLIC_BASE_URL", "MA_SECRET_KEY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.data_dir.endswith("data")
    assert cfg.bind_host == "127.0.0.1" and cfg.bind_port == 8484
    assert cfg.public_base_url == "http://localhost:8484"
    assert cfg.oauth_redirect_uri == "http://localhost:8484/oauth/callback"
    assert cfg.secret_key is None


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("MA_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("MA_BIND", "0.0.0.0:9000")
    monkeypatch.setenv("MA_PUBLIC_BASE_URL", "https://audit.example.com/")
    monkeypatch.setenv("MA_SECRET_KEY", "k" * 44)
    cfg = load_config()
    assert cfg.data_dir == str(tmp_path / "d")
    assert cfg.bind_host == "0.0.0.0" and cfg.bind_port == 9000
    assert cfg.public_base_url == "https://audit.example.com"
    assert cfg.oauth_redirect_uri == "https://audit.example.com/oauth/callback"
    assert cfg.secret_key == "k" * 44
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_config.py -q`
Expected: `ModuleNotFoundError: No module named 'webapp.config'`.

- [ ] **Step 3: Write the implementation**

`webapp/config.py`:
```python
"""Env-driven configuration (the hosting-ready seam). All MA_* variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    data_dir: str
    bind_host: str
    bind_port: int
    public_base_url: str
    secret_key: str | None

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "auditor.db")

    @property
    def key_path(self) -> str:
        return os.path.join(self.data_dir, ".key")

    @property
    def oauth_redirect_uri(self) -> str:
        return f"{self.public_base_url}/oauth/callback"


def load_config() -> Config:
    data_dir = os.environ.get("MA_DATA_DIR") or os.path.join(os.getcwd(), "data")
    bind = os.environ.get("MA_BIND", "127.0.0.1:8484")
    host, _, port = bind.rpartition(":")
    public = os.environ.get("MA_PUBLIC_BASE_URL", "http://localhost:8484")
    return Config(
        data_dir=data_dir,
        bind_host=host or "127.0.0.1",
        bind_port=int(port),
        public_base_url=public.rstrip("/"),
        secret_key=os.environ.get("MA_SECRET_KEY") or None,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_config.py -q`
Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add webapp/config.py tests/test_config.py
git commit -m "feat: env-driven config with hosting-ready MA_* variables"
```

---

### Task 3: `webapp/store.py` — SQLite store + encrypted secrets

**Files:**
- Create: `webapp/store.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_store.py`:
```python
import json
import pytest
from webapp.store import Store


@pytest.fixture()
def store(tmp_path):
    return Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))


def test_secret_roundtrip_and_keyfile_created(tmp_path):
    s = Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))
    blob = s.encrypt({"email": "a@b.c", "token": "shh"})
    assert b"shh" not in blob
    assert s.decrypt(blob) == {"email": "a@b.c", "token": "shh"}
    assert (tmp_path / ".key").exists()
    # a second Store with the same keyfile can decrypt
    s2 = Store(db_path=str(tmp_path / "t2.db"), key_path=str(tmp_path / ".key"))
    assert s2.decrypt(blob)["token"] == "shh"


def test_explicit_secret_key_skips_keyfile(tmp_path):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    s = Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"),
              secret_key=key)
    assert s.decrypt(s.encrypt({"x": 1})) == {"x": 1}
    assert not (tmp_path / ".key").exists()


def test_settings_roundtrip(store):
    assert store.settings_get("oauth_client_id") is None
    store.settings_set("oauth_client_id", "abc")
    store.settings_set("oauth_client_id", "abc2")
    assert store.settings_get("oauth_client_id") == "abc2"


def test_migration_and_connection_crud(store):
    mid = store.create_migration("acme->globex")
    assert store.get_migration(mid)["name"] == "acme->globex"
    assert [m["id"] for m in store.list_migrations()] == [mid]
    store.save_connection(mid, "source", "pat", "https://src.atlassian.net",
                          secret={"email": "a@b.c", "token": "t1"},
                          account_email="a@b.c")
    store.save_connection(mid, "source", "pat", "https://src2.atlassian.net",
                          secret={"email": "a@b.c", "token": "t2"},
                          account_email="a@b.c")          # upsert by (mig, role)
    row = store.get_connection(mid, "source")
    assert row["site_url"] == "https://src2.atlassian.net"
    assert store.connection_secret(row)["token"] == "t2"
    assert store.get_connection(mid, "target") is None


def test_run_lifecycle_and_active_guard(store):
    mid = store.create_migration("m")
    rid = store.create_run(mid, {"projects": ["AC"]})
    assert store.active_run(mid)["id"] == rid
    store.update_run(rid, status="done", verdict="CLEAN",
                     stats={"issues": 5}, phase="finalize")
    r = store.get_run(rid)
    assert r["status"] == "done" and r["verdict"] == "CLEAN"
    assert json.loads(r["stats_json"])["issues"] == 5
    assert store.active_run(mid) is None
    assert len(store.list_runs(mid)) == 1


def test_run_projects_roundtrip(store):
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    store.set_run_projects(rid, [
        {"key": "AC", "name": "AC Support", "src_count": 100, "tgt_count": 95,
         "missing": 5, "tail_count": 5, "fidelity_pct": 99.0,
         "blind_spot": 0, "status": "done"}])
    rows = store.get_run_projects(rid)
    assert rows[0]["key"] == "AC" and rows[0]["tgt_count"] == 95


def test_findings_issue_pagination_and_filters(store):
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    rows = []
    for i in range(120):
        rows.append({"project": "AC", "kind": "missing_in_tgt",
                     "src_key": f"AC-{i}", "tgt_key": None, "field": None,
                     "summary": f"issue {i}", "detail": {"n": i}})
    rows.append({"project": "MS", "kind": "field_mismatch", "src_key": "MS-1",
                 "tgt_key": "MS-1", "field": "status",
                 "summary": "status differs", "detail": {}})
    store.insert_findings_issue(rid, rows)
    page1, total = store.query_issues(rid, page=1, size=50)
    assert total == 121 and len(page1) == 50
    only_ms, t2 = store.query_issues(rid, project="MS")
    assert t2 == 1 and only_ms[0]["field"] == "status"
    hits, t3 = store.query_issues(rid, q="issue 11")
    assert t3 >= 1 and all("issue 11" in h["summary"] for h in hits)
    byk, t4 = store.query_issues(rid, kind="field_mismatch")
    assert t4 == 1


def test_findings_config_and_areas(store):
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    store.insert_findings_config(rid, [
        {"area": "statuses", "name": "On Hold", "kind": "missing_in_tgt", "detail": {}},
        {"area": "custom_fields", "name": "Squad", "kind": "missing_in_tgt", "detail": {}},
    ])
    assert set(store.config_areas(rid)) == {"statuses", "custom_fields"}
    rows = store.query_config(rid, "statuses")
    assert rows[0]["name"] == "On Hold"


def test_events_stream(store):
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    store.add_event(rid, "extract", "info", "AC 100/200")
    store.add_event(rid, "extract", "info", "AC 200/200")
    evs = store.get_events(rid)
    assert len(evs) == 2
    later = store.get_events(rid, after_id=evs[0]["id"])
    assert len(later) == 1 and later[0]["message"] == "AC 200/200"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_store.py -q`
Expected: `ModuleNotFoundError: No module named 'webapp.store'`.

- [ ] **Step 3: Write the implementation**

`webapp/store.py`:
```python
"""SQLite persistence + Fernet-encrypted secrets.

Single-file DB under MA_DATA_DIR. All methods synchronous; the connection is
created with check_same_thread=False because the run engine thread and the
request threads share the Store (sqlite serializes writes itself; our writes
are short transactions).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from cryptography.fernet import Fernet

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS migrations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS connections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  migration_id INTEGER NOT NULL REFERENCES migrations(id),
  role TEXT NOT NULL CHECK(role IN ('source','target')),
  auth_type TEXT NOT NULL CHECK(auth_type IN ('oauth','pat')),
  site_url TEXT NOT NULL, cloud_id TEXT, account_email TEXT,
  secret_enc BLOB NOT NULL, status TEXT DEFAULT 'new', verified_at REAL,
  UNIQUE(migration_id, role));
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  migration_id INTEGER NOT NULL REFERENCES migrations(id),
  status TEXT NOT NULL DEFAULT 'running',
  phase TEXT DEFAULT 'verify', verdict TEXT,
  started_at REAL NOT NULL, finished_at REAL,
  params_json TEXT NOT NULL DEFAULT '{}',
  stats_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS run_projects (
  run_id INTEGER NOT NULL REFERENCES runs(id),
  key TEXT NOT NULL, name TEXT, src_count INTEGER, tgt_count INTEGER,
  missing INTEGER, tail_count INTEGER, fidelity_pct REAL,
  blind_spot INTEGER DEFAULT 0, status TEXT,
  PRIMARY KEY (run_id, key));
CREATE TABLE IF NOT EXISTS findings_issue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  project TEXT NOT NULL, kind TEXT NOT NULL,
  src_key TEXT, tgt_key TEXT, field TEXT, summary TEXT,
  detail_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS ix_fi ON findings_issue (run_id, project, kind);
CREATE TABLE IF NOT EXISTS findings_config (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  area TEXT NOT NULL, name TEXT, kind TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS ix_fc ON findings_config (run_id, area);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  ts REAL NOT NULL, phase TEXT, level TEXT DEFAULT 'info', message TEXT);
CREATE INDEX IF NOT EXISTS ix_ev ON events (run_id, id);
"""


class Store:
    def __init__(self, db_path: str, key_path: str, secret_key: str | None = None):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        if secret_key:
            key = secret_key.encode()
        elif os.path.exists(key_path):
            key = open(key_path, "rb").read()
        else:
            key = Fernet.generate_key()
            with open(key_path, "wb") as fh:
                fh.write(key)
            os.chmod(key_path, 0o600)
        self._fernet = Fernet(key)

    # ------------------------------------------------------------- secrets
    def encrypt(self, data: dict) -> bytes:
        return self._fernet.encrypt(json.dumps(data).encode())

    def decrypt(self, blob: bytes) -> dict:
        return json.loads(self._fernet.decrypt(bytes(blob)))

    # -------------------------------------------------------------- helpers
    def _exec(self, sql, args=()):
        with self._lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def _rows(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _row(self, sql, args=()):
        r = self.db.execute(sql, args).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------- settings
    def settings_get(self, key: str) -> str | None:
        r = self._row("SELECT value FROM settings WHERE key=?", (key,))
        return r["value"] if r else None

    def settings_set(self, key: str, value: str) -> None:
        self._exec("INSERT INTO settings(key,value) VALUES(?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, value))

    def settings_delete(self, key: str) -> None:
        self._exec("DELETE FROM settings WHERE key=?", (key,))

    # ----------------------------------------------------------- migrations
    def create_migration(self, name: str) -> int:
        return self._exec("INSERT INTO migrations(name,created_at) VALUES(?,?)",
                          (name, time.time())).lastrowid

    def list_migrations(self) -> list[dict]:
        return self._rows("SELECT * FROM migrations ORDER BY id DESC")

    def get_migration(self, mid: int) -> dict | None:
        return self._row("SELECT * FROM migrations WHERE id=?", (mid,))

    # ----------------------------------------------------------- connections
    def save_connection(self, migration_id: int, role: str, auth_type: str,
                        site_url: str, secret: dict, cloud_id: str | None = None,
                        account_email: str | None = None) -> int:
        return self._exec(
            "INSERT INTO connections(migration_id,role,auth_type,site_url,"
            "cloud_id,account_email,secret_enc) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(migration_id,role) DO UPDATE SET auth_type=excluded.auth_type,"
            "site_url=excluded.site_url,cloud_id=excluded.cloud_id,"
            "account_email=excluded.account_email,secret_enc=excluded.secret_enc,"
            "status='new',verified_at=NULL",
            (migration_id, role, auth_type, site_url, cloud_id, account_email,
             self.encrypt(secret))).lastrowid

    def get_connection(self, migration_id: int, role: str) -> dict | None:
        return self._row("SELECT * FROM connections WHERE migration_id=? AND role=?",
                         (migration_id, role))

    def connection_secret(self, conn_row: dict) -> dict:
        return self.decrypt(conn_row["secret_enc"])

    def update_connection_secret(self, conn_id: int, secret: dict) -> None:
        self._exec("UPDATE connections SET secret_enc=? WHERE id=?",
                   (self.encrypt(secret), conn_id))

    def mark_connection_verified(self, conn_id: int, account_email: str) -> None:
        self._exec("UPDATE connections SET status='verified',verified_at=?,"
                   "account_email=? WHERE id=?",
                   (time.time(), account_email, conn_id))

    # ----------------------------------------------------------------- runs
    def create_run(self, migration_id: int, params: dict) -> int:
        return self._exec(
            "INSERT INTO runs(migration_id,started_at,params_json) VALUES(?,?,?)",
            (migration_id, time.time(), json.dumps(params))).lastrowid

    def update_run(self, run_id: int, *, status=None, phase=None, verdict=None,
                   stats: dict | None = None, finished: bool = False) -> None:
        sets, args = [], []
        if status is not None:
            sets.append("status=?"); args.append(status)
        if phase is not None:
            sets.append("phase=?"); args.append(phase)
        if verdict is not None:
            sets.append("verdict=?"); args.append(verdict)
        if stats is not None:
            sets.append("stats_json=?"); args.append(json.dumps(stats, default=str))
        if finished or status in ("done", "failed", "cancelled"):
            sets.append("finished_at=?"); args.append(time.time())
        if sets:
            args.append(run_id)
            self._exec(f"UPDATE runs SET {','.join(sets)} WHERE id=?", args)

    def get_run(self, run_id: int) -> dict | None:
        return self._row("SELECT * FROM runs WHERE id=?", (run_id,))

    def list_runs(self, migration_id: int) -> list[dict]:
        return self._rows("SELECT * FROM runs WHERE migration_id=? ORDER BY id DESC",
                          (migration_id,))

    def active_run(self, migration_id: int) -> dict | None:
        return self._row("SELECT * FROM runs WHERE migration_id=? AND "
                         "status='running' ORDER BY id DESC LIMIT 1",
                         (migration_id,))

    def stale_running(self) -> list[dict]:
        return self._rows("SELECT * FROM runs WHERE status='running'")

    # --------------------------------------------------------- run projects
    def set_run_projects(self, run_id: int, rows: list[dict]) -> None:
        with self._lock:
            for r in rows:
                self.db.execute(
                    "INSERT INTO run_projects(run_id,key,name,src_count,tgt_count,"
                    "missing,tail_count,fidelity_pct,blind_spot,status) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,key) DO UPDATE SET "
                    "name=excluded.name,src_count=excluded.src_count,"
                    "tgt_count=excluded.tgt_count,missing=excluded.missing,"
                    "tail_count=excluded.tail_count,fidelity_pct=excluded.fidelity_pct,"
                    "blind_spot=excluded.blind_spot,status=excluded.status",
                    (run_id, r["key"], r.get("name"), r.get("src_count"),
                     r.get("tgt_count"), r.get("missing"), r.get("tail_count"),
                     r.get("fidelity_pct"), int(r.get("blind_spot") or 0),
                     r.get("status")))
            self.db.commit()

    def get_run_projects(self, run_id: int) -> list[dict]:
        return self._rows("SELECT * FROM run_projects WHERE run_id=? ORDER BY key",
                          (run_id,))

    # ------------------------------------------------------------- findings
    def insert_findings_issue(self, run_id: int, rows: list[dict]) -> None:
        with self._lock:
            self.db.executemany(
                "INSERT INTO findings_issue(run_id,project,kind,src_key,tgt_key,"
                "field,summary,detail_json) VALUES(?,?,?,?,?,?,?,?)",
                [(run_id, r["project"], r["kind"], r.get("src_key"),
                  r.get("tgt_key"), r.get("field"), r.get("summary"),
                  json.dumps(r.get("detail") or {}, default=str)) for r in rows])
            self.db.commit()

    def query_issues(self, run_id: int, project=None, kind=None, q=None,
                     page: int = 1, size: int = 50) -> tuple[list[dict], int]:
        where, args = ["run_id=?"], [run_id]
        if project:
            where.append("project=?"); args.append(project)
        if kind:
            where.append("kind=?"); args.append(kind)
        if q:
            where.append("(summary LIKE ? OR src_key LIKE ? OR tgt_key LIKE ? "
                         "OR field LIKE ?)")
            like = f"%{q}%"
            args += [like, like, like, like]
        w = " AND ".join(where)
        total = self.db.execute(
            f"SELECT COUNT(*) c FROM findings_issue WHERE {w}", args).fetchone()["c"]
        rows = self._rows(
            f"SELECT * FROM findings_issue WHERE {w} ORDER BY id "
            f"LIMIT ? OFFSET ?", args + [size, (page - 1) * size])
        return rows, total

    def issue_kind_counts(self, run_id: int, project=None) -> dict:
        where, args = ["run_id=?"], [run_id]
        if project:
            where.append("project=?"); args.append(project)
        rows = self._rows(
            f"SELECT kind, COUNT(*) c FROM findings_issue WHERE "
            f"{' AND '.join(where)} GROUP BY kind", args)
        return {r["kind"]: r["c"] for r in rows}

    def insert_findings_config(self, run_id: int, rows: list[dict]) -> None:
        with self._lock:
            self.db.executemany(
                "INSERT INTO findings_config(run_id,area,name,kind,detail_json) "
                "VALUES(?,?,?,?,?)",
                [(run_id, r["area"], r.get("name"), r["kind"],
                  json.dumps(r.get("detail") or {}, default=str)) for r in rows])
            self.db.commit()

    def config_areas(self, run_id: int) -> list[str]:
        return [r["area"] for r in self._rows(
            "SELECT DISTINCT area FROM findings_config WHERE run_id=? ORDER BY area",
            (run_id,))]

    def query_config(self, run_id: int, area: str) -> list[dict]:
        return self._rows("SELECT * FROM findings_config WHERE run_id=? AND area=? "
                          "ORDER BY id", (run_id, area))

    # --------------------------------------------------------------- events
    def add_event(self, run_id: int, phase: str, level: str, message: str) -> None:
        self._exec("INSERT INTO events(run_id,ts,phase,level,message) "
                   "VALUES(?,?,?,?,?)", (run_id, time.time(), phase, level, message))

    def get_events(self, run_id: int, after_id: int = 0) -> list[dict]:
        return self._rows("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id",
                          (run_id, after_id))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_store.py -q`
Expected: `9 passed`.

- [ ] **Step 5: Commit**

```bash
git add webapp/store.py tests/test_store.py
git commit -m "feat: SQLite store with Fernet-encrypted secrets, findings queries, event log"
```

---

### Task 4: `auditor/scope.py` — project enumeration + matching

**Files:**
- Create: `auditor/scope.py`
- Test: `tests/test_scope.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_scope.py`:
```python
from auditor.scope import match_projects

SRC = [
    {"key": "AC", "name": "AC Support", "id": "100"},
    {"key": "MS", "name": "Managed Services", "id": "101"},
    {"key": "OLD", "name": "Legacy", "id": "102"},
]
TGT = [
    {"key": "AC", "name": "AC Support", "id": "900"},
    {"key": "MS", "name": "Managed Services", "id": "901"},
    {"key": "NEW", "name": "Greenfield", "id": "902"},
]


def test_match_by_key():
    m = match_projects(SRC, TGT)
    keys = [p["key"] for p in m["matched"]]
    assert keys == ["AC", "MS"]
    ac = m["matched"][0]
    assert ac["src_id"] == "100" and ac["tgt_id"] == "900"
    assert ac["src_count"] is None and ac["tgt_count"] is None


def test_source_and_target_only():
    m = match_projects(SRC, TGT)
    assert [p["key"] for p in m["source_only"]] == ["OLD"]
    assert [p["key"] for p in m["target_only"]] == ["NEW"]


def test_empty_sides():
    m = match_projects([], TGT)
    assert m["matched"] == [] and len(m["target_only"]) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_scope.py -q`
Expected: `ModuleNotFoundError: No module named 'auditor.scope'`.

- [ ] **Step 3: Write the implementation**

`auditor/scope.py`:
```python
"""Project scope: enumerate both sides and match by key.

Counts (src_count/tgt_count) are left None here; the run engine fills them
via approx_count so this stays a pure function over project lists.
"""
from __future__ import annotations


def match_projects(src_projects: list, tgt_projects: list) -> dict:
    s = {p["key"]: p for p in src_projects}
    t = {p["key"]: p for p in tgt_projects}
    matched = []
    for key in sorted(set(s) & set(t)):
        matched.append({
            "key": key,
            "name": s[key].get("name"),
            "src_id": s[key].get("id"),
            "tgt_id": t[key].get("id"),
            "src_count": None,
            "tgt_count": None,
        })
    source_only = [{"key": k, "name": s[k].get("name")}
                   for k in sorted(set(s) - set(t))]
    target_only = [{"key": k, "name": t[k].get("name")}
                   for k in sorted(set(t) - set(s))]
    return {"matched": matched, "source_only": source_only,
            "target_only": target_only}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_scope.py -q`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add auditor/scope.py tests/test_scope.py
git commit -m "feat: project scope matching (matched/source-only/target-only)"
```

---

### Task 5: `auditor/permissions.py` — blind-spot detection + consent-gated elevation

**Files:**
- Create: `auditor/permissions.py`
- Test: `tests/test_permissions.py`

Background (the lesson this encodes): on the reference audit, the target MS
project initially read as 0/16,016 because the auditing account lacked Browse —
a permission artifact masquerading as total data loss. Detection: compare the
JQL `approximate-count` (permission-bound) against the project's
`insight.totalIssueCount` (from `/project/search?expand=insight`). A zero/low
search count with a populated insight count = blind spot. Fix path: grant the
account the project's admin role (recorded), undo at run end.

- [ ] **Step 1: Write the failing tests**

`tests/test_permissions.py`:
```python
import httpx
from auditor.client import Connection, JiraClient
from auditor.permissions import (apply_elevation, detect_blind_spots,
                                 find_admin_role_id, undo_elevation)


def mk(handler):
    conn = Connection(auth_type="pat", site_url="https://t.atlassian.net",
                      email="a@b.c", api_token="x")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def test_detect_blind_spot_when_search_zero_but_insight_populated():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("approximate-count"):
            jql = req.content.decode()
            return httpx.Response(200, json={"count": 0 if "MS" in jql else 40000})
        if p.endswith("project/search"):
            return httpx.Response(200, json={"isLast": True, "values": [
                {"key": "MS", "insight": {"totalIssueCount": 16016}},
                {"key": "AC", "insight": {"totalIssueCount": 40092}}]})
        return httpx.Response(404)
    out = detect_blind_spots(mk(handler), ["MS", "AC"])
    ms = next(o for o in out if o["key"] == "MS")
    ac = next(o for o in out if o["key"] == "AC")
    assert ms["blind_spot"] is True and ms["search_count"] == 0
    assert ms["insight_count"] == 16016
    assert ac["blind_spot"] is False


def test_no_blind_spot_when_insight_missing_and_search_zero():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("approximate-count"):
            return httpx.Response(200, json={"count": 0})
        if p.endswith("project/search"):
            return httpx.Response(200, json={"isLast": True, "values": [
                {"key": "EMPTY"}]})
        return httpx.Response(404)
    out = detect_blind_spots(mk(handler), ["EMPTY"])
    assert out[0]["blind_spot"] is False        # genuinely empty, not masked


def test_find_admin_role_id_prefers_administrators():
    def handler(req):
        return httpx.Response(200, json=[
            {"name": "Developers", "id": 1},
            {"name": "Administrators", "id": 9},
            {"name": "Admin-lite", "id": 3}])
    assert find_admin_role_id(mk(handler)) == 9


def test_apply_and_undo_elevation_logs():
    posts, deletes = [], []
    def handler(req):
        if req.method == "POST":
            posts.append(str(req.url.path))
            return httpx.Response(200, json={})
        if req.method == "DELETE":
            deletes.append(str(req.url))
            return httpx.Response(204)
        return httpx.Response(404)
    cl = mk(handler)
    grants = apply_elevation(cl, ["10001", "10002"], role_id=9, account_id="acc-1")
    assert [g["ok"] for g in grants] == [True, True]
    assert posts == ["/rest/api/3/project/10001/role/9",
                     "/rest/api/3/project/10002/role/9"]
    undone = undo_elevation(cl, grants, role_id=9, account_id="acc-1")
    assert all(u["ok"] for u in undone) and len(deletes) == 2
    assert "user=acc-1" in deletes[0]


def test_apply_elevation_records_failures():
    def handler(req):
        if "10002" in str(req.url.path):
            return httpx.Response(400, text="already a member")
        return httpx.Response(200, json={})
    grants = apply_elevation(mk(handler), ["10001", "10002"], 9, "acc-1")
    assert grants[0]["ok"] is True and grants[1]["ok"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_permissions.py -q`
Expected: `ModuleNotFoundError: No module named 'auditor.permissions'`.

- [ ] **Step 3: Write the implementation**

`auditor/permissions.py`:
```python
"""Permission blind-spot detection + consent-gated role elevation.

Encodes the reference-audit lesson: a permission-bound zero must never be read
as an empty project. Elevation is built/applied ONLY when the operator
confirms in the UI; every grant is logged so undo is deterministic.
"""
from __future__ import annotations

from .client import JiraClient


def detect_blind_spots(client: JiraClient, project_keys: list[str]) -> list[dict]:
    projects, _ = client.all_projects()
    insight = {}
    for p in projects:
        cnt = ((p.get("insight") or {}).get("totalIssueCount"))
        insight[p.get("key")] = cnt
    out = []
    for key in project_keys:
        sc = client.approx_count(f'project = "{key}"')
        search_count = sc if isinstance(sc, int) else None
        ins = insight.get(key)
        blind = (search_count is not None and ins is not None
                 and ins > 0 and search_count < ins * 0.5)
        out.append({"key": key, "search_count": search_count,
                    "insight_count": ins, "blind_spot": bool(blind)})
    return out


def find_admin_role_id(client: JiraClient) -> int | None:
    st, roles = client.req("/rest/api/3/role")
    if st != 200 or not isinstance(roles, list):
        return None
    admin = {r.get("name", ""): r.get("id") for r in roles
             if "admin" in r.get("name", "").lower()}
    return (admin.get("Administrators") or admin.get("Administrator")
            or next(iter(admin.values()), None))


def apply_elevation(client: JiraClient, project_ids: list[str], role_id: int,
                    account_id: str) -> list[dict]:
    log = []
    for pid in project_ids:
        st, d = client.req(f"/rest/api/3/project/{pid}/role/{role_id}", "POST",
                           {"user": [account_id]})
        log.append({"project_id": pid, "status": st, "ok": st in (200, 204)})
    return log


def undo_elevation(client: JiraClient, grants: list[dict], role_id: int,
                   account_id: str) -> list[dict]:
    out = []
    for g in grants:
        if not g.get("ok"):
            continue
        pid = g["project_id"]
        st, _ = client.req(
            f"/rest/api/3/project/{pid}/role/{role_id}?user={account_id}",
            "DELETE")
        out.append({"project_id": pid, "status": st, "ok": st in (200, 204)})
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_permissions.py -q`
Expected: `5 passed`.

- [ ] **Step 5: Commit**

```bash
git add auditor/permissions.py tests/test_permissions.py
git commit -m "feat: permission blind-spot detection + logged, undoable role elevation"
```

---

### Task 6: `auditor/extract.py` — content-fingerprint extraction

**Files:**
- Create: `auditor/extract.py`
- Test: `tests/test_extract.py`

Port of `extract_core.py`: every issue (no sampling), audit-critical fields, description/comment bodies reduced to sha fingerprints, gzip JSONL output, count verification. Instance-specific custom fields are NOT hardcoded — callers pass `extra_fields`.

- [ ] **Step 1: Write the failing tests**

`tests/test_extract.py`:
```python
import gzip, json
import httpx
from auditor.client import Connection, JiraClient, h16
from auditor.extract import CORE_FIELDS, extract_project, slim


def test_core_fields_have_no_instance_customfields():
    assert not any(f.startswith("customfield_") for f in CORE_FIELDS)
    for must in ("summary", "description", "status", "comment", "attachment"):
        assert must in CORE_FIELDS


def test_slim_fingerprints_description_and_comments():
    issue = {"key": "AC-1", "id": "1", "fields": {
        "summary": "s",
        "description": {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "hello world"}]}]},
        "comment": {"total": 2, "comments": [
            {"author": {"displayName": "A"}, "created": "c1", "updated": "u1",
             "body": {"type": "doc", "content": [
                 {"type": "paragraph", "content": [{"type": "text", "text": "first"}]}]}},
            {"author": {"displayName": "B"}, "created": "c2", "updated": "u2",
             "body": {"type": "doc", "content": [
                 {"type": "paragraph", "content": [{"type": "text", "text": "second"}]}]}},
        ]},
        "attachment": [{"filename": "a.png", "size": 10, "created": "c",
                        "author": {"displayName": "A"}}],
        "worklog": {"total": 3},
        "issuelinks": [{"type": {"name": "Blocks"},
                        "inwardIssue": {"key": "AC-9"}, "outwardIssue": None}],
        "environment": None,
    }}
    out = slim(issue)
    f = out["fields"]
    assert f["description"] == {"len": 11, "sha": h16("hello world"),
                                "head": "hello world"}
    assert f["comment"]["total"] == 2 and f["comment"]["inline"] == 2
    assert f["comment"]["items"][0]["sha"] == h16("first")
    assert f["attachment"] == [{"filename": "a.png", "size": 10, "created": "c",
                                "author": "A"}]
    assert f["worklog"] == {"total": 3}
    assert f["issuelinks"] == [{"type": "Blocks", "inward": "AC-9", "outward": None}]
    assert f["environment"] is None


def _client_with_issues(issues, count):
    def handler(req):
        p = str(req.url.path)
        if p.endswith("approximate-count"):
            return httpx.Response(200, json={"count": count})
        if p.endswith("search/jql"):
            return httpx.Response(200, json={"issues": issues, "isLast": True})
        return httpx.Response(404)
    conn = Connection(auth_type="pat", site_url="https://s.atlassian.net",
                      email="e", api_token="t")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def test_extract_project_writes_gz_and_verifies(tmp_path):
    issues = [{"key": f"AC-{i}", "id": str(i), "fields": {"summary": f"s{i}"}}
              for i in range(3)]
    out = tmp_path / "AC.core.jsonl.gz"
    progress = []
    res = extract_project(_client_with_issues(issues, 3), "AC", str(out),
                          progress=lambda n: progress.append(n))
    assert res == {"extracted": 3, "approx": 3, "verified": True}
    with gzip.open(out, "rt") as fh:
        rows = [json.loads(l) for l in fh]
    assert [r["key"] for r in rows] == ["AC-0", "AC-1", "AC-2"]


def test_extract_project_flags_count_mismatch(tmp_path):
    issues = [{"key": "AC-1", "id": "1", "fields": {"summary": "s"}}]
    res = extract_project(_client_with_issues(issues, 5), "AC",
                          str(tmp_path / "x.gz"))
    assert res["verified"] is False and res["approx"] == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_extract.py -q`
Expected: `ModuleNotFoundError: No module named 'auditor.extract'`.

- [ ] **Step 3: Write the implementation**

`auditor/extract.py`:
```python
"""Deterministic content-complete extraction (port of extract_core.py).

Every issue of a project, both audit-critical fields and content fingerprints
(description/comments reduced to sha16 + length so megabytes of prose become
comparable rows). Output: gzip JSONL, one slim issue per line, key-ordered.
Count-verified against approximate-count so a silent pagination gap can never
masquerade as a clean extraction.
"""
from __future__ import annotations

import gzip
import json
from typing import Callable

from .client import JiraClient, adf_text, h16

CORE_FIELDS = [
    "summary", "description", "issuetype", "status", "statuscategorychangedate",
    "priority", "resolution", "resolutiondate", "assignee", "reporter", "creator",
    "created", "updated", "duedate", "labels", "components", "fixVersions",
    "versions", "parent", "issuelinks", "subtasks", "comment", "attachment",
    "votes", "watches", "timetracking", "environment", "security", "worklog",
]


def slim(issue: dict) -> dict:
    f = dict(issue.get("fields", {}))
    dtext = adf_text(f.get("description")) if f.get("description") else ""
    f["description"] = {"len": len(dtext), "sha": h16(dtext), "head": dtext[:200]}
    c = f.get("comment") or {}
    items = []
    for cm in (c.get("comments") or []):
        ctext = adf_text(cm.get("body"))
        items.append({"author": (cm.get("author") or {}).get("displayName"),
                      "created": cm.get("created"), "updated": cm.get("updated"),
                      "len": len(ctext), "sha": h16(ctext)})
    f["comment"] = {"total": c.get("total"), "inline": len(items), "items": items}
    f["attachment"] = [{"filename": a.get("filename"), "size": a.get("size"),
                        "created": a.get("created"),
                        "author": (a.get("author") or {}).get("displayName")}
                       for a in (f.get("attachment") or [])]
    wl = f.get("worklog") or {}
    f["worklog"] = {"total": wl.get("total")}
    f["issuelinks"] = [{"type": (l.get("type") or {}).get("name"),
                        "inward": (l.get("inwardIssue") or {}).get("key"),
                        "outward": (l.get("outwardIssue") or {}).get("key")}
                       for l in (f.get("issuelinks") or [])]
    etext = adf_text(f.get("environment")) if f.get("environment") else ""
    f["environment"] = {"len": len(etext), "sha": h16(etext)} if etext else None
    return {"key": issue["key"], "id": issue.get("id"), "fields": f}


def extract_project(client: JiraClient, project_key: str, out_path: str,
                    extra_fields: tuple = (),
                    progress: Callable[[int], None] | None = None) -> dict:
    fields = CORE_FIELDS + list(extra_fields)
    n = 0
    with gzip.open(out_path, "wt", encoding="utf-8") as fh:
        for iss in client.search_jql(
                f'project = "{project_key}" ORDER BY key ASC', fields, page=50):
            fh.write(json.dumps(slim(iss), default=str) + "\n")
            n += 1
            if progress and n % 500 == 0:
                progress(n)
    ac = client.approx_count(f'project = "{project_key}"')
    verified = isinstance(ac, int) and n == ac
    if progress:
        progress(n)
    return {"extracted": n, "approx": ac, "verified": verified}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_extract.py -q`
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add auditor/extract.py tests/test_extract.py
git commit -m "feat: content-fingerprint extraction with count verification"
```

---

### Task 7: `auditor/compare.py` — fidelity comparison → findings

**Files:**
- Create: `auditor/compare.py`
- Test: `tests/test_compare.py`

Port of `compare.py` reshaped to emit finding dicts (spec kinds) instead of files. Key semantics preserved: presence split by the cutover line (`tail` = missing key-number above target max = expected drift; `hole` = below the line = **genuine loss** → `missing_in_tgt`), field SPECS with severity, remap tables, user-mapping audit, comment count/content fidelity, attachment fidelity. New: `missing_in_src` for target-extra issues (above src max → `tail_post_cutover` with `direction: "target"`), `key_collision` when a same-key pair's identity metadata (created + reporter + summary) disagrees.

- [ ] **Step 1: Write the failing tests**

`tests/test_compare.py`:
```python
import gzip, json
import pytest
from auditor.client import h16
from auditor.compare import compare_project


def write_side(path, rows):
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def mk_issue(key, summary="s", desc="body", status="Open", created="2026-01-01T00:00:00.000+0000",
             reporter="Ana", comments=(), attachments=(), labels=(), links=()):
    return {"key": key, "id": key, "fields": {
        "summary": summary,
        "description": {"len": len(desc), "sha": h16(desc), "head": desc[:200]},
        "issuetype": {"name": "Task"}, "status": {"name": status},
        "priority": {"name": "P3"}, "resolution": None, "resolutiondate": None,
        "created": created, "updated": "x", "duedate": None,
        "labels": list(labels), "components": [], "fixVersions": [], "versions": [],
        "parent": None, "environment": None, "security": None,
        "assignee": {"displayName": "Bob"}, "reporter": {"displayName": reporter},
        "creator": {"displayName": reporter},
        "comment": {"total": len(comments), "inline": len(comments),
                    "items": [{"author": "A", "created": "c", "updated": "u",
                               "len": len(t), "sha": h16(t)} for t in comments]},
        "worklog": {"total": 0}, "votes": {"votes": 0}, "watches": {"watchCount": 0},
        "attachment": [{"filename": fn, "size": sz, "created": "c", "author": "A"}
                       for fn, sz in attachments],
        "issuelinks": [{"type": t, "inward": i, "outward": o} for t, i, o in links],
    }}


@pytest.fixture()
def paths(tmp_path):
    return str(tmp_path / "src.gz"), str(tmp_path / "tgt.gz")


def kinds(findings):
    return sorted(f["kind"] for f in findings)


def test_identical_sides_produce_no_findings(paths):
    src, tgt = paths
    rows = [mk_issue("AC-1"), mk_issue("AC-2")]
    write_side(src, rows); write_side(tgt, rows)
    out = compare_project("AC", src, tgt)
    assert out["findings"] == []
    assert out["stats"]["src"] == 2 and out["stats"]["common"] == 2
    assert out["stats"]["fidelity_pct"] == 100.0


def test_genuine_hole_vs_post_cutover_tail(paths):
    src, tgt = paths
    # target max key-num = 3; AC-2 missing below the line = HOLE,
    # AC-9 missing above the line = expected tail.
    write_side(src, [mk_issue("AC-1"), mk_issue("AC-2"), mk_issue("AC-3"),
                     mk_issue("AC-9")])
    write_side(tgt, [mk_issue("AC-1"), mk_issue("AC-3")])
    out = compare_project("AC", src, tgt)
    by_kind = {f["kind"]: f for f in out["findings"]}
    assert by_kind["missing_in_tgt"]["src_key"] == "AC-2"
    tail = by_kind["tail_post_cutover"]
    assert tail["src_key"] == "AC-9" and tail["detail"]["direction"] == "source"
    assert out["stats"]["missing_in_tgt"] == 1 and out["stats"]["tails"] == 1


def test_target_extra_above_src_max_is_target_tail(paths):
    src, tgt = paths
    write_side(src, [mk_issue("AC-1")])
    write_side(tgt, [mk_issue("AC-1"), mk_issue("AC-5")])
    out = compare_project("AC", src, tgt)
    f = out["findings"][0]
    assert f["kind"] == "tail_post_cutover" and f["detail"]["direction"] == "target"
    assert f["tgt_key"] == "AC-5"


def test_target_extra_below_src_max_is_missing_in_src(paths):
    src, tgt = paths
    write_side(src, [mk_issue("AC-1"), mk_issue("AC-9")])
    write_side(tgt, [mk_issue("AC-1"), mk_issue("AC-5"), mk_issue("AC-9")])
    out = compare_project("AC", src, tgt)
    assert kinds(out["findings"]) == ["missing_in_src"]


def test_field_and_content_mismatches(paths):
    src, tgt = paths
    write_side(src, [mk_issue("AC-1", status="Open", desc="original")])
    write_side(tgt, [mk_issue("AC-1", status="Done", desc="rewritten")])
    out = compare_project("AC", src, tgt)
    ks = kinds(out["findings"])
    assert "field_mismatch" in ks and "content_mismatch" in ks
    fm = next(f for f in out["findings"] if f["kind"] == "field_mismatch")
    assert fm["field"] == "status" and fm["detail"]["src"] == "Open" \
        and fm["detail"]["tgt"] == "Done" and fm["detail"]["sev"] == "high"
    assert out["stats"]["remap"]["status"][0]["count"] == 1


def test_comment_and_attachment_fidelity(paths):
    src, tgt = paths
    write_side(src, [mk_issue("AC-1", comments=("hello", "world"),
                              attachments=(("a.png", 10),))])
    write_side(tgt, [mk_issue("AC-1", comments=("hello", "DIFFERENT"),
                              attachments=(("a.png", 10), ("b.png", 5)))])
    out = compare_project("AC", src, tgt)
    ks = kinds(out["findings"])
    assert "comment_mismatch" in ks and "attachment_mismatch" in ks
    am = next(f for f in out["findings"] if f["kind"] == "attachment_mismatch")
    assert am["detail"]["extra_in_tgt"] == ["b.png|5"]


def test_key_collision_when_identity_metadata_disagrees(paths):
    src, tgt = paths
    write_side(src, [mk_issue("AC-1", summary="real issue", reporter="Ana",
                              created="2020-01-01T00:00:00.000+0000")])
    write_side(tgt, [mk_issue("AC-1", summary="totally different", reporter="Zed",
                              created="2026-06-01T00:00:00.000+0000")])
    out = compare_project("AC", src, tgt)
    assert "key_collision" in kinds(out["findings"])


def test_unmapped_users_in_stats(paths):
    src, tgt = paths
    s = mk_issue("AC-1", reporter="Ana")
    t = mk_issue("AC-1", reporter="Ana")
    t["fields"]["assignee"] = {"displayName": "Former user"}
    write_side(src, [s]); write_side(tgt, [t])
    out = compare_project("AC", src, tgt)
    assert {"src": "Bob", "occurrences": 1} in out["stats"]["unmapped_users"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_compare.py -q`
Expected: `ModuleNotFoundError: No module named 'auditor.compare'`.

- [ ] **Step 3: Write the implementation**

`auditor/compare.py`:
```python
"""Field-by-field migration comparison for one project (port of compare.py).

No sampling: every common issue is compared on the SPECS ledger; presence is
split by the cutover line (missing keys numbered ABOVE the other side's max
key are post-cutover drift, not loss). Emits spec-shaped finding dicts plus a
stats block; persistence is the caller's job.
"""
from __future__ import annotations

import gzip
import json
from collections import Counter, defaultdict


def _load(path: str) -> dict:
    d = {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            d[r["key"]] = r["fields"]
    return d


def _nm(o):
    if isinstance(o, dict):
        return o.get("name") or o.get("value")
    return o


def _person(o):
    return o.get("displayName") if isinstance(o, dict) else None


def _nameset(lst):
    return sorted([(x.get("name") if isinstance(x, dict) else x)
                   for x in (lst or [])])


def _num(k: str) -> int:
    try:
        return int(k.split("-")[-1])
    except (ValueError, AttributeError):
        return -1


SPECS = [
    ("summary",        lambda f: f.get("summary"), "high"),
    ("issuetype",      lambda f: _nm(f.get("issuetype")), "high"),
    ("status",         lambda f: _nm(f.get("status")), "high"),
    ("priority",       lambda f: _nm(f.get("priority")), "med"),
    ("resolution",     lambda f: _nm(f.get("resolution")), "high"),
    ("resolutiondate", lambda f: f.get("resolutiondate"), "med"),
    ("created",        lambda f: f.get("created"), "high"),
    ("duedate",        lambda f: f.get("duedate"), "med"),
    ("labels",         lambda f: sorted(f.get("labels") or []), "med"),
    ("components",     lambda f: _nameset(f.get("components")), "med"),
    ("fixVersions",    lambda f: _nameset(f.get("fixVersions")), "med"),
    ("versions",       lambda f: _nameset(f.get("versions")), "med"),
    ("parent",         lambda f: (f.get("parent") or {}).get("key"), "high"),
    ("environment",    lambda f: (f.get("environment") or {}).get("sha"), "med"),
    ("security",       lambda f: _nm(f.get("security")), "high"),
    ("assignee",       lambda f: _person(f.get("assignee")), "high"),
    ("reporter",       lambda f: _person(f.get("reporter")), "high"),
    ("creator",        lambda f: _person(f.get("creator")), "med"),
    ("worklog_total",  lambda f: (f.get("worklog") or {}).get("total"), "med"),
    ("votes",          lambda f: (f.get("votes") or {}).get("votes"), "low"),
    ("watches",        lambda f: (f.get("watches") or {}).get("watchCount"), "low"),
    ("issuelinks",     lambda f: sorted(
        f"{l.get('type')}|{l.get('inward')}|{l.get('outward')}"
        for l in (f.get("issuelinks") or [])), "med"),
]
_REMAP_FIELDS = ("status", "issuetype", "priority")
_FORMER = {"Former user", "Former User"}


def _desc_sha(f):
    return (f.get("description") or {}).get("sha")


def _att_set(f):
    return sorted(f"{a.get('filename')}|{a.get('size')}"
                  for a in (f.get("attachment") or []))


def compare_project(project: str, src_path: str, tgt_path: str) -> dict:
    src, tgt = _load(src_path), _load(tgt_path)
    sk, tk = set(src), set(tgt)
    common = sorted(sk & tk, key=_num)
    missing = sorted(sk - tk, key=_num)
    extra = sorted(tk - sk, key=_num)
    tgt_max = max((_num(k) for k in tk), default=-1)
    src_max = max((_num(k) for k in sk), default=-1)

    findings: list[dict] = []
    # presence: source side
    for k in missing:
        if _num(k) > tgt_max:
            findings.append({"project": project, "kind": "tail_post_cutover",
                             "src_key": k, "tgt_key": None, "field": None,
                             "summary": (src[k].get("summary") or "")[:200],
                             "detail": {"direction": "source",
                                        "created": src[k].get("created"),
                                        "cutover_max_key": tgt_max}})
        else:
            findings.append({"project": project, "kind": "missing_in_tgt",
                             "src_key": k, "tgt_key": None, "field": None,
                             "summary": (src[k].get("summary") or "")[:200],
                             "detail": {"created": src[k].get("created"),
                                        "below_cutover_line": True}})
    # presence: target side
    for k in extra:
        if _num(k) > src_max:
            findings.append({"project": project, "kind": "tail_post_cutover",
                             "src_key": None, "tgt_key": k, "field": None,
                             "summary": (tgt[k].get("summary") or "")[:200],
                             "detail": {"direction": "target",
                                        "created": tgt[k].get("created"),
                                        "cutover_max_key": src_max}})
        else:
            findings.append({"project": project, "kind": "missing_in_src",
                             "src_key": None, "tgt_key": k, "field": None,
                             "summary": (tgt[k].get("summary") or "")[:200],
                             "detail": {"created": tgt[k].get("created")}})

    remap = {f: Counter() for f in _REMAP_FIELDS}
    user_pairs: Counter = Counter()
    unmapped: Counter = Counter()
    mismatch_issue_keys: set = set()
    sev_count: Counter = Counter()
    field_counts: Counter = Counter()

    for k in common:
        fs, ft = src[k], tgt[k]
        # collision: same key, different identity metadata (>=2 of 3 disagree)
        ident_diff = sum([
            fs.get("created") != ft.get("created"),
            _person(fs.get("reporter")) != _person(ft.get("reporter")),
            fs.get("summary") != ft.get("summary"),
        ])
        if ident_diff >= 2:
            findings.append({"project": project, "kind": "key_collision",
                             "src_key": k, "tgt_key": k, "field": None,
                             "summary": (fs.get("summary") or "")[:200],
                             "detail": {"src_created": fs.get("created"),
                                        "tgt_created": ft.get("created"),
                                        "src_reporter": _person(fs.get("reporter")),
                                        "tgt_reporter": _person(ft.get("reporter"))}})
            continue   # a collided pair's field diffs are meaningless noise
        for name, fn, sev in SPECS:
            a, b = fn(fs), fn(ft)
            if a != b:
                mismatch_issue_keys.add(k)
                sev_count[sev] += 1
                field_counts[name] += 1
                kind = "link_mismatch" if name == "issuelinks" else "field_mismatch"
                findings.append({"project": project, "kind": kind,
                                 "src_key": k, "tgt_key": k, "field": name,
                                 "summary": f"{name} differs",
                                 "detail": {"src": a, "tgt": b, "sev": sev}})
                if name in remap:
                    remap[name][(a, b)] += 1
        if _desc_sha(fs) != _desc_sha(ft):
            mismatch_issue_keys.add(k)
            findings.append({"project": project, "kind": "content_mismatch",
                             "src_key": k, "tgt_key": k, "field": "description",
                             "summary": "description content differs",
                             "detail": {"src_len": (fs.get("description") or {}).get("len"),
                                        "tgt_len": (ft.get("description") or {}).get("len")}})
        for role in ("assignee", "reporter", "creator"):
            a, b = _person(fs.get(role)), _person(ft.get(role))
            if a:
                user_pairs[(a, b)] += 1
                if b is None or b in _FORMER:
                    unmapped[a] += 1
        sc, tc = fs.get("comment") or {}, ft.get("comment") or {}
        cm_detail = None
        if sc.get("total") != tc.get("total"):
            cm_detail = {"src_total": sc.get("total"), "tgt_total": tc.get("total")}
        else:
            full = (sc.get("total") == sc.get("inline")
                    and tc.get("total") == tc.get("inline"))
            if full and (sc.get("total") or 0) > 0:
                s_sha = sorted(i["sha"] for i in sc.get("items", []))
                t_sha = sorted(i["sha"] for i in tc.get("items", []))
                if s_sha != t_sha:
                    cm_detail = {"content_differs": True,
                                 "total": sc.get("total")}
        if cm_detail:
            mismatch_issue_keys.add(k)
            findings.append({"project": project, "kind": "comment_mismatch",
                             "src_key": k, "tgt_key": k, "field": "comment",
                             "summary": "comment fidelity differs",
                             "detail": cm_detail})
        sa, ta = _att_set(fs), _att_set(ft)
        if sa != ta:
            mismatch_issue_keys.add(k)
            findings.append({"project": project, "kind": "attachment_mismatch",
                             "src_key": k, "tgt_key": k, "field": "attachment",
                             "summary": "attachments differ",
                             "detail": {"missing_in_tgt": sorted(set(sa) - set(ta)),
                                        "extra_in_tgt": sorted(set(ta) - set(sa))}})

    holes = sum(1 for f in findings if f["kind"] == "missing_in_tgt")
    tails = sum(1 for f in findings if f["kind"] == "tail_post_cutover")
    clean_common = len(common) - len(mismatch_issue_keys)
    fidelity = round(100.0 * clean_common / len(common), 2) if common else 100.0
    stats = {
        "project": project, "src": len(sk), "tgt": len(tk),
        "common": len(common), "missing_in_tgt": holes,
        "missing_in_src": sum(1 for f in findings if f["kind"] == "missing_in_src"),
        "tails": tails, "collisions": sum(1 for f in findings
                                          if f["kind"] == "key_collision"),
        "issues_with_mismatches": len(mismatch_issue_keys),
        "fidelity_pct": fidelity,
        "severity_totals": dict(sev_count),
        "field_mismatch_counts": dict(field_counts),
        "remap": {f: [{"src": s, "tgt": t, "count": c}
                      for (s, t), c in cnt.most_common(40)]
                  for f, cnt in remap.items() if cnt},
        "unmapped_users": [{"src": u, "occurrences": c}
                           for u, c in unmapped.most_common(60)],
        "distinct_src_people": len({a for (a, _) in user_pairs}),
    }
    return {"stats": stats, "findings": findings}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_compare.py -q`
Expected: `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add auditor/compare.py tests/test_compare.py
git commit -m "feat: fidelity comparison with cutover-tail split, collisions, content/comment/attachment checks"
```

---

### Task 8: `auditor/config_audit.py` — instance config parity

**Files:**
- Create: `auditor/config_audit.py`
- Test: `tests/test_config_audit.py`

Port of `config_audit.py` with the `config_fix.py` corrections folded in: correct select-type detection (`select|radio|checkbox|cascading`), servicedeskapi `start/limit` pagination via `client.sd_list`. Emits config findings + per-area summaries.

- [ ] **Step 1: Write the failing tests**

`tests/test_config_audit.py`:
```python
import httpx
from auditor.client import Connection, JiraClient
from auditor.config_audit import audit_config


def mk(handler, site):
    conn = Connection(auth_type="pat", site_url=site, email="e", api_token="t")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def make_pair(src_data, tgt_data):
    """src_data/tgt_data: dict path-suffix -> response json."""
    def build(data):
        def handler(req):
            p = str(req.url.path)
            for suffix, payload in data.items():
                if p.endswith(suffix):
                    return httpx.Response(200, json=payload)
            return httpx.Response(200, json={"values": [], "isLast": True})
        return handler
    return (mk(build(src_data), "https://s.atlassian.net"),
            mk(build(tgt_data), "https://t.atlassian.net"))


BASE = {
    "/rest/api/3/status": [{"name": "Open"}, {"name": "On Hold"}],
    "/rest/api/3/issuetype": [{"name": "Task"}],
    "/rest/api/3/priority": [{"name": "P1"}],
    "/rest/api/3/resolution": [{"name": "Done"}],
    "/rest/api/3/issueLinkType": {"issueLinkTypes": [{"name": "Blocks"}]},
    "/rest/api/3/role": [{"name": "Administrators", "id": 9}],
    "/rest/api/3/field": [],
    "/rest/api/3/workflow/search": {"values": [], "isLast": True},
    "/rest/api/3/screens": {"values": [], "isLast": True},
}


def test_simple_dimension_source_only_findings():
    tgt = dict(BASE); tgt["/rest/api/3/status"] = [{"name": "Open"}]
    src_cl, tgt_cl = make_pair(BASE, tgt)
    out = audit_config(src_cl, tgt_cl)
    st = out["areas"]["statuses"]
    assert st["src"] == 2 and st["tgt"] == 1 and st["in_both"] == 1
    f = [x for x in out["findings"] if x["area"] == "statuses"]
    assert f == [{"area": "statuses", "name": "On Hold",
                  "kind": "missing_in_tgt", "detail": {}}]


def test_custom_field_type_and_option_mismatches():
    src = dict(BASE)
    src["/rest/api/3/field"] = [
        {"name": "Squad", "id": "customfield_1", "custom": True,
         "schema": {"custom": "com.atlassian.jira.plugin:select"}},
        {"name": "Effort", "id": "customfield_2", "custom": True,
         "schema": {"custom": "com.atlassian.jira.plugin:float"}},
    ]
    src["/rest/api/3/field/customfield_1/context"] = {
        "values": [{"id": "ctx1"}], "isLast": True}
    src["/rest/api/3/field/customfield_1/context/ctx1/option"] = {
        "values": [{"value": "Alpha"}, {"value": "Beta"}], "isLast": True}
    tgt = dict(BASE)
    tgt["/rest/api/3/field"] = [
        {"name": "Squad", "id": "customfield_9", "custom": True,
         "schema": {"custom": "com.atlassian.jira.plugin:select"}},
        {"name": "Effort", "id": "customfield_8", "custom": True,
         "schema": {"custom": "com.atlassian.jira.plugin:textfield"}},
    ]
    tgt["/rest/api/3/field/customfield_9/context"] = {
        "values": [{"id": "ctxA"}], "isLast": True}
    tgt["/rest/api/3/field/customfield_9/context/ctxA/option"] = {
        "values": [{"value": "Alpha"}], "isLast": True}
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl)
    kinds = {(f["name"], f["kind"]) for f in out["findings"]
             if f["area"] == "custom_fields"}
    assert ("Effort", "type_mismatch") in kinds
    assert ("Squad", "option_mismatch") in kinds
    opt = next(f for f in out["findings"] if f["kind"] == "option_mismatch")
    assert opt["detail"]["missing_options_in_tgt"] == ["Beta"]


def test_workflow_structure_mismatch():
    src = dict(BASE)
    src["/rest/api/3/workflow/search"] = {"values": [
        {"id": {"name": "Flow"}, "transitions": [{"name": "Start"}, {"name": "Finish"}],
         "statuses": [{"id": "1"}, {"id": "2"}]}], "isLast": True}
    tgt = dict(BASE)
    tgt["/rest/api/3/workflow/search"] = {"values": [
        {"id": {"name": "Flow"}, "transitions": [{"name": "Start"}],
         "statuses": [{"id": "1"}]}], "isLast": True}
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl)
    f = next(x for x in out["findings"] if x["area"] == "workflows")
    assert f["kind"] == "structure_mismatch" and f["name"] == "Flow"
    assert f["detail"]["transitions_missing_in_tgt"] == ["Finish"]


def test_jsm_request_types_and_queues():
    src = dict(BASE)
    src["/rest/servicedeskapi/servicedesk"] = {
        "values": [{"id": "4", "projectKey": "AC"}], "isLastPage": True}
    src["/rest/servicedeskapi/servicedesk/4/requesttype"] = {
        "values": [{"name": "Bug"}, {"name": "Access"}], "isLastPage": True}
    src["/rest/servicedeskapi/servicedesk/4/queue"] = {
        "values": [{"name": "All open"}], "isLastPage": True}
    tgt = dict(BASE)
    tgt["/rest/servicedeskapi/servicedesk"] = {
        "values": [{"id": "7", "projectKey": "AC"}], "isLastPage": True}
    tgt["/rest/servicedeskapi/servicedesk/7/requesttype"] = {
        "values": [{"name": "Bug"}], "isLastPage": True}
    tgt["/rest/servicedeskapi/servicedesk/7/queue"] = {
        "values": [{"name": "All open"}], "isLastPage": True}
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    f = [x for x in out["findings"] if x["area"] == "jsm"]
    assert f == [{"area": "jsm", "name": "AC: request type 'Access'",
                  "kind": "missing_in_tgt",
                  "detail": {"project": "AC", "object": "request_type"}}]
    assert out["areas"]["jsm"]["AC"]["request_types"]["src"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_config_audit.py -q`
Expected: `ModuleNotFoundError: No module named 'auditor.config_audit'`.

- [ ] **Step 3: Write the implementation**

`auditor/config_audit.py`:
```python
"""Instance configuration parity audit (port of config_audit.py + config_fix.py).

Compares EVERY config object by NAME (IDs are re-minted by migration):
simple dimensions, custom fields (type + select options), workflows
(structure), screens (deep field check, capped), and JSM request types +
queues per selected project. Emits spec-shaped config findings.
"""
from __future__ import annotations

from typing import Callable

from .client import JiraClient

SIMPLE = [
    ("statuses", "/rest/api/3/status", None),
    ("issue_types", "/rest/api/3/issuetype", None),
    ("priorities", "/rest/api/3/priority", None),
    ("resolutions", "/rest/api/3/resolution", None),
    ("link_types", "/rest/api/3/issueLinkType", "issueLinkTypes"),
    ("roles", "/rest/api/3/role", None),
    ("screens", "/rest/api/3/screens", None),
    ("screen_schemes", "/rest/api/3/screenscheme", None),
    ("issuetype_screen_schemes", "/rest/api/3/issuetypescreenscheme", None),
    ("workflow_schemes", "/rest/api/3/workflowscheme", None),
    ("issuetype_schemes", "/rest/api/3/issuetypescheme", None),
    ("field_configurations", "/rest/api/3/fieldconfiguration", None),
    ("field_config_schemes", "/rest/api/3/fieldconfigurationscheme", None),
    ("permission_schemes", "/rest/api/3/permissionscheme", "permissionSchemes"),
    ("notification_schemes", "/rest/api/3/notificationscheme", None),
]
_SELECT_MARKERS = ("select", "radio", "checkbox", "cascading")
_SCREEN_DEEP_CAP = 60
_OPTION_CONTEXT_CAP = 3


def _names(items, fn=lambda x: x.get("name")):
    return [fn(i) for i in (items or []) if fn(i)]


def _summary(label, s_names, t_names):
    s, t = set(s_names), set(t_names)
    return {"label": label, "src": len(s), "tgt": len(t),
            "in_both": len(s & t), "source_only": sorted(s - t),
            "target_only_count": len(t - s)}


def _field_options(client: JiraClient, fid: str) -> set:
    opts = []
    ctx, _ = client.paginate_start_at(f"/rest/api/3/field/{fid}/context")
    for c in (ctx or [])[:_OPTION_CONTEXT_CAP]:
        o, _ = client.paginate_start_at(
            f"/rest/api/3/field/{fid}/context/{c['id']}/option")
        opts += _names(o or [], lambda x: x.get("value"))
    return set(opts)


def _screen_fields(client: JiraClient, sid) -> set:
    out = []
    tabs, _ = client.paginate_start_at(f"/rest/api/3/screens/{sid}/tabs")
    for tb in (tabs or []):
        st, flds = client.req(f"/rest/api/3/screens/{sid}/tabs/{tb['id']}/fields")
        if st == 200 and isinstance(flds, list):
            out += _names(flds)
    return set(out)


def _wf_name(w):
    return (w.get("id") or {}).get("name") if isinstance(w.get("id"), dict) \
        else w.get("name")


def audit_config(src: JiraClient, tgt: JiraClient, jsm_projects=(),
                 progress: Callable[[str], None] | None = None) -> dict:
    areas: dict = {}
    findings: list[dict] = []
    say = progress or (lambda m: None)

    # ---- simple dimensions
    for area, path, key in SIMPLE:
        s, se = src.paginate_start_at(path, key=key)
        t, te = tgt.paginate_start_at(path, key=key)
        summ = _summary(area, _names(s), _names(t))
        if se or te:
            summ["error"] = f"src={se} tgt={te}"
        areas[area] = summ
        for name in summ["source_only"]:
            findings.append({"area": area, "name": name,
                             "kind": "missing_in_tgt", "detail": {}})
        say(f"[{area}] src={summ['src']} tgt={summ['tgt']} "
            f"source-only={len(summ['source_only'])}")

    # ---- custom fields: presence + type + select options
    sf, _ = src.paginate_start_at("/rest/api/3/field")
    tf, _ = tgt.paginate_start_at("/rest/api/3/field")
    scustom = {f["name"]: f for f in (sf or []) if f.get("custom")}
    tcustom = {f["name"]: f for f in (tf or []) if f.get("custom")}
    summ = _summary("custom_fields", scustom.keys(), tcustom.keys())
    for name in summ["source_only"]:
        findings.append({"area": "custom_fields", "name": name,
                         "kind": "missing_in_tgt",
                         "detail": {"type": str((scustom[name].get("schema") or {})
                                                .get("custom", "")).split(":")[-1]}})
    checked = 0
    for name in sorted(set(scustom) & set(tcustom)):
        s_type = str((scustom[name].get("schema") or {}).get("custom", "")).split(":")[-1]
        t_type = str((tcustom[name].get("schema") or {}).get("custom", "")).split(":")[-1]
        if s_type != t_type:
            findings.append({"area": "custom_fields", "name": name,
                             "kind": "type_mismatch",
                             "detail": {"src_type": s_type, "tgt_type": t_type}})
        ct = str((scustom[name].get("schema") or {}).get("custom", ""))
        if any(m in ct for m in _SELECT_MARKERS):
            checked += 1
            so = _field_options(src, scustom[name]["id"])
            to = _field_options(tgt, tcustom[name]["id"])
            miss = sorted(so - to)
            if miss:
                findings.append({"area": "custom_fields", "name": name,
                                 "kind": "option_mismatch",
                                 "detail": {"missing_options_in_tgt": miss[:40],
                                            "src_opts": len(so),
                                            "tgt_opts": len(to)}})
    summ["select_fields_checked"] = checked
    areas["custom_fields"] = summ
    say(f"[custom_fields] src={summ['src']} tgt={summ['tgt']} checked={checked}")

    # ---- workflows: structural comparison for in-both
    sw, _ = src.paginate_start_at("/rest/api/3/workflow/search",
                                  params={"expand": "transitions,statuses"})
    tw, _ = tgt.paginate_start_at("/rest/api/3/workflow/search",
                                  params={"expand": "transitions,statuses"})
    swn = {_wf_name(w): w for w in (sw or [])}
    twn = {_wf_name(w): w for w in (tw or [])}
    areas["workflows"] = _summary("workflows", swn.keys(), twn.keys())
    for name in areas["workflows"]["source_only"]:
        findings.append({"area": "workflows", "name": name,
                         "kind": "missing_in_tgt", "detail": {}})
    for name in sorted(set(swn) & set(twn)):
        s_tr = set(tr.get("name") for tr in (swn[name].get("transitions") or []))
        t_tr = set(tr.get("name") for tr in (twn[name].get("transitions") or []))
        s_st = len(swn[name].get("statuses") or [])
        t_st = len(twn[name].get("statuses") or [])
        if s_tr != t_tr or s_st != t_st:
            findings.append({"area": "workflows", "name": name,
                             "kind": "structure_mismatch",
                             "detail": {"src_statuses": s_st, "tgt_statuses": t_st,
                                        "transitions_missing_in_tgt":
                                            sorted(s_tr - t_tr)[:20]}})
    say(f"[workflows] in_both={areas['workflows']['in_both']}")

    # ---- screens: deep field check for in-both (bounded)
    ss, _ = src.paginate_start_at("/rest/api/3/screens")
    ts, _ = tgt.paginate_start_at("/rest/api/3/screens")
    ssn = {s["name"]: s for s in (ss or [])}
    tsn = {s["name"]: s for s in (ts or [])}
    in_both = sorted(set(ssn) & set(tsn))
    deep = in_both[:_SCREEN_DEEP_CAP]
    for name in deep:
        s_f = _screen_fields(src, ssn[name]["id"])
        t_f = _screen_fields(tgt, tsn[name]["id"])
        miss = sorted(s_f - t_f)
        if miss:
            findings.append({"area": "screens", "name": name,
                             "kind": "field_mismatch",
                             "detail": {"fields_missing_in_tgt": miss[:25]}})
    areas["screens"]["deep_checked"] = len(deep)
    areas["screens"]["capped"] = len(in_both) > _SCREEN_DEEP_CAP
    say(f"[screens] deep_checked={len(deep)} capped={areas['screens']['capped']}")

    # ---- JSM request types + queues per selected project (paginated correctly)
    def _sd_id(client, key):
        for s in client.sd_list("/rest/servicedeskapi/servicedesk"):
            if s.get("projectKey") == key:
                return s.get("id")
        return None

    def _jsm(client, key):
        sid = _sd_id(client, key)
        if not sid:
            return {"request_types": [], "queues": []}
        rt = client.sd_list(f"/rest/servicedeskapi/servicedesk/{sid}/requesttype")
        q = client.sd_list(f"/rest/servicedeskapi/servicedesk/{sid}/queue")
        return {"request_types": _names(rt), "queues": _names(q)}

    jsm_area = {}
    for key in jsm_projects:
        s_j, t_j = _jsm(src, key), _jsm(tgt, key)
        entry = {}
        for obj in ("request_types", "queues"):
            s_set, t_set = set(s_j[obj]), set(t_j[obj])
            entry[obj] = {"src": len(s_set), "tgt": len(t_set),
                          "source_only": sorted(s_set - t_set)}
            label = "request type" if obj == "request_types" else "queue"
            for name in sorted(s_set - t_set):
                findings.append({"area": "jsm",
                                 "name": f"{key}: {label} '{name}'",
                                 "kind": "missing_in_tgt",
                                 "detail": {"project": key,
                                            "object": label.replace(" ", "_")}})
        jsm_area[key] = entry
        say(f"[jsm {key}] rt src={entry['request_types']['src']}/"
            f"tgt={entry['request_types']['tgt']}")
    if jsm_projects:
        areas["jsm"] = jsm_area

    return {"areas": areas, "findings": findings}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_config_audit.py -q`
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add auditor/config_audit.py tests/test_config_audit.py
git commit -m "feat: instance config parity audit with select-option, workflow-structure, screen-field and JSM checks"
```

---

### Task 9: `auditor/findings.py` — run summary + verdict

**Files:**
- Create: `auditor/findings.py`
- Test: `tests/test_findings.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_findings.py`:
```python
from auditor.findings import build_run_summary


def proj(missing=0, tails=0, collisions=0, mismatched=0, src=100, common=None):
    common = common if common is not None else src - missing - tails
    return {"stats": {"project": "AC", "src": src, "tgt": common,
                      "common": common, "missing_in_tgt": missing,
                      "missing_in_src": 0, "tails": tails,
                      "collisions": collisions,
                      "issues_with_mismatches": mismatched,
                      "fidelity_pct": round(100 * (common - mismatched) /
                                            common, 2) if common else 100.0}}


def cfg(n_missing=0):
    return {"areas": {}, "findings": [
        {"area": "statuses", "name": f"S{i}", "kind": "missing_in_tgt",
         "detail": {}} for i in range(n_missing)]}


def test_clean():
    s = build_run_summary({"AC": proj()}, cfg(), [])
    assert s["verdict"] == "CLEAN"
    assert s["stats"]["issues_src_total"] == 100


def test_tails_only_is_clean_with_tails():
    s = build_run_summary({"AC": proj(tails=5)}, cfg(), [])
    assert s["verdict"] == "CLEAN_WITH_TAILS"
    assert any("tail" in h.lower() for h in s["headlines"])


def test_mismatches_or_config_gaps_are_gaps_found():
    assert build_run_summary({"AC": proj(mismatched=3)}, cfg(), [])["verdict"] \
        == "GAPS_FOUND"
    assert build_run_summary({"AC": proj()}, cfg(5), [])["verdict"] == "GAPS_FOUND"


def test_holes_collisions_or_blindspots_are_critical():
    assert build_run_summary({"AC": proj(missing=2)}, cfg(), [])["verdict"] \
        == "CRITICAL"
    assert build_run_summary({"AC": proj(collisions=1)}, cfg(), [])["verdict"] \
        == "CRITICAL"
    bs = [{"key": "MS", "search_count": 0, "insight_count": 16016,
           "blind_spot": True}]
    s = build_run_summary({"AC": proj()}, cfg(), bs)
    assert s["verdict"] == "CRITICAL"
    assert any("blind" in h.lower() for h in s["headlines"])


def test_headlines_name_the_worst_project():
    s = build_run_summary({"AC": proj(missing=7)}, cfg(), [])
    assert any("AC" in h and "7" in h for h in s["headlines"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_findings.py -q`
Expected: `ModuleNotFoundError: No module named 'auditor.findings'`.

- [ ] **Step 3: Write the implementation**

`auditor/findings.py`:
```python
"""Run-level summary: aggregate stats, verdict, and prose headlines.

Verdict ladder (worst wins):
  CRITICAL          genuine holes (missing below the cutover line), key
                    collisions, or unresolved permission blind spots —
                    the audit cannot be called clean.
  GAPS_FOUND        field/content mismatches or config objects missing in
                    the target. Data is present but not faithful/complete.
  CLEAN_WITH_TAILS  only post-cutover drift (expected when a source stays
                    live after the snapshot).
  CLEAN             nothing found.
"""
from __future__ import annotations


def build_run_summary(project_results: dict, config_result: dict,
                      blind_spots: list) -> dict:
    stats_list = [r["stats"] for r in project_results.values()]
    holes = sum(s.get("missing_in_tgt", 0) for s in stats_list)
    tails = sum(s.get("tails", 0) for s in stats_list)
    collisions = sum(s.get("collisions", 0) for s in stats_list)
    mismatched = sum(s.get("issues_with_mismatches", 0) for s in stats_list)
    cfg_missing = sum(1 for f in config_result.get("findings", [])
                      if f["kind"] == "missing_in_tgt")
    cfg_other = sum(1 for f in config_result.get("findings", [])
                    if f["kind"] != "missing_in_tgt")
    live_blind = [b for b in blind_spots if b.get("blind_spot")]

    if holes or collisions or live_blind:
        verdict = "CRITICAL"
    elif mismatched or cfg_missing or cfg_other:
        verdict = "GAPS_FOUND"
    elif tails:
        verdict = "CLEAN_WITH_TAILS"
    else:
        verdict = "CLEAN"

    headlines: list[str] = []
    for b in live_blind:
        headlines.append(
            f"Permission blind spot on {b['key']}: search sees "
            f"{b.get('search_count')} of {b.get('insight_count')} issues. "
            f"Counts below it are NOT trustworthy until access is fixed.")
    worst = sorted(stats_list, key=lambda s: -s.get("missing_in_tgt", 0))
    if worst and worst[0].get("missing_in_tgt"):
        w = worst[0]
        headlines.append(
            f"{w['project']} has {w['missing_in_tgt']} issues missing in the "
            f"target below the cutover line. This is genuine data loss until "
            f"proven otherwise.")
    if collisions:
        headlines.append(
            f"{collisions} key collision(s): same key, different issue "
            f"identity on each side. Treat matched-field stats for those "
            f"keys as noise.")
    if tails and not holes:
        headlines.append(
            f"{tails} issue(s) exist only as post-cutover tail (created "
            f"after the snapshot). Expected drift, not loss.")
    if mismatched:
        headlines.append(
            f"{mismatched} migrated issue(s) have at least one field or "
            f"content difference.")
    if cfg_missing:
        headlines.append(
            f"{cfg_missing} config object(s) from the source are missing in "
            f"the target (statuses, fields, screens, schemes or JSM objects).")
    if not headlines:
        headlines.append("Every audited issue and config object matched. "
                         "Clean migration.")

    return {
        "stats": {
            "projects": len(stats_list),
            "issues_src_total": sum(s.get("src", 0) for s in stats_list),
            "issues_tgt_total": sum(s.get("tgt", 0) for s in stats_list),
            "holes": holes, "tails": tails, "collisions": collisions,
            "issues_with_mismatches": mismatched,
            "config_missing": cfg_missing, "config_other": cfg_other,
            "blind_spots": len(live_blind),
        },
        "verdict": verdict,
        "headlines": headlines,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_findings.py -q`
Expected: `5 passed`.

- [ ] **Step 5: Commit**

```bash
git add auditor/findings.py tests/test_findings.py
git commit -m "feat: run summary with verdict ladder and prose headlines"
```

---

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

### Task 11: `webapp/runs.py` — run engine

**Files:**
- Create: `webapp/runs.py`
- Test: `tests/test_runs.py`

The engine owns the phase state machine. Core stage functions are injected as a `stages` dict so tests run instantly with stubs and `main.py` wires the real ones. Elevation is NOT a phase action — the permissions phase only detects and records; elevation happens via an explicit endpoint between runs (spec §6: consent-gated), after which the operator re-runs.

> **Final-review amendment (spec §6 phase 7 — elevation auto-undo at finalize).**
> `RunEngine.__init__` takes an injected `elevation_undo` callable (default no-op; `create_app` wires the real one). At `finalize` — after the run is marked done — and best-effort in the failure path, the engine calls it to auto-de-grant elevation. The undo is **migration-scoped**: it undoes every still-active elevation recorded across ALL of the migration's runs (each keyed `elevation:{run_id}:{side}` in settings), not just the current run's. This is provably safe because the active-run guard means only one run per migration is ever in-flight, so it can never strip a grant a live run needs; it bounds the privilege window to ≤1 run and is self-healing. The real callable lives in `webapp/stages.py` as `undo_migration_elevations(store, migration_id, src, tgt, log=None)` (it needs both `build_clients`/the store side and the core `undo_elevation`); injecting it keeps `runs.py` free of a `stages` import and keeps the engine testable with a stub. A `None` client (e.g. verify failed before `ctx["src"]`/`["tgt"]` were set) skips that side.

- [ ] **Step 1: Write the failing tests**

`tests/test_runs.py`:
```python
import time
import pytest
from webapp.runs import PHASES, RunEngine
from webapp.store import Store


@pytest.fixture()
def store(tmp_path):
    return Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))


def ok_stages(record=None):
    rec = record if record is not None else []
    def stage(name):
        def fn(ctx):
            rec.append(name)
            if name == "compare":
                ctx["project_results"] = {"AC": {"stats": {
                    "project": "AC", "src": 10, "tgt": 10, "common": 10,
                    "missing_in_tgt": 0, "missing_in_src": 0, "tails": 0,
                    "collisions": 0, "issues_with_mismatches": 0,
                    "fidelity_pct": 100.0}}}
                ctx["issue_findings"] = []
            if name == "config":
                ctx["config_result"] = {"areas": {}, "findings": []}
            if name == "permissions":
                ctx["blind_spots"] = []
        return fn
    return {p: stage(p) for p in PHASES if p != "finalize"}, rec


def wait_done(store, rid, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = store.get_run(rid)
        if r["status"] != "running":
            return r
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def test_happy_path_runs_all_phases_and_finalizes(store, tmp_path):
    stages, rec = ok_stages()
    eng = RunEngine(store, str(tmp_path / "ws"), stages=stages)
    mid = store.create_migration("m")
    rid = eng.start(mid, {"projects": ["AC"]})
    r = wait_done(store, rid)
    assert r["status"] == "done" and r["verdict"] == "CLEAN"
    assert rec == [p for p in PHASES if p != "finalize"]
    evs = store.get_events(rid)
    assert any("finalize" == e["phase"] for e in evs)


def test_failing_phase_marks_run_failed_with_event(store, tmp_path):
    stages, _ = ok_stages()
    def boom(ctx):
        raise RuntimeError("source unreachable")
    stages["extract"] = boom
    eng = RunEngine(store, str(tmp_path / "ws"), stages=stages)
    mid = store.create_migration("m")
    rid = eng.start(mid, {})
    r = wait_done(store, rid)
    assert r["status"] == "failed"
    msgs = [e["message"] for e in store.get_events(rid)]
    assert any("source unreachable" in m for m in msgs)


def test_second_start_while_active_raises(store, tmp_path):
    started = []
    stages, _ = ok_stages(started)
    import threading
    gate = threading.Event()
    def slow(ctx):
        gate.wait(2)
    stages["verify"] = slow
    eng = RunEngine(store, str(tmp_path / "ws"), stages=stages)
    mid = store.create_migration("m")
    rid = eng.start(mid, {})
    with pytest.raises(RuntimeError):
        eng.start(mid, {})
    gate.set()
    wait_done(store, rid)


def test_cancel_stops_between_phases(store, tmp_path):
    stages, rec = ok_stages()
    eng = RunEngine(store, str(tmp_path / "ws"), stages=stages)
    mid = store.create_migration("m")
    # cancel before the thread even starts by pre-setting the flag via start+cancel
    import threading
    hold = threading.Event()
    def first(ctx):
        hold.wait(2)
    stages["verify"] = first
    rid = eng.start(mid, {})
    eng.cancel(rid)
    hold.set()
    r = wait_done(store, rid)
    assert r["status"] == "cancelled"
    assert "scope" not in rec        # no phase after the cancel point ran


def test_mark_stale_failed(store, tmp_path):
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})        # simulates a run orphaned by restart
    eng = RunEngine(store, str(tmp_path / "ws"), stages={})
    n = eng.mark_stale_failed()
    assert n == 1 and store.get_run(rid)["status"] == "failed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_runs.py -q`
Expected: `ModuleNotFoundError: No module named 'webapp.runs'`.

- [ ] **Step 3: Write the implementation**

`webapp/runs.py`:
```python
"""Background run engine: one thread per run, phase state machine, events.

Stages are injected callables `fn(ctx)` keyed by phase name; ctx is a dict
the stages share (clients, params, results). The engine owns persistence:
phase transitions, events, findings, the final verdict. Swapping the thread
for a queue worker later only touches this file (spec §9).
"""
from __future__ import annotations

import os
import threading
import traceback

from auditor.findings import build_run_summary
from .store import Store

PHASES = ["verify", "scope", "permissions", "extract", "compare", "config",
          "finalize"]


class RunEngine:
    def __init__(self, store: Store, workspace_root: str, stages: dict | None = None):
        self.store = store
        self.workspace_root = workspace_root
        self.stages = stages or {}
        self._cancelled: set[int] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ lifecycle
    def start(self, migration_id: int, params: dict) -> int:
        # Hold the lock across check-then-create so two concurrent start() calls
        # can't both pass the active-run guard (TOCTOU -> duplicate audit threads).
        with self._lock:
            if self.store.active_run(migration_id):
                raise RuntimeError("a run is already active for this migration")
            run_id = self.store.create_run(migration_id, params)
        # Resumability (spec §6): re-running with reuse_extracts_from points
        # this run at the PRIOR run's workspace so cached gz extracts are
        # reused and stage_extract skips re-pulling them.
        ws_run = params.get("reuse_extracts_from") or run_id
        ws = os.path.join(self.workspace_root, str(migration_id), str(ws_run))
        os.makedirs(os.path.join(ws, "src"), exist_ok=True)
        os.makedirs(os.path.join(ws, "tgt"), exist_ok=True)
        t = threading.Thread(target=self._execute,
                             args=(run_id, migration_id, params, ws),
                             daemon=True, name=f"run-{run_id}")
        t.start()
        return run_id

    def cancel(self, run_id: int) -> None:
        with self._lock:
            self._cancelled.add(run_id)
        self.store.add_event(run_id, "engine", "warn", "cancel requested")

    def mark_stale_failed(self) -> int:
        stale = self.store.stale_running()
        for r in stale:
            self.store.update_run(r["id"], status="failed")
            self.store.add_event(r["id"], "engine", "error",
                                 "marked failed: server restarted mid-run")
        return len(stale)

    def _is_cancelled(self, run_id: int) -> bool:
        with self._lock:
            return run_id in self._cancelled

    # -------------------------------------------------------------- execute
    def _execute(self, run_id: int, migration_id: int, params: dict, ws: str):
        store = self.store
        ctx = {"run_id": run_id, "migration_id": migration_id,
               "params": params, "workspace": ws, "store": store,
               "project_results": {}, "issue_findings": [],
               "config_result": {"areas": {}, "findings": []},
               "blind_spots": []}

        def say(phase, msg, level="info"):
            store.add_event(run_id, phase, level, msg)

        try:
            for phase in PHASES:
                if self._is_cancelled(run_id):
                    store.update_run(run_id, status="cancelled")
                    say("engine", "run cancelled", "warn")
                    return
                store.update_run(run_id, phase=phase)
                say(phase, f"phase started: {phase}")
                if phase == "finalize":
                    summary = build_run_summary(ctx["project_results"],
                                                ctx["config_result"],
                                                ctx["blind_spots"])
                    if ctx["issue_findings"]:
                        store.insert_findings_issue(run_id, ctx["issue_findings"])
                    if ctx["config_result"].get("findings"):
                        store.insert_findings_config(
                            run_id, ctx["config_result"]["findings"])
                    stats = dict(summary["stats"])
                    stats["headlines"] = summary["headlines"]
                    stats["areas"] = ctx["config_result"].get("areas", {})
                    stats["project_stats"] = {
                        k: v["stats"] for k, v in ctx["project_results"].items()}
                    store.update_run(run_id, status="done",
                                     verdict=summary["verdict"], stats=stats)
                    say(phase, f"run complete: verdict={summary['verdict']}")
                    return
                fn = self.stages.get(phase)
                if fn is not None:
                    fn(ctx)
                say(phase, f"phase done: {phase}")
        except Exception as exc:  # noqa: BLE001 — any stage failure must land in the run record, not a dead thread
            say("engine", f"run failed: {exc}", "error")
            say("engine", traceback.format_exc()[-1500:], "error")
            store.update_run(run_id, status="failed")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_runs.py -q`
Expected: `5 passed`.

- [ ] **Step 5: Commit**

```bash
git add webapp/runs.py tests/test_runs.py
git commit -m "feat: threaded run engine with phase state machine, cancel, stale-run recovery"
```

---

### Task 12: `webapp/stages.py` — real stage wiring (core ↔ engine)

**Files:**
- Create: `webapp/stages.py`
- Test: `tests/test_stages.py`

Builds the production `stages` dict: constructs `Connection`/`JiraClient` for both sides from stored connections (with OAuth refresh persistence wired into the store), then calls the core functions and shapes ctx. This is the only file that knows both worlds.

- [ ] **Step 1: Write the failing tests**

`tests/test_stages.py`:
```python
import httpx
import pytest
from webapp.stages import build_clients, build_stages
from webapp.store import Store


@pytest.fixture()
def store(tmp_path):
    return Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))


def test_build_clients_pat_and_oauth(store):
    mid = store.create_migration("m")
    store.save_connection(mid, "source", "pat", "https://s.atlassian.net",
                          secret={"email": "a@b.c", "token": "tok"})
    store.save_connection(mid, "target", "oauth", "https://t.atlassian.net",
                          cloud_id="cid-9",
                          secret={"access_token": "at", "refresh_token": "rt",
                                  "expires_at": 9e12})
    src, tgt = build_clients(store, mid, http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    assert src.conn.auth_type == "pat" and src.conn.email == "a@b.c"
    assert tgt.conn.auth_type == "oauth" and tgt.conn.cloud_id == "cid-9"
    assert src.conn.api_base == "https://s.atlassian.net"
    assert tgt.conn.api_base == "https://api.atlassian.com/ex/jira/cid-9"


def test_oauth_refresh_persists_back_to_store(store):
    mid = store.create_migration("m")
    store.save_connection(mid, "source", "oauth", "https://s.atlassian.net",
                          cloud_id="c1",
                          secret={"access_token": "old", "refresh_token": "rt1",
                                  "expires_at": 1})   # expired -> proactive refresh
    calls = {"n": 0}
    def handler(req):
        if "auth.atlassian.com" in str(req.url):
            calls["n"] += 1
            return httpx.Response(200, json={"access_token": "new",
                                             "refresh_token": "rt2",
                                             "expires_in": 3600})
        return httpx.Response(200, json={"ok": 1})
    store.settings_set("oauth_client_id", "cid")
    store.settings_set("oauth_client_secret_enc",
                       store.encrypt({"secret": "sec"}).decode())
    src, _tgt_missing = build_clients(store, mid,
                                      http=httpx.Client(
                                          transport=httpx.MockTransport(handler)),
                                      require_both=False)
    st, _ = src.req("/rest/api/3/myself")
    assert st == 200 and calls["n"] == 1
    row = store.get_connection(mid, "source")
    sec = store.connection_secret(row)
    assert sec["refresh_token"] == "rt2" and sec["access_token"] == "new"


def test_build_stages_returns_all_engine_phases():
    from webapp.runs import PHASES
    stages = build_stages()
    for p in PHASES:
        if p == "finalize":
            continue
        assert p in stages and callable(stages[p])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_stages.py -q`
Expected: `ModuleNotFoundError: No module named 'webapp.stages'`.

- [ ] **Step 3: Write the implementation**

`webapp/stages.py`:
```python
"""Production stage functions: the only module that knows both the core
library and the store. Each stage is fn(ctx); ctx comes from RunEngine.

ctx keys written here and consumed downstream:
  clients (src, tgt) · scope rows · blind_spots · project_results ·
  issue_findings · config_result
"""
from __future__ import annotations

import os

import httpx

from auditor import compare as compare_mod
from auditor import config_audit as config_mod
from auditor import extract as extract_mod
from auditor import permissions as perm_mod
from auditor import scope as scope_mod
from auditor.client import Connection, JiraClient
from . import oauth as oauth_mod
from .store import Store


def _oauth_secret(store: Store) -> tuple[str | None, str | None]:
    cid = store.settings_get("oauth_client_id")
    enc = store.settings_get("oauth_client_secret_enc")
    sec = store.decrypt(enc.encode())["secret"] if enc else None
    return cid, sec


def build_clients(store: Store, migration_id: int,
                  http: httpx.Client | None = None,
                  require_both: bool = True):
    out = []
    cid, csec = _oauth_secret(store)
    for role in ("source", "target"):
        row = store.get_connection(migration_id, role)
        if row is None:
            if require_both:
                raise RuntimeError(f"no {role} connection configured")
            out.append(None)
            continue
        secret = store.connection_secret(row)
        if row["auth_type"] == "pat":
            conn = Connection(auth_type="pat", site_url=row["site_url"],
                              email=secret["email"], api_token=secret["token"])
        else:
            conn = Connection(auth_type="oauth", site_url=row["site_url"],
                              cloud_id=row["cloud_id"],
                              access_token=secret.get("access_token"),
                              refresh_token=secret.get("refresh_token"),
                              expires_at=float(secret.get("expires_at") or 0))
            conn_id = row["id"]
            if cid and csec:
                conn.refresh_fn = lambda rt, _cid=cid, _cs=csec: \
                    oauth_mod.refresh_tokens(_cid, _cs, rt, http=http)
            conn.on_tokens_refreshed = lambda c, _id=conn_id: \
                store.update_connection_secret(_id, {
                    "access_token": c.access_token,
                    "refresh_token": c.refresh_token,
                    "expires_at": c.expires_at})
        out.append(JiraClient(conn, http=http))
    return out[0], out[1]


# ------------------------------------------------------------------ stages
def _say(ctx, phase, msg, level="info"):
    ctx["store"].add_event(ctx["run_id"], phase, level, msg)


def stage_verify(ctx):
    store: Store = ctx["store"]
    src, tgt = build_clients(store, ctx["migration_id"])
    ctx["src"], ctx["tgt"] = src, tgt
    for role, cl in (("source", src), ("target", tgt)):
        me = cl.myself()    # raises ClientError loudly on auth failure
        row = store.get_connection(ctx["migration_id"], role)
        store.mark_connection_verified(row["id"],
                                       me.get("emailAddress") or "")
        ctx[f"{role}_account_id"] = me.get("accountId")
        _say(ctx, "verify", f"{role}: authenticated as "
             f"{me.get('displayName', '?')}")


def stage_scope(ctx):
    src, tgt = ctx["src"], ctx["tgt"]
    sp, serr = src.all_projects()
    tp, terr = tgt.all_projects()
    if serr or terr:
        raise RuntimeError(f"project enumeration failed: src={serr} tgt={terr}")
    matched = scope_mod.match_projects(sp, tp)
    selected = ctx["params"].get("projects") or \
        [m["key"] for m in matched["matched"]]
    ctx["selected"] = [m for m in matched["matched"] if m["key"] in selected]
    ctx["scope"] = matched
    rows = []
    for m in ctx["selected"]:
        m["src_count"] = src.approx_count(f'project = "{m["key"]}"')
        m["tgt_count"] = tgt.approx_count(f'project = "{m["key"]}"')
        rows.append({"key": m["key"], "name": m["name"],
                     "src_count": m["src_count"] if isinstance(m["src_count"], int) else None,
                     "tgt_count": m["tgt_count"] if isinstance(m["tgt_count"], int) else None,
                     "status": "scoped"})
    ctx["store"].set_run_projects(ctx["run_id"], rows)
    _say(ctx, "scope", f"{len(ctx['selected'])} project(s) in scope; "
         f"{len(matched['source_only'])} source-only, "
         f"{len(matched['target_only'])} target-only")


def stage_permissions(ctx):
    keys = [m["key"] for m in ctx["selected"]]
    spots = []
    for side, cl in (("source", ctx["src"]), ("target", ctx["tgt"])):
        for s in perm_mod.detect_blind_spots(cl, keys):
            s["side"] = side
            spots.append(s)
            if s["blind_spot"]:
                _say(ctx, "permissions",
                     f"BLIND SPOT on {side} {s['key']}: search sees "
                     f"{s['search_count']} of {s['insight_count']}. Fix access "
                     f"(elevation) and re-run before trusting counts.", "warn")
    ctx["blind_spots"] = spots
    rows = ctx["store"].get_run_projects(ctx["run_id"])
    blind_keys = {s["key"] for s in spots if s["blind_spot"]}
    for r in rows:
        r["blind_spot"] = 1 if r["key"] in blind_keys else 0
    ctx["store"].set_run_projects(ctx["run_id"], rows)


def stage_extract(ctx):
    reuse = bool(ctx["params"].get("reuse_extracts_from"))
    for m in ctx["selected"]:
        for side, cl in (("src", ctx["src"]), ("tgt", ctx["tgt"])):
            path = os.path.join(ctx["workspace"], side,
                                f"{m['key']}.core.jsonl.gz")
            if reuse and os.path.exists(path):
                _say(ctx, "extract",
                     f"{side} {m['key']}: reusing cached extract")
                continue
            total = m["src_count"] if side == "src" else m["tgt_count"]
            res = extract_mod.extract_project(
                cl, m["key"], path,
                progress=lambda n, k=m["key"], s=side, t=total: _say(
                    ctx, "extract", f"{s} {k}: {n}/{t if isinstance(t, int) else '?'}"))
            if not res["verified"]:
                if isinstance(res["approx"], int):
                    # Hard mismatch gates the compare phase (spec §10): a
                    # silent pagination gap must never feed the diff.
                    raise RuntimeError(
                        f"{side} {m['key']}: extracted {res['extracted']} but "
                        f"approximate-count says {res['approx']} — extraction "
                        f"not complete, refusing to compare")
                _say(ctx, "extract",
                     f"{side} {m['key']}: approximate-count unavailable "
                     f"({res['approx']}); proceeding on extracted="
                     f"{res['extracted']}", "warn")


def stage_compare(ctx):
    results, all_findings = {}, []
    for m in ctx["selected"]:
        out = compare_mod.compare_project(
            m["key"],
            os.path.join(ctx["workspace"], "src", f"{m['key']}.core.jsonl.gz"),
            os.path.join(ctx["workspace"], "tgt", f"{m['key']}.core.jsonl.gz"))
        results[m["key"]] = out
        all_findings.extend(out["findings"])
        s = out["stats"]
        _say(ctx, "compare",
             f"{m['key']}: common={s['common']} holes={s['missing_in_tgt']} "
             f"tails={s['tails']} mismatched={s['issues_with_mismatches']} "
             f"fidelity={s['fidelity_pct']}%")
    ctx["project_results"] = results
    ctx["issue_findings"] = all_findings
    rows = ctx["store"].get_run_projects(ctx["run_id"])
    for r in rows:
        st = results.get(r["key"], {}).get("stats")
        if st:
            r.update({"missing": st["missing_in_tgt"], "tail_count": st["tails"],
                      "fidelity_pct": st["fidelity_pct"], "status": "compared"})
    ctx["store"].set_run_projects(ctx["run_id"], rows)


def stage_config(ctx):
    jsm = ctx["params"].get("jsm_projects") or \
        [m["key"] for m in ctx["selected"]]
    ctx["config_result"] = config_mod.audit_config(
        ctx["src"], ctx["tgt"], jsm_projects=jsm,
        progress=lambda msg: _say(ctx, "config", msg))


def build_stages() -> dict:
    return {"verify": stage_verify, "scope": stage_scope,
            "permissions": stage_permissions, "extract": stage_extract,
            "compare": stage_compare, "config": stage_config}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_stages.py -q`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add webapp/stages.py tests/test_stages.py
git commit -m "feat: production stage wiring (clients from store, core stages into engine ctx)"
```

---

### Task 13: `webapp/analysis.py` — analysis JSON API

**Files:**
- Create: `webapp/analysis.py`
- Test: `tests/test_analysis.py`

FastAPI router with the JSON endpoints the analysis pages consume. App wiring (templates, full routes) lands in Task 14; this router is mounted there. For tests, mount the router on a bare FastAPI app with a seeded store.

- [ ] **Step 1: Write the failing tests**

`tests/test_analysis.py`:
```python
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from webapp.analysis import make_router
from webapp.store import Store


@pytest.fixture()
def client(tmp_path):
    store = Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    store.update_run(rid, status="done", verdict="GAPS_FOUND", stats={
        "projects": 1, "issues_src_total": 10, "issues_tgt_total": 9,
        "holes": 1, "tails": 0, "collisions": 0, "issues_with_mismatches": 2,
        "config_missing": 1, "config_other": 0, "blind_spots": 0,
        "headlines": ["AC has 1 issue missing"],
        "project_stats": {"AC": {"src": 10, "tgt": 9, "fidelity_pct": 80.0}},
        "areas": {"statuses": {"src": 2, "tgt": 1}}})
    store.set_run_projects(rid, [{"key": "AC", "name": "AC Support",
                                  "src_count": 10, "tgt_count": 9, "missing": 1,
                                  "tail_count": 0, "fidelity_pct": 80.0,
                                  "blind_spot": 0, "status": "compared"}])
    store.insert_findings_issue(rid, [
        {"project": "AC", "kind": "missing_in_tgt", "src_key": "AC-2",
         "tgt_key": None, "field": None, "summary": "lost issue", "detail": {}},
        {"project": "AC", "kind": "field_mismatch", "src_key": "AC-3",
         "tgt_key": "AC-3", "field": "status",
         "summary": "status differs", "detail": {"src": "Open", "tgt": "Done"}}])
    store.insert_findings_config(rid, [
        {"area": "statuses", "name": "On Hold", "kind": "missing_in_tgt",
         "detail": {}}])
    store.add_event(rid, "compare", "info", "AC compared")
    app = FastAPI()
    app.state.store = store
    app.include_router(make_router())
    c = TestClient(app)
    c.rid = rid
    return c


def test_summary(client):
    d = client.get(f"/api/runs/{client.rid}/summary").json()
    assert d["verdict"] == "GAPS_FOUND"
    assert d["stats"]["holes"] == 1
    assert d["headlines"] == ["AC has 1 issue missing"]


def test_projects(client):
    d = client.get(f"/api/runs/{client.rid}/projects").json()
    assert d[0]["key"] == "AC" and d[0]["fidelity_pct"] == 80.0


def test_issues_filters_and_pagination(client):
    d = client.get(f"/api/runs/{client.rid}/issues",
                   params={"kind": "field_mismatch"}).json()
    assert d["total"] == 1 and d["rows"][0]["field"] == "status"
    assert isinstance(d["rows"][0]["detail"], dict)
    d2 = client.get(f"/api/runs/{client.rid}/issues",
                    params={"q": "lost"}).json()
    assert d2["total"] == 1 and d2["rows"][0]["src_key"] == "AC-2"
    d3 = client.get(f"/api/runs/{client.rid}/issues",
                    params={"page": 2, "size": 1}).json()
    assert d3["total"] == 2 and len(d3["rows"]) == 1


def test_kind_counts(client):
    d = client.get(f"/api/runs/{client.rid}/issues/kinds").json()
    assert d == {"missing_in_tgt": 1, "field_mismatch": 1}


def test_config_areas_and_rows(client):
    areas = client.get(f"/api/runs/{client.rid}/config").json()
    assert areas["areas"] == ["statuses"]
    rows = client.get(f"/api/runs/{client.rid}/config",
                      params={"area": "statuses"}).json()
    assert rows["rows"][0]["name"] == "On Hold"


def test_events_incremental(client):
    evs = client.get(f"/api/runs/{client.rid}/events").json()
    assert evs[-1]["message"] == "AC compared"
    none = client.get(f"/api/runs/{client.rid}/events",
                      params={"after": evs[-1]["id"]}).json()
    assert none == []


def test_unknown_run_404(client):
    assert client.get("/api/runs/999/summary").status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_analysis.py -q`
Expected: `ModuleNotFoundError: No module named 'webapp.analysis'`.

- [ ] **Step 3: Write the implementation**

`webapp/analysis.py`:
```python
"""JSON API powering the analysis UI. Server-side pagination/filtering so a
40k-issue run never ships to the browser at once."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request


def _store(request: Request):
    return request.app.state.store


def _run_or_404(store, run_id: int) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run


def make_router() -> APIRouter:
    r = APIRouter()

    @r.get("/api/runs/{run_id}/summary")
    def summary(run_id: int, request: Request):
        store = _store(request)
        run = _run_or_404(store, run_id)
        stats = json.loads(run["stats_json"] or "{}")
        return {"run_id": run_id, "status": run["status"],
                "phase": run["phase"], "verdict": run["verdict"],
                "started_at": run["started_at"],
                "finished_at": run["finished_at"],
                "headlines": stats.pop("headlines", []),
                "project_stats": stats.pop("project_stats", {}),
                "areas": stats.pop("areas", {}),
                "stats": stats}

    @r.get("/api/runs/{run_id}/projects")
    def projects(run_id: int, request: Request):
        store = _store(request)
        _run_or_404(store, run_id)
        return store.get_run_projects(run_id)

    @r.get("/api/runs/{run_id}/issues")
    def issues(run_id: int, request: Request, project: str | None = None,
               kind: str | None = None, q: str | None = None,
               page: int = 1, size: int = 50):
        store = _store(request)
        _run_or_404(store, run_id)
        size = max(1, min(size, 200))
        rows, total = store.query_issues(run_id, project=project, kind=kind,
                                         q=q, page=max(1, page), size=size)
        for row in rows:
            row["detail"] = json.loads(row.pop("detail_json") or "{}")
        return {"rows": rows, "total": total, "page": page, "size": size}

    @r.get("/api/runs/{run_id}/issues/kinds")
    def kinds(run_id: int, request: Request, project: str | None = None):
        store = _store(request)
        _run_or_404(store, run_id)
        return store.issue_kind_counts(run_id, project=project)

    @r.get("/api/runs/{run_id}/config")
    def config(run_id: int, request: Request, area: str | None = None):
        store = _store(request)
        _run_or_404(store, run_id)
        if area is None:
            return {"areas": store.config_areas(run_id)}
        rows = store.query_config(run_id, area)
        for row in rows:
            row["detail"] = json.loads(row.pop("detail_json") or "{}")
        return {"rows": rows}

    @r.get("/api/runs/{run_id}/events")
    def events(run_id: int, request: Request, after: int = 0):
        store = _store(request)
        _run_or_404(store, run_id)
        return store.get_events(run_id, after_id=after)

    return r
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_analysis.py -q`
Expected: `7 passed`.

- [ ] **Step 5: Commit**

```bash
git add webapp/analysis.py tests/test_analysis.py
git commit -m "feat: analysis JSON API (summary, projects, paginated issues, config, events)"
```

---

### Task 14: `webapp/main.py` + templates + static — the web app and analysis UI

**Files:**
- Create: `webapp/main.py`, `webapp/templates/base.html`, `webapp/templates/index.html`, `webapp/templates/settings.html`, `webapp/templates/migration.html`, `webapp/templates/run.html`, `webapp/templates/analysis.html`, `webapp/templates/elevate.html`, `webapp/static/app.css`, `webapp/static/app.js`
- Test: `tests/test_main.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_main.py`:
```python
import httpx
import pytest
from fastapi.testclient import TestClient
from webapp.config import Config
from webapp.main import create_app


def mk_app(tmp_path, handler=None):
    cfg = Config(data_dir=str(tmp_path / "data"), bind_host="127.0.0.1",
                 bind_port=8484, public_base_url="http://localhost:8484",
                 secret_key=None)
    http = httpx.Client(transport=httpx.MockTransport(handler)) if handler else None
    app = create_app(cfg, http=http)
    return app, TestClient(app, follow_redirects=False)


def ok_jira(req):
    p = str(req.url.path)
    if p.endswith("/myself"):
        return httpx.Response(200, json={"displayName": "Igor",
                                         "emailAddress": "i@x.y",
                                         "accountId": "acc-1"})
    if p.endswith("project/search"):
        return httpx.Response(200, json={"isLast": True, "values": [
            {"key": "AC", "name": "AC Support", "id": "1",
             "insight": {"totalIssueCount": 5}}]})
    if p.endswith("approximate-count"):
        return httpx.Response(200, json={"count": 5})
    if "/role" in p and req.method == "POST":
        return httpx.Response(200, json={})
    if "/role" in p and req.method == "DELETE":
        return httpx.Response(204)
    if p.endswith("/rest/api/3/role"):
        return httpx.Response(200, json=[{"name": "Administrators", "id": 9}])
    return httpx.Response(200, json={"values": [], "isLast": True})


def test_dashboard_renders(tmp_path):
    _, c = mk_app(tmp_path)
    r = c.get("/")
    assert r.status_code == 200 and "Migration Auditor" in r.text


def test_create_migration_and_page(tmp_path):
    app, c = mk_app(tmp_path)
    r = c.post("/migrations", data={"name": "acme->globex"})
    assert r.status_code == 303
    loc = r.headers["location"]
    page = c.get(loc)
    assert page.status_code == 200
    assert ("acme-&gt;globex" in page.text) or ("acme->globex" in page.text)


def test_pat_connection_save_and_verify(tmp_path):
    app, c = mk_app(tmp_path, handler=ok_jira)
    c.post("/migrations", data={"name": "m"})
    r = c.post("/migrations/1/connections",
               data={"role": "source", "site_url": "https://s.atlassian.net",
                     "email": "i@x.y", "api_token": "tok"})
    assert r.status_code == 303
    page = c.get("/migrations/1")
    assert "verified" in page.text
    row = app.state.store.get_connection(1, "source")
    assert row["status"] == "verified"
    assert b"tok" not in row["secret_enc"]


def test_pat_connection_bad_auth_shows_error(tmp_path):
    def deny(req):
        return httpx.Response(401, text="no")
    app, c = mk_app(tmp_path, handler=deny)
    c.post("/migrations", data={"name": "m"})
    r = c.post("/migrations/1/connections",
               data={"role": "source", "site_url": "https://s.atlassian.net",
                     "email": "i@x.y", "api_token": "bad"},
               follow_redirects=True)
    assert "could not authenticate" in r.text.lower()
    assert app.state.store.get_connection(1, "source") is None


def test_settings_roundtrip_encrypts_secret(tmp_path):
    app, c = mk_app(tmp_path)
    r = c.post("/settings", data={"oauth_client_id": "cid",
                                  "oauth_client_secret": "sek"})
    assert r.status_code == 303
    store = app.state.store
    assert store.settings_get("oauth_client_id") == "cid"
    enc = store.settings_get("oauth_client_secret_enc")
    assert "sek" not in enc
    assert store.decrypt(enc.encode())["secret"] == "sek"


def test_run_start_requires_both_connections(tmp_path):
    app, c = mk_app(tmp_path, handler=ok_jira)
    c.post("/migrations", data={"name": "m"})
    r = c.post("/migrations/1/runs", data={}, follow_redirects=True)
    assert "configure both connections" in r.text.lower()


def test_sse_stream_for_finished_run(tmp_path):
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    store.add_event(rid, "verify", "info", "hello-evt")
    store.update_run(rid, status="done")
    r = c.get(f"/runs/{rid}/stream")
    assert r.status_code == 200
    assert "hello-evt" in r.text and "event: done" in r.text


def test_run_and_analysis_pages_render(tmp_path):
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    store.update_run(rid, status="done", verdict="CLEAN",
                     stats={"headlines": [], "project_stats": {}, "areas": {}})
    assert c.get(f"/runs/{rid}").status_code == 200
    for view in ("", "/projects", "/config", "/issues", "/log"):
        resp = c.get(f"/runs/{rid}/analysis{view}")
        assert resp.status_code == 200, view
    assert c.get(f"/runs/{rid}/analysis/projects/AC").status_code == 200


def test_elevation_apply_and_undo(tmp_path):
    app, c = mk_app(tmp_path, handler=ok_jira)
    store = app.state.store
    c.post("/migrations", data={"name": "m"})
    c.post("/migrations/1/connections",
           data={"role": "source", "site_url": "https://s.atlassian.net",
                 "email": "i@x.y", "api_token": "t"})
    c.post("/migrations/1/connections",
           data={"role": "target", "site_url": "https://t.atlassian.net",
                 "email": "i@x.y", "api_token": "t"})
    rid = store.create_run(1, {})
    store.update_run(rid, status="done")
    store.set_run_projects(rid, [{"key": "AC", "name": "AC", "src_count": 5,
                                  "tgt_count": 0, "missing": None,
                                  "tail_count": None, "fidelity_pct": None,
                                  "blind_spot": 1, "status": "scoped"}])
    page = c.get(f"/runs/{rid}/elevate")
    assert page.status_code == 200 and "AC" in page.text
    r = c.post(f"/runs/{rid}/elevate", data={"side": "target"})
    assert r.status_code == 303
    log = store.settings_get(f"elevation:{rid}:target")
    assert log and '"ok": true' in log
    r2 = c.post(f"/runs/{rid}/elevate/undo", data={"side": "target"})
    assert r2.status_code == 303
    assert store.settings_get(f"elevation:{rid}:target") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_main.py -q`
Expected: `ModuleNotFoundError: No module named 'webapp.main'`.

- [ ] **Step 3: Write `webapp/main.py`**

```python
"""FastAPI app: wizard, runs, SSE, analysis pages, settings, elevation."""
from __future__ import annotations

import json
import os
import secrets
import time

import httpx
import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import (HTMLResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from auditor.client import ClientError, Connection, JiraClient
from auditor.permissions import (apply_elevation, find_admin_role_id,
                                 undo_elevation)
from .analysis import make_router
from .config import Config, load_config
from .oauth import accessible_resources, build_authorize_url, exchange_code
from .runs import RunEngine
from .stages import build_clients, build_stages
from .store import Store

_HERE = os.path.dirname(__file__)


def create_app(cfg: Config | None = None, http: httpx.Client | None = None) -> FastAPI:
    cfg = cfg or load_config()
    os.makedirs(cfg.data_dir, exist_ok=True)
    store = Store(db_path=cfg.db_path, key_path=cfg.key_path,
                  secret_key=cfg.secret_key)
    engine = RunEngine(store, os.path.join(cfg.data_dir, "migrations"),
                       stages=build_stages())
    engine.mark_stale_failed()

    app = FastAPI(title="Migration Auditor")
    app.state.store = store
    app.state.engine = engine
    app.state.config = cfg
    app.state.http = http            # injected mock in tests; None in prod
    app.state.oauth_pending = {}     # state -> {migration_id, role, tokens?}
    templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))
    app.mount("/static", StaticFiles(directory=os.path.join(_HERE, "static")),
              name="static")
    app.include_router(make_router())

    def page(request, name, **ctx):
        ctx.update({"request": request})
        return templates.TemplateResponse(name, ctx)

    def _mk_client(conn: Connection) -> JiraClient:
        return JiraClient(conn, http=app.state.http)

    # ------------------------------------------------------------ dashboard
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        migs = store.list_migrations()
        for m in migs:
            runs = store.list_runs(m["id"])
            m["last_run"] = runs[0] if runs else None
        return page(request, "index.html", migrations=migs)

    @app.post("/migrations")
    def create_migration(name: str = Form(...)):
        mid = store.create_migration(name.strip() or "untitled")
        return RedirectResponse(f"/migrations/{mid}", status_code=303)

    # ------------------------------------------------------------- settings
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, saved: int = 0):
        return page(request, "settings.html",
                    client_id=store.settings_get("oauth_client_id") or "",
                    has_secret=bool(store.settings_get("oauth_client_secret_enc")),
                    redirect_uri=cfg.oauth_redirect_uri, saved=saved)

    @app.post("/settings")
    def settings_save(oauth_client_id: str = Form(""),
                      oauth_client_secret: str = Form("")):
        store.settings_set("oauth_client_id", oauth_client_id.strip())
        if oauth_client_secret.strip():
            store.settings_set(
                "oauth_client_secret_enc",
                store.encrypt({"secret": oauth_client_secret.strip()}).decode())
        return RedirectResponse("/settings?saved=1", status_code=303)

    # ------------------------------------------------------- migration page
    @app.get("/migrations/{mid}", response_class=HTMLResponse)
    def migration_page(request: Request, mid: int, error: str = ""):
        mig = store.get_migration(mid)
        if mig is None:
            return RedirectResponse("/", status_code=303)
        conns = {role: store.get_connection(mid, role)
                 for role in ("source", "target")}
        return page(request, "migration.html", mig=mig, conns=conns,
                    runs=store.list_runs(mid), error=error,
                    oauth_ready=bool(store.settings_get("oauth_client_id")),
                    active=store.active_run(mid))

    @app.post("/migrations/{mid}/connections")
    def save_pat_connection(mid: int, role: str = Form(...),
                            site_url: str = Form(...), email: str = Form(...),
                            api_token: str = Form(...)):
        site = site_url.strip().rstrip("/")
        if not site.startswith("http"):
            site = "https://" + site
        conn = Connection(auth_type="pat", site_url=site,
                          email=email.strip(), api_token=api_token.strip())
        try:
            me = _mk_client(conn).myself()
        except ClientError as exc:
            return RedirectResponse(
                f"/migrations/{mid}?error=Could not authenticate {role}: "
                f"HTTP {exc.status}", status_code=303)
        store.save_connection(mid, role, "pat", site,
                              secret={"email": email.strip(),
                                      "token": api_token.strip()},
                              account_email=me.get("emailAddress"))
        row = store.get_connection(mid, role)
        store.mark_connection_verified(row["id"], me.get("emailAddress") or "")
        return RedirectResponse(f"/migrations/{mid}", status_code=303)

    # ----------------------------------------------------------- oauth flow
    @app.get("/oauth/start")
    def oauth_start(migration_id: int, role: str):
        client_id = store.settings_get("oauth_client_id")
        if not client_id:
            return RedirectResponse(
                f"/migrations/{migration_id}?error=Configure the OAuth client "
                f"in Settings first", status_code=303)
        state = secrets.token_urlsafe(24)
        app.state.oauth_pending[state] = {"migration_id": migration_id,
                                          "role": role}
        return RedirectResponse(build_authorize_url(
            client_id, cfg.oauth_redirect_uri, state), status_code=303)

    @app.get("/oauth/callback", response_class=HTMLResponse)
    def oauth_callback(request: Request, state: str = "", code: str = "",
                       error: str = ""):
        pend = app.state.oauth_pending.pop(state, None)
        if pend is None or error or not code:
            return page(request, "index.html", migrations=store.list_migrations(),
                        flash=f"OAuth failed: {error or 'invalid state'}")
        client_id = store.settings_get("oauth_client_id")
        enc = store.settings_get("oauth_client_secret_enc")
        secret = store.decrypt(enc.encode())["secret"] if enc else ""
        tokens = exchange_code(client_id, secret, code, cfg.oauth_redirect_uri,
                               http=app.state.http)
        sites = accessible_resources(tokens["access_token"], http=app.state.http)
        pend["tokens"] = tokens
        new_state = secrets.token_urlsafe(24)
        app.state.oauth_pending[new_state] = pend
        return page(request, "migration.html",
                    mig=store.get_migration(pend["migration_id"]),
                    conns={r: store.get_connection(pend["migration_id"], r)
                           for r in ("source", "target")},
                    runs=store.list_runs(pend["migration_id"]),
                    error="", oauth_ready=True,
                    active=store.active_run(pend["migration_id"]),
                    site_pick={"state": new_state, "sites": sites,
                               "role": pend["role"]})

    @app.post("/oauth/select")
    def oauth_select(state: str = Form(...), cloud_id: str = Form(...),
                     site_url: str = Form(...)):
        pend = app.state.oauth_pending.pop(state, None)
        if pend is None:
            return RedirectResponse("/", status_code=303)
        t = pend["tokens"]
        store.save_connection(
            pend["migration_id"], pend["role"], "oauth", site_url,
            cloud_id=cloud_id,
            secret={"access_token": t["access_token"],
                    "refresh_token": t.get("refresh_token"),
                    "expires_at": time.time() + float(t.get("expires_in", 3600))})
        return RedirectResponse(f"/migrations/{pend['migration_id']}",
                                status_code=303)

    # ----------------------------------------------------------------- runs
    @app.post("/migrations/{mid}/runs")
    def start_run(mid: int, projects: str = Form(""),
                  reuse_extracts_from: str = Form("")):
        if not (store.get_connection(mid, "source")
                and store.get_connection(mid, "target")):
            return RedirectResponse(
                f"/migrations/{mid}?error=Configure both connections first",
                status_code=303)
        params = {}
        keys = [k.strip() for k in projects.split(",") if k.strip()]
        if keys:
            params["projects"] = keys
        if reuse_extracts_from.strip().isdigit():
            params["reuse_extracts_from"] = int(reuse_extracts_from)
        try:
            rid = engine.start(mid, params)
        except RuntimeError as exc:
            return RedirectResponse(f"/migrations/{mid}?error={exc}",
                                    status_code=303)
        return RedirectResponse(f"/runs/{rid}", status_code=303)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: int):
        run = store.get_run(run_id)
        if run is None:
            return RedirectResponse("/", status_code=303)
        return page(request, "run.html", run=run,
                    mig=store.get_migration(run["migration_id"]))

    @app.post("/runs/{run_id}/cancel")
    def cancel_run(run_id: int):
        engine.cancel(run_id)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.get("/runs/{run_id}/stream")
    async def run_stream(run_id: int, request: Request):
        async def gen():
            last = 0
            while True:
                if await request.is_disconnected():
                    return
                for e in store.get_events(run_id, after_id=last):
                    last = e["id"]
                    yield f"data: {json.dumps(e)}\n\n"
                run = store.get_run(run_id)
                if run is None or run["status"] != "running":
                    yield "event: done\ndata: {}\n\n"
                    return
                await asyncio.sleep(1.0)
        return StreamingResponse(gen(), media_type="text/event-stream")

    # ------------------------------------------------------------- analysis
    @app.get("/runs/{run_id}/analysis", response_class=HTMLResponse)
    @app.get("/runs/{run_id}/analysis/{view}", response_class=HTMLResponse)
    @app.get("/runs/{run_id}/analysis/projects/{project}",
             response_class=HTMLResponse)
    def analysis_page(request: Request, run_id: int, view: str = "overview",
                      project: str = ""):
        run = store.get_run(run_id)
        if run is None:
            return RedirectResponse("/", status_code=303)
        if project:
            view = "project"
        src = store.get_connection(run["migration_id"], "source") or {}
        tgt = store.get_connection(run["migration_id"], "target") or {}
        return page(request, "analysis.html", run=run, view=view,
                    project=project,
                    mig=store.get_migration(run["migration_id"]),
                    src_base=(src.get("site_url") or "").rstrip("/"),
                    tgt_base=(tgt.get("site_url") or "").rstrip("/"))

    # ------------------------------------------------------------ elevation
    @app.get("/runs/{run_id}/elevate", response_class=HTMLResponse)
    def elevate_confirm(request: Request, run_id: int):
        run = store.get_run(run_id)
        rows = [r for r in store.get_run_projects(run_id) if r["blind_spot"]]
        return page(request, "elevate.html", run=run, rows=rows,
                    undo_src=store.settings_get(f"elevation:{run_id}:source"),
                    undo_tgt=store.settings_get(f"elevation:{run_id}:target"))

    def _side_client(run, side):
        src, tgt = build_clients(store, run["migration_id"],
                                 http=app.state.http)
        return src if side == "source" else tgt

    @app.post("/runs/{run_id}/elevate")
    def elevate_apply(run_id: int, side: str = Form(...)):
        run = store.get_run(run_id)
        cl = _side_client(run, side)
        me = cl.myself()
        role_id = find_admin_role_id(cl)
        blind = {r["key"] for r in store.get_run_projects(run_id)
                 if r["blind_spot"]}
        projects, _ = cl.all_projects()
        ids = [p["id"] for p in projects if p.get("key") in blind]
        grants = apply_elevation(cl, ids, role_id, me["accountId"])
        store.settings_set(f"elevation:{run_id}:{side}", json.dumps(
            {"role_id": role_id, "account_id": me["accountId"],
             "grants": grants}))
        store.add_event(run_id, "permissions", "warn",
                        f"elevation applied on {side}: "
                        f"{sum(1 for g in grants if g['ok'])}/{len(grants)} "
                        f"projects (undo available)")
        return RedirectResponse(f"/runs/{run_id}/elevate", status_code=303)

    @app.post("/runs/{run_id}/elevate/undo")
    def elevate_undo(run_id: int, side: str = Form(...)):
        run = store.get_run(run_id)
        raw = store.settings_get(f"elevation:{run_id}:{side}")
        if raw:
            data = json.loads(raw)
            cl = _side_client(run, side)
            undo_elevation(cl, data["grants"], data["role_id"],
                           data["account_id"])
            store.settings_delete(f"elevation:{run_id}:{side}")
            store.add_event(run_id, "permissions", "info",
                            f"elevation undone on {side}")
        return RedirectResponse(f"/runs/{run_id}/elevate", status_code=303)

    return app


def cli():
    import argparse
    ap = argparse.ArgumentParser(prog="migration-auditor")
    ap.add_argument("command", choices=["serve"])
    ap.parse_args()
    cfg = load_config()
    uvicorn.run(create_app(cfg), host=cfg.bind_host, port=cfg.bind_port)
```

- [ ] **Step 4: Write the templates**

`webapp/templates/base.html`:
```html
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Migration Auditor</title>
<link rel="stylesheet" href="/static/app.css">
</head><body>
<header class="masthead"><div class="wrap">
  <a class="brand" href="/">Migration Auditor</a>
  <nav><a href="/">Migrations</a><a href="/settings">Settings</a></nav>
</div></header>
<main class="wrap">
{% block content %}{% endblock %}
</main>
<script src="/static/app.js"></script>
{% block scripts %}{% endblock %}
</body></html>
```

`webapp/templates/index.html`:
```html
{% extends "base.html" %}
{% block content %}
{% if flash %}<div class="flash">{{ flash }}</div>{% endif %}
<h1>Migrations</h1>
<form method="post" action="/migrations" class="row">
  <input name="name" placeholder="e.g. acme -> globex UAT" required>
  <button>New migration</button>
</form>
<table class="data">
  <thead><tr><th>Name</th><th>Last run</th><th>Verdict</th></tr></thead>
  <tbody>
  {% for m in migrations %}
    <tr>
      <td><a href="/migrations/{{ m.id }}">{{ m.name }}</a></td>
      <td>{% if m.last_run %}<a href="/runs/{{ m.last_run.id }}">run #{{ m.last_run.id }}</a>
          · {{ m.last_run.status }}{% else %}—{% endif %}</td>
      <td>{% if m.last_run and m.last_run.verdict %}
          <span class="verdict v-{{ m.last_run.verdict }}">{{ m.last_run.verdict }}</span>
          {% endif %}</td>
    </tr>
  {% else %}<tr><td colspan="3">No migrations yet.</td></tr>{% endfor %}
  </tbody>
</table>
{% endblock %}
```

`webapp/templates/settings.html`:
```html
{% extends "base.html" %}
{% block content %}
<h1>Settings</h1>
{% if saved %}<div class="flash ok">Saved.</div>{% endif %}
<p class="lead">Atlassian OAuth app (optional — PAT connections work without it).
Callback URL to register: <code>{{ redirect_uri }}</code></p>
<form method="post" action="/settings" class="stack">
  <label>Client ID <input name="oauth_client_id" value="{{ client_id }}"></label>
  <label>Client secret
    <input name="oauth_client_secret" type="password"
           placeholder="{{ '•••••• (saved)' if has_secret else '' }}"></label>
  <button>Save</button>
</form>
{% endblock %}
```

`webapp/templates/migration.html`:
```html
{% extends "base.html" %}
{% block content %}
<h1>{{ mig.name }}</h1>
{% if error %}<div class="flash err">{{ error }}</div>{% endif %}
<div class="grid2">
{% for role in ["source", "target"] %}
  <section class="panel">
    <h3>{{ role|capitalize }}</h3>
    {% set c = conns[role] %}
    {% if c %}
      <p><code>{{ c.site_url }}</code><br>
      {{ c.auth_type|upper }} · {{ c.account_email or "" }} ·
      <span class="chip {{ 'ok' if c.status == 'verified' else '' }}">{{ c.status }}</span></p>
    {% endif %}
    {% if oauth_ready %}
      <a class="btn" href="/oauth/start?migration_id={{ mig.id }}&role={{ role }}">
        Connect with Atlassian</a>
      <p class="muted">…or enter a PAT:</p>
    {% endif %}
    <form method="post" action="/migrations/{{ mig.id }}/connections" class="stack">
      <input type="hidden" name="role" value="{{ role }}">
      <input name="site_url" placeholder="https://site.atlassian.net" required>
      <input name="email" placeholder="email" required>
      <input name="api_token" type="password" placeholder="API token" required>
      <button>Save {{ role }} (PAT)</button>
    </form>
  </section>
{% endfor %}
</div>
{% if site_pick %}
  <section class="panel">
    <h3>Pick the {{ site_pick.role }} site</h3>
    {% for s in site_pick.sites %}
    <form method="post" action="/oauth/select" class="row">
      <input type="hidden" name="state" value="{{ site_pick.state }}">
      <input type="hidden" name="cloud_id" value="{{ s.id }}">
      <input type="hidden" name="site_url" value="{{ s.url }}">
      <button>{{ s.name }} — {{ s.url }}</button>
    </form>
    {% endfor %}
  </section>
{% endif %}
<section class="panel">
  <h3>Run an audit</h3>
  {% if active %}
    <p>Run <a href="/runs/{{ active.id }}">#{{ active.id }}</a> is in progress.</p>
  {% else %}
  <form method="post" action="/migrations/{{ mig.id }}/runs" class="row">
    <input name="projects" placeholder="project keys, comma-separated (empty = all matched)">
    <button>Start audit</button>
  </form>
  {% endif %}
</section>
<section class="panel">
  <h3>Runs</h3>
  <table class="data"><thead>
    <tr><th>#</th><th>Status</th><th>Phase</th><th>Verdict</th><th></th></tr></thead>
  <tbody>
  {% for r in runs %}
    <tr><td><a href="/runs/{{ r.id }}">{{ r.id }}</a></td>
        <td>{{ r.status }}</td><td>{{ r.phase }}</td>
        <td>{% if r.verdict %}<span class="verdict v-{{ r.verdict }}">{{ r.verdict }}</span>{% endif %}</td>
        <td>{% if r.status == 'done' %}<a href="/runs/{{ r.id }}/analysis">analysis →</a>{% endif %}</td></tr>
  {% else %}<tr><td colspan="5">No runs yet.</td></tr>{% endfor %}
  </tbody></table>
</section>
{% endblock %}
```

`webapp/templates/run.html`:
```html
{% extends "base.html" %}
{% block content %}
<h1>Run #{{ run.id }} — {{ mig.name }}</h1>
<p>Status: <b id="status">{{ run.status }}</b> · Phase: <b id="phase">{{ run.phase }}</b>
 {% if run.status == 'running' %}
 <form method="post" action="/runs/{{ run.id }}/cancel" style="display:inline">
   <button class="danger">Cancel</button></form>{% endif %}
 {% if run.status == 'done' %}
   <a class="btn" href="/runs/{{ run.id }}/analysis">Open analysis →</a>{% endif %}
 <a class="btn" href="/runs/{{ run.id }}/elevate">Permissions / elevation</a>
 {% if run.status != 'running' %}
 <form method="post" action="/migrations/{{ mig.id }}/runs" style="display:inline">
   <input type="hidden" name="reuse_extracts_from" value="{{ run.id }}">
   <button>Re-run (reuse extracts)</button></form>{% endif %}</p>
<pre id="log" class="log"></pre>
{% endblock %}
{% block scripts %}
<script>
const log = document.getElementById("log");
const es = new EventSource("/runs/{{ run.id }}/stream");
es.onmessage = (e) => {
  const ev = JSON.parse(e.data);
  log.textContent += `[${ev.phase}] ${ev.message}\n`;
  log.scrollTop = log.scrollHeight;
  document.getElementById("phase").textContent = ev.phase;
};
es.addEventListener("done", () => { es.close(); location.reload(); });
</script>
{% endblock %}
```

`webapp/templates/elevate.html`:
```html
{% extends "base.html" %}
{% block content %}
<h1>Permission elevation — run #{{ run.id }}</h1>
<p class="lead">Blind-spotted projects (search sees fewer issues than exist).
Granting adds YOUR account to each project's Administrators role on that side.
It is recorded and undoable. Re-run the audit afterwards.</p>
<table class="data"><thead><tr><th>Project</th><th>src count</th><th>tgt count</th></tr></thead>
<tbody>
{% for r in rows %}<tr><td>{{ r.key }}</td><td>{{ r.src_count }}</td><td>{{ r.tgt_count }}</td></tr>
{% else %}<tr><td colspan="3">No blind spots recorded on this run.</td></tr>{% endfor %}
</tbody></table>
<div class="row">
{% for side in ["source", "target"] %}
  <form method="post" action="/runs/{{ run.id }}/elevate">
    <input type="hidden" name="side" value="{{ side }}">
    <button class="danger">Grant on {{ side }}</button>
  </form>
  <form method="post" action="/runs/{{ run.id }}/elevate/undo">
    <input type="hidden" name="side" value="{{ side }}">
    <button {% if (side == 'source' and not undo_src) or (side == 'target' and not undo_tgt) %}disabled{% endif %}>
      Undo {{ side }}</button>
  </form>
{% endfor %}
</div>
{% endblock %}
```

`webapp/templates/analysis.html`:
```html
{% extends "base.html" %}
{% block content %}
<div class="anav">
  <span class="kicker">{{ mig.name }} · run #{{ run.id }}</span>
  <nav>
    {% for v, label in [("overview","Overview"),("projects","Projects"),
                        ("config","Config parity"),("issues","All findings"),
                        ("log","Run log")] %}
      <a href="/runs/{{ run.id }}/analysis{{ '' if v == 'overview' else '/' + v }}"
         class="{{ 'active' if view == v }}">{{ label }}</a>
    {% endfor %}
  </nav>
</div>
<div id="app"
     data-run="{{ run.id }}" data-view="{{ view }}" data-project="{{ project }}"
     data-src-base="{{ src_base }}" data-tgt-base="{{ tgt_base }}"></div>
{% endblock %}
{% block scripts %}<script>Analysis.boot();</script>{% endblock %}
```

- [ ] **Step 5: Write the static assets**

`webapp/static/app.css` (broadsheet design system, condensed):
```css
:root{--bg:#FBF0E4;--paper:#FFFAF3;--panel:#fff;--ink:#241C16;--ink-soft:#6E5F52;
--ink-faint:#9A8B7C;--rule:#E7D7C5;--accent:#0F5257;--crit:#9C1A3D;--warn:#C0631B;
--ochre:#B68410;--ok:#3C7A4E;--eng:#2C4A7C}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,
"Segoe UI",sans-serif}
.wrap{max-width:1240px;margin:0 auto;padding:0 24px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
h1{font-family:Georgia,serif;font-size:30px;margin:.6em 0 .4em}
h3{font-family:Georgia,serif;margin:.2em 0 .6em}
.masthead{border-top:6px solid var(--ink);background:var(--paper);
border-bottom:1px solid var(--rule);margin-bottom:18px}
.masthead .wrap{display:flex;align-items:center;justify-content:space-between;
padding:14px 24px}
.brand{font-family:Georgia,serif;font-weight:700;font-size:20px;color:var(--ink)}
.masthead nav a{margin-left:16px;font-size:13px;text-transform:uppercase;
letter-spacing:.06em;color:var(--ink-soft)}
.panel{background:var(--panel);border:1px solid var(--rule);border-radius:3px;
padding:16px 18px;margin:12px 0}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:860px){.grid2{grid-template-columns:1fr}}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0}
.stack{display:flex;flex-direction:column;gap:8px;max-width:420px}
input,select{padding:8px 10px;border:1px solid var(--rule);border-radius:3px;
background:#fff;font:inherit}
button,.btn{display:inline-block;background:var(--accent);color:#fff;border:0;
padding:8px 14px;border-radius:3px;font:inherit;cursor:pointer}
button.danger{background:var(--crit)}
button[disabled]{background:var(--ink-faint);cursor:not-allowed}
table.data{border-collapse:collapse;width:100%;font-size:13.5px;background:#fff;
border:1px solid var(--rule)}
table.data th{text-align:left;font-size:11px;text-transform:uppercase;
letter-spacing:.05em;color:var(--ink-soft);padding:9px 10px;
border-bottom:2px solid var(--rule);background:var(--paper)}
table.data td{padding:8px 10px;border-bottom:1px solid var(--rule)}
.flash{background:#fff;border-left:4px solid var(--accent);padding:10px 14px;
margin:10px 0}.flash.err{border-color:var(--crit)}.flash.ok{border-color:var(--ok)}
.chip{display:inline-block;font-size:11.5px;font-weight:600;padding:1px 9px;
border-radius:20px;background:#eee;color:var(--ink-soft)}
.chip.ok{background:#e3efe6;color:var(--ok)}
.verdict{font-weight:700;padding:2px 10px;border-radius:3px;font-size:12px}
.v-CLEAN{background:#e3efe6;color:var(--ok)}
.v-CLEAN_WITH_TAILS{background:#f5ecd6;color:var(--ochre)}
.v-GAPS_FOUND{background:#f7e3d2;color:var(--warn)}
.v-CRITICAL{background:#f6dde4;color:var(--crit)}
.log{background:var(--ink);color:#F3E9DA;padding:14px;border-radius:3px;
min-height:200px;max-height:480px;overflow:auto;font:12.5px/1.5 monospace}
.muted{color:var(--ink-faint)}.lead{color:var(--ink-soft);max-width:72ch}
.kicker{font-size:11px;letter-spacing:.25em;text-transform:uppercase;
color:var(--crit);font-weight:700}
.anav{display:flex;justify-content:space-between;align-items:baseline;
border-bottom:1px solid var(--rule);padding-bottom:8px;margin-bottom:14px}
.anav nav a{margin-left:14px;font-size:12.5px;text-transform:uppercase;
letter-spacing:.04em;color:var(--ink-soft);padding-bottom:8px}
.anav nav a.active{color:var(--crit);border-bottom:2px solid var(--crit)}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:14px 0}
@media(max-width:900px){.kpis{grid-template-columns:repeat(2,1fr)}}
.kpi{background:#fff;border:1px solid var(--rule);border-left:4px solid var(--accent);
border-radius:3px;padding:14px}
.kpi.crit{border-left-color:var(--crit)}.kpi.warn{border-left-color:var(--warn)}
.kpi.ok{border-left-color:var(--ok)}
.kpi .num{font:600 32px/1 monospace}.kpi .lbl{font-size:12px;font-weight:600;
text-transform:uppercase;margin-top:6px}.kpi .sub{font-size:11.5px;
color:var(--ink-faint);font-family:monospace}
.finding{background:var(--paper);border:1px solid var(--rule);
border-left:4px solid var(--crit);padding:10px 14px;margin:8px 0;font-size:14px}
.bar{display:grid;grid-template-columns:160px 1fr 44px;gap:8px;align-items:center;
margin:5px 0;font-size:13px}
.bar .track{background:#f0e5d6;height:14px;border-radius:2px;overflow:hidden}
.bar .fill{height:100%;background:var(--accent)}
.bar .val{font-family:monospace;text-align:right}
.pager{display:flex;gap:8px;align-items:center;margin:10px 0}
.kbadge{font-family:monospace;font-size:11px;padding:1px 7px;border-radius:3px;
background:#eee}
.k-missing_in_tgt{background:#f6dde4;color:var(--crit)}
.k-missing_in_src{background:#f6dde4;color:var(--crit)}
.k-key_collision{background:#f6dde4;color:var(--crit)}
.k-tail_post_cutover{background:#f5ecd6;color:var(--ochre)}
.k-field_mismatch{background:#f7e3d2;color:var(--warn)}
.k-content_mismatch{background:#f7e3d2;color:var(--warn)}
.k-comment_mismatch{background:#f7e3d2;color:var(--warn)}
.k-attachment_mismatch{background:#f7e3d2;color:var(--warn)}
.k-link_mismatch{background:#eee;color:var(--ink-soft)}
```

`webapp/static/app.js` (analysis renderer, vanilla):
```javascript
const Analysis = (() => {
  let R, V, P, SRC, TGT;
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
    (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  const get = async (p) => (await fetch(p)).json();
  const issueLink = (key, base) =>
    key && base ? `<a href="${base}/browse/${key}" target="_blank">${esc(key)}</a>`
                : esc(key ?? "—");
  const kb = (k) => `<span class="kbadge k-${esc(k)}">${esc(k)}</span>`;

  function kpi(num, lbl, sub, tone = "") {
    return `<div class="kpi ${tone}"><div class="num">${num}</div>
      <div class="lbl">${lbl}</div><div class="sub">${sub || ""}</div></div>`;
  }
  function bars(pairs) {
    const mx = Math.max(...pairs.map(([, v]) => v), 1);
    return pairs.map(([k, v]) => `<div class="bar"><div>${esc(k)}</div>
      <div class="track"><div class="fill" style="width:${100 * v / mx}%"></div></div>
      <div class="val">${v}</div></div>`).join("");
  }
  function issuesTable(d) {
    const rows = d.rows.map((r) => `<tr>
      <td>${kb(r.kind)}</td><td>${issueLink(r.src_key, SRC)}</td>
      <td>${issueLink(r.tgt_key, TGT)}</td><td>${esc(r.project)}</td>
      <td>${esc(r.field ?? "")}</td><td>${esc(r.summary ?? "")}</td>
      <td class="muted">${esc(JSON.stringify(r.detail)).slice(0, 160)}</td></tr>`);
    return `<table class="data"><thead><tr><th>Kind</th><th>Source</th>
      <th>Target</th><th>Project</th><th>Field</th><th>Summary</th><th>Detail</th>
      </tr></thead><tbody>${rows.join("") ||
      '<tr><td colspan="7">No findings.</td></tr>'}</tbody></table>`;
  }
  async function issuesView(el, fixed = {}) {
    let page = 1;
    const render = async () => {
      const q = new URLSearchParams({ page, size: 50, ...fixed });
      const sel = $("#f-kind"), txt = $("#f-q");
      if (sel && sel.value) q.set("kind", sel.value);
      if (txt && txt.value) q.set("q", txt.value);
      const d = await get(`/api/runs/${R}/issues?${q}`);
      $("#tbl").innerHTML = issuesTable(d);
      $("#count").textContent =
        `${d.total} finding(s) · page ${page}/${Math.max(1, Math.ceil(d.total / 50))}`;
    };
    const kinds = await get(`/api/runs/${R}/issues/kinds` +
      (fixed.project ? `?project=${fixed.project}` : ""));
    el.innerHTML = `<div class="row">
      <select id="f-kind"><option value="">All kinds</option>
        ${Object.entries(kinds).map(([k, n]) =>
          `<option value="${k}">${k} (${n})</option>`).join("")}</select>
      <input id="f-q" placeholder="search key / summary / field">
      <span id="count" class="muted"></span>
      <span class="pager"><button id="prev">‹</button><button id="next">›</button></span>
      </div><div id="tbl"></div>`;
    $("#f-kind").onchange = () => { page = 1; render(); };
    $("#f-q").oninput = () => { page = 1; render(); };
    $("#prev").onclick = () => { page = Math.max(1, page - 1); render(); };
    $("#next").onclick = () => { page += 1; render(); };
    render();
  }

  const views = {
    async overview(el) {
      const s = await get(`/api/runs/${R}/summary`);
      const st = s.stats;
      el.innerHTML = `
        <p><span class="verdict v-${esc(s.verdict)}">${esc(s.verdict)}</span></p>
        <div class="kpis">
          ${kpi(st.issues_src_total ?? 0, "Source issues", `${st.projects} project(s)`)}
          ${kpi(st.issues_tgt_total ?? 0, "Target issues", "")}
          ${kpi(st.holes ?? 0, "Genuine holes", "missing below cutover", st.holes ? "crit" : "ok")}
          ${kpi(st.tails ?? 0, "Post-cutover tails", "expected drift", "warn")}
          ${kpi(st.issues_with_mismatches ?? 0, "Issues w/ mismatches", "", st.issues_with_mismatches ? "warn" : "ok")}
          ${kpi(st.collisions ?? 0, "Key collisions", "", st.collisions ? "crit" : "ok")}
          ${kpi((st.config_missing ?? 0) + (st.config_other ?? 0), "Config gaps", "")}
          ${kpi(st.blind_spots ?? 0, "Blind spots", "permissions", st.blind_spots ? "crit" : "ok")}
        </div>
        ${s.headlines.map((h) => `<div class="finding">${esc(h)}</div>`).join("")}
        <div class="panel"><h3>Fidelity by project</h3>
        ${bars(Object.entries(s.project_stats).map(([k, v]) =>
          [k + " (fidelity %)", Math.round(v.fidelity_pct ?? 100)]))}</div>`;
    },
    async projects(el) {
      const rows = await get(`/api/runs/${R}/projects`);
      el.innerHTML = `<table class="data"><thead><tr><th>Project</th>
        <th>Src</th><th>Tgt</th><th>Holes</th><th>Tails</th><th>Fidelity</th>
        <th>Blind spot</th></tr></thead><tbody>` + rows.map((r) => `<tr>
        <td><a href="/runs/${R}/analysis/projects/${esc(r.key)}">${esc(r.key)}</a>
            ${esc(r.name ?? "")}</td>
        <td>${r.src_count ?? "?"}</td><td>${r.tgt_count ?? "?"}</td>
        <td>${r.missing ?? "—"}</td><td>${r.tail_count ?? "—"}</td>
        <td>${r.fidelity_pct != null ? r.fidelity_pct + "%" : "—"}</td>
        <td>${r.blind_spot ? "⚠ YES" : ""}</td></tr>`).join("") +
        "</tbody></table>";
    },
    async project(el) { await issuesView(el, { project: P }); },
    async issues(el) { await issuesView(el); },
    async config(el) {
      const { areas } = await get(`/api/runs/${R}/config`);
      const s = await get(`/api/runs/${R}/summary`);
      const blocks = [];
      for (const a of areas) {
        const { rows } = await get(`/api/runs/${R}/config?area=${a}`);
        const meta = (s.areas || {})[a];
        blocks.push(`<div class="panel"><h3>${esc(a)}
          ${meta ? `<span class="muted">src ${meta.src ?? "?"} · tgt ${meta.tgt ?? "?"}</span>` : ""}</h3>
          <table class="data"><thead><tr><th>Object</th><th>Kind</th><th>Detail</th>
          </tr></thead><tbody>${rows.map((r) => `<tr><td>${esc(r.name)}</td>
          <td>${kb(r.kind)}</td><td class="muted">${esc(JSON.stringify(r.detail))
          .slice(0, 200)}</td></tr>`).join("")}</tbody></table></div>`);
      }
      el.innerHTML = blocks.join("") ||
        '<div class="finding">No config gaps found.</div>';
    },
    async log(el) {
      const evs = await get(`/api/runs/${R}/events`);
      el.innerHTML = `<pre class="log">${evs.map((e) =>
        `[${esc(e.phase)}] ${esc(e.message)}`).join("\n")}</pre>`;
    },
  };

  return {
    boot() {
      const el = document.getElementById("app");
      R = el.dataset.run; V = el.dataset.view || "overview";
      P = el.dataset.project; SRC = el.dataset.srcBase; TGT = el.dataset.tgtBase;
      (views[V] || views.overview)(el);
    },
  };
})();
```

- [ ] **Step 6: Run the tests**

Run: `python3 -m pytest tests/test_main.py -q`
Expected: `10 passed`.

- [ ] **Step 7: Run the FULL suite**

Run: `python3 -m pytest -q`
Expected: all tests pass, 0 failures.

- [ ] **Step 8: Commit**

```bash
git add webapp/main.py webapp/templates webapp/static tests/test_main.py
git commit -m "feat: web app — wizard, OAuth flow, runs with SSE, elevation, multi-page analysis UI"
```

---

### Task 15: Dockerfile + packaging check

**Files:**
- Create: `Dockerfile`, `.dockerignore`

- [ ] **Step 1: Write the files**

`Dockerfile`:
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY auditor ./auditor
COPY webapp ./webapp
RUN pip install --no-cache-dir .
ENV MA_DATA_DIR=/data MA_BIND=0.0.0.0:8484
VOLUME /data
EXPOSE 8484
CMD ["migration-auditor", "serve"]
```

`.dockerignore`:
```
data/
__pycache__/
*.pyc
.git/
tests/
docs/
```

- [ ] **Step 2: Verify the console entry point works**

```bash
python3 -c "from webapp.main import cli; print('entry ok')"
python3 -m pytest -q
```
Expected: `entry ok`, full suite passes.

- [ ] **Step 3: Commit**

```bash
git add Dockerfile .dockerignore
git commit -m "chore: Dockerfile (hosting-ready container) + dockerignore"
```

---

### Task 16: Demo seed + end-to-end verification

**Files:**
- Create: `scripts/seed_demo.py`

A synthetic-data seeder so the UI can be demoed/verified without real Jira credentials (placeholder companies only, per the synthetic-data rule).

- [ ] **Step 1: Write the seeder**

`scripts/seed_demo.py`:
```python
"""Seed a synthetic finished run so the analysis UI can be inspected without
real credentials. Usage: python3 scripts/seed_demo.py  (uses MA_DATA_DIR)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from webapp.config import load_config
from webapp.store import Store

cfg = load_config()
os.makedirs(cfg.data_dir, exist_ok=True)
store = Store(db_path=cfg.db_path, key_path=cfg.key_path,
              secret_key=cfg.secret_key)
mid = store.create_migration("Acme DC -> Globex Cloud (demo)")
store.save_connection(mid, "source", "pat", "https://acme.atlassian.net",
                      secret={"email": "demo@acme.test", "token": "demo"},
                      account_email="demo@acme.test")
store.save_connection(mid, "target", "pat", "https://globex.atlassian.net",
                      secret={"email": "demo@globex.test", "token": "demo"},
                      account_email="demo@globex.test")
rid = store.create_run(mid, {"projects": ["SUP", "ENG"]})
store.set_run_projects(rid, [
    {"key": "SUP", "name": "Support", "src_count": 16022, "tgt_count": 15105,
     "missing": 0, "tail_count": 917, "fidelity_pct": 99.4, "blind_spot": 0,
     "status": "compared"},
    {"key": "ENG", "name": "Engineering", "src_count": 40092, "tgt_count": 39997,
     "missing": 3, "tail_count": 92, "fidelity_pct": 97.1, "blind_spot": 0,
     "status": "compared"}])
issues = []
for i in range(3):
    issues.append({"project": "ENG", "kind": "missing_in_tgt",
                   "src_key": f"ENG-{100 + i}", "tgt_key": None, "field": None,
                   "summary": f"Lost issue example {i}",
                   "detail": {"below_cutover_line": True}})
for i in range(40):
    issues.append({"project": "SUP", "kind": "tail_post_cutover",
                   "src_key": f"SUP-{16000 + i}", "tgt_key": None, "field": None,
                   "summary": f"Post-cutover ticket {i}",
                   "detail": {"direction": "source"}})
for i in range(25):
    issues.append({"project": "ENG", "kind": "field_mismatch",
                   "src_key": f"ENG-{200 + i}", "tgt_key": f"ENG-{200 + i}",
                   "field": "status", "summary": "status differs",
                   "detail": {"src": "On Hold", "tgt": "To Do", "sev": "high"}})
store.insert_findings_issue(rid, issues)
store.insert_findings_config(rid, [
    {"area": "statuses", "name": n, "kind": "missing_in_tgt", "detail": {}}
    for n in ("On Hold", "Waiting", "RCA")] + [
    {"area": "custom_fields", "name": "Squad", "kind": "type_mismatch",
     "detail": {"src_type": "select", "tgt_type": "textfield"}}])
for msg in ("source: authenticated", "2 projects in scope",
            "ENG: common=39994 holes=3 tails=92", "run complete"):
    store.add_event(rid, "demo", "info", msg)
store.update_run(rid, status="done", verdict="CRITICAL", stats={
    "projects": 2, "issues_src_total": 56114, "issues_tgt_total": 55102,
    "holes": 3, "tails": 1009, "collisions": 0, "issues_with_mismatches": 25,
    "config_missing": 3, "config_other": 1, "blind_spots": 0,
    "headlines": [
        "ENG has 3 issues missing in the target below the cutover line. "
        "This is genuine data loss until proven otherwise.",
        "1,009 issue(s) exist only as post-cutover tail. Expected drift."],
    "project_stats": {"SUP": {"src": 16022, "tgt": 15105, "fidelity_pct": 99.4},
                       "ENG": {"src": 40092, "tgt": 39997, "fidelity_pct": 97.1}},
    "areas": {"statuses": {"src": 74, "tgt": 42}}})
print(f"Seeded migration {mid}, run {rid}. Start the app and open "
      f"http://localhost:8484/runs/{rid}/analysis")
```

- [ ] **Step 2: Run the seeder + full suite**

```bash
python3 scripts/seed_demo.py
python3 -m pytest -q
```
Expected: seeder prints the analysis URL; full suite passes.

- [ ] **Step 3: Manual smoke (visual verification — REQUIRED)**

```bash
migration-auditor serve &
sleep 2
```
Then verify with Playwright (python) or a browser: open `http://localhost:8484`,
the seeded migration page, the run page, and every analysis view
(`/analysis`, `/analysis/projects`, `/analysis/projects/ENG`, `/analysis/config`,
`/analysis/issues`, `/analysis/log`). Confirm: verdict banner renders CRITICAL,
KPI cards populated, findings tables paginate/filter, issue keys link to
`https://acme.atlassian.net/browse/...`. Kill the server afterwards.

- [ ] **Step 4: Commit**

```bash
git add scripts/seed_demo.py
git commit -m "chore: synthetic demo seeder for UI verification"
```

---

## Final acceptance checklist

- [ ] `python3 -m pytest -q` — entire suite green
- [ ] `migration-auditor serve` boots; dashboard loads
- [ ] Seeded analysis pages all render with data
- [ ] No real customer names anywhere in code/fixtures (synthetic only)
- [ ] `data/` is gitignored; no tokens in any committed file
