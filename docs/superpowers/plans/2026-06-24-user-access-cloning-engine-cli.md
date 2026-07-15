# User Access Cloning — Phase 1 (Engine + Client + CLI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a usable `migration-auditor clone-access` CLI that additively clones a Jira Cloud user's group memberships (and, with the role scan, direct project-role memberships) onto another account — single pair or `main,clone` CSV, with preview / dry-run / apply modes.

**Architecture:** A pure engine module `auditor/cloneaccess.py` (resolve → gather → plan → apply) built on new read/write methods added to the existing `JiraClient`. A CLI subcommand wires it to a saved connection. The web UI is a separate Phase-2 plan on top of this engine. Spec: `docs/superpowers/specs/2026-06-24-user-access-cloning-design.md`.

**Tech Stack:** Python ≥3.11, `httpx` (sync `JiraClient`), `pytest` + `httpx.MockTransport`, `argparse` CLI. No new dependencies.

## Global Constraints

- **Additive only.** Never remove a user from any group or role. The clone keeps its existing access; the tool only adds what the main has and the clone lacks.
- **Idempotent.** Every write is preceded by a membership check (diff against the clone's current state). An "already a member" API response is recorded as `already`, never a failure. Re-running is safe.
- **Identity:** each value is a Jira **accountId** (used directly) or an **email** (resolved via `GET /rest/api/3/user/search?query=<email>`, matching one active `atlassian`-type account). Zero matches → `blocked` (unresolved); >1 → `blocked` (ambiguous). Never guess. `main` must differ from `clone` (else `noop`).
- **Phasing:** groups are planned/applied without the project scan; **direct project-role** cloning requires the instance role-actor scan (`scan_roles=True`).
- **Modes:** `preview` = `dry_run=True, scan_roles=False` (groups only, no writes). `full dry-run` = `dry_run=True, scan_roles=True` (everything, no writes). `apply` = `dry_run=False, scan_roles=True`.
- **Concurrency:** the role-actor scan reuses `auditor.envaudit._pool.map_results` (shared `MA_GATHER_WORKERS`).
- **Circuit breaker:** a run aborts (raising `CloneAborted`) after `MA_BREAKER_THRESHOLD` (default 5; `0` disables) consecutive server-side failures (HTTP 5xx / 429 / transport `< 0`) during writes. Partial results so far are returned/persisted.
- **Jira Cloud only** (the existing Cloud `JiraClient`). No account provisioning. No removals. No permission-scheme direct-user-grant or personal-share cloning (reported as "not cloned").
- **No new dependencies; Python ≥3.11.** Run `pytest -q` green before each task's final commit.
- Repo root: `/mnt/d/Atlassian-Products/Migration-auditor`. Branch: `feat/user-access-cloning`.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `auditor/client.py` | Jira HTTP client | Add 6 read/write methods for groups, project roles, user search |
| `auditor/cloneaccess.py` | Clone engine (resolve→gather→plan→apply→orchestrate) | Create |
| `webapp/main.py` | CLI entrypoint | Add `clone-access` subcommand to `cli()` |
| `tests/test_client.py` | Client tests | Add tests for the 6 new methods |
| `tests/test_cloneaccess.py` | Engine tests | Create |
| `tests/test_clone_cli.py` | CLI tests | Create |

The engine returns a structured **report** consumed by the CLI (and Phase-2 UI):

```python
# report
{
  "dry_run": bool, "scanned_roles": bool,
  "pairs": [
    {"main": str, "clone": str, "main_id": str|None, "clone_id": str|None,
     "status": "ok"|"blocked"|"noop", "reason": str|None,
     "groups": {"added": [str], "already": [str], "failed": [{"group": str, "error": str}]},
     "roles":  {"added": [str], "already": [str], "failed": [{"role": str, "error": str}], "scanned": bool}},
    ...],
  "summary": {"pairs": int, "blocked": int, "groups_added": int, "roles_added": int, "failed": int},
}
```
Role identifiers in `added`/`already`/`failed` are rendered `"PROJECT/RoleName"`.

---

### Task 1: JiraClient — group & project-role read/write methods

**Files:**
- Modify: `auditor/client.py` (add methods to `class JiraClient`, near the other `create_*`/write helpers ~line 455+)
- Test: `tests/test_client.py`

**Interfaces:**
- Produces (all on `JiraClient`):
  - `search_users(query: str, max_results: int = 10) -> tuple[list, str | None]`
  - `user_groups(account_id: str) -> tuple[list, str | None]`  (list of `{"name","groupId"}`)
  - `add_user_to_group(group_id: str, account_id: str) -> tuple[int, dict | list]`
  - `project_role_map(project_key: str) -> tuple[dict, str | None]`  (`{roleName: roleId}`)
  - `project_role_actors(project_key: str, role_id: str) -> tuple[list, str | None]`  (list of actor dicts)
  - `add_user_to_project_role(project_key: str, role_id: str, account_id: str) -> tuple[int, dict | list]`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_client.py` (it already imports `Connection, JiraClient` and defines `mk_pat(handler)`):

```python
def test_user_groups_and_search():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/user/groups"):
            assert req.url.params.get("accountId") == "acc-1"
            return httpx.Response(200, json=[{"name": "g1", "groupId": "gid-1"},
                                             {"name": "g2", "groupId": "gid-2"}])
        if p.endswith("/user/search"):
            assert req.url.params.get("query") == "x@y.z"
            return httpx.Response(200, json=[{"accountId": "acc-9",
                                              "accountType": "atlassian",
                                              "emailAddress": "x@y.z", "active": True}])
        return httpx.Response(404)
    c = mk_pat(handler)
    groups, err = c.user_groups("acc-1")
    assert err is None and [g["groupId"] for g in groups] == ["gid-1", "gid-2"]
    users, err = c.search_users("x@y.z")
    assert err is None and users[0]["accountId"] == "acc-9"


def test_add_user_to_group_posts_accountid():
    seen = {}
    def handler(req):
        seen["path"] = str(req.url.path)
        seen["gid"] = req.url.params.get("groupId")
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={})
    c = mk_pat(handler)
    st, _ = c.add_user_to_group("gid-1", "acc-1")
    assert st == 201
    assert seen["path"].endswith("/group/user")
    assert seen["gid"] == "gid-1"
    assert seen["body"] == {"accountId": "acc-1"}


def test_project_role_map_and_actors():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/project/AC/role"):
            return httpx.Response(200, json={
                "Administrators": "https://x.atlassian.net/rest/api/3/project/AC/role/10002",
                "Members": "https://x.atlassian.net/rest/api/3/project/AC/role/10001"})
        if p.endswith("/project/AC/role/10002"):
            return httpx.Response(200, json={"actors": [
                {"type": "atlassian-user-role-actor", "actorUser": {"accountId": "acc-1"}},
                {"type": "atlassian-group-role-actor", "actorGroup": {"name": "g1"}}]})
        return httpx.Response(404)
    c = mk_pat(handler)
    rmap, err = c.project_role_map("AC")
    assert err is None and rmap == {"Administrators": "10002", "Members": "10001"}
    actors, err = c.project_role_actors("AC", "10002")
    assert err is None and len(actors) == 2


def test_add_user_to_project_role_posts_user_array():
    seen = {}
    def handler(req):
        seen["path"] = str(req.url.path); seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={})
    c = mk_pat(handler)
    st, _ = c.add_user_to_project_role("AC", "10002", "acc-1")
    assert st == 200
    assert seen["path"].endswith("/project/AC/role/10002")
    assert seen["body"] == {"user": ["acc-1"]}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_client.py -k "user_groups or add_user_to_group or project_role or project_role_actors" -v`
Expected: FAIL — methods not defined (AttributeError).

- [ ] **Step 3: Implement the methods**

In `auditor/client.py`, inside `class JiraClient` (next to the other write helpers), add:

```python
    # ----------------------------------------------------- access (groups/roles)
    def search_users(self, query: str, max_results: int = 10):
        st, d = self.req("/rest/api/3/user/search",
                         params={"query": query, "maxResults": max_results})
        if st != 200 or not isinstance(d, list):
            return [], (d.get("_error") if isinstance(d, dict) else f"status {st}")
        return d, None

    def user_groups(self, account_id: str):
        st, d = self.req("/rest/api/3/user/groups",
                         params={"accountId": account_id})
        if st != 200 or not isinstance(d, list):
            return [], (d.get("_error") if isinstance(d, dict) else f"status {st}")
        return d, None

    def add_user_to_group(self, group_id: str, account_id: str):
        return self.req("/rest/api/3/group/user", "POST",
                        {"accountId": account_id}, params={"groupId": group_id})

    def project_role_map(self, project_key: str):
        st, d = self.req(f"/rest/api/3/project/{project_key}/role")
        if st != 200 or not isinstance(d, dict):
            return {}, (d.get("_error") if isinstance(d, dict) else f"status {st}")
        # value is the role URL; the id is its last path segment.
        return {name: url.rstrip("/").rsplit("/", 1)[-1]
                for name, url in d.items()}, None

    def project_role_actors(self, project_key: str, role_id: str):
        st, d = self.req(f"/rest/api/3/project/{project_key}/role/{role_id}")
        if st != 200 or not isinstance(d, dict):
            return [], (d.get("_error") if isinstance(d, dict) else f"status {st}")
        return d.get("actors", []), None

    def add_user_to_project_role(self, project_key: str, role_id: str,
                                 account_id: str):
        return self.req(f"/rest/api/3/project/{project_key}/role/{role_id}",
                        "POST", {"user": [account_id]})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_client.py -v`
Expected: PASS (new tests + all existing client tests).

- [ ] **Step 5: Commit**

```bash
cd /mnt/d/Atlassian-Products/Migration-auditor
git add auditor/client.py tests/test_client.py
git commit -m "feat(client): group-membership & project-role read/write methods"
```

---

### Task 2: Engine — identity resolution

**Files:**
- Create: `auditor/cloneaccess.py`
- Test: `tests/test_cloneaccess.py`

**Interfaces:**
- Consumes: `JiraClient.search_users` (Task 1).
- Produces:
  - `ACCOUNT_ID_RE` (module regex) and `looks_like_account_id(value: str) -> bool`
  - `resolve_identity(client, value: str) -> dict` → `{"input": str, "account_id": str|None, "status": "resolved"|"unresolved"|"ambiguous", "reason": str|None}`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cloneaccess.py`:

```python
import httpx
from auditor.client import Connection, JiraClient
from auditor import cloneaccess as ca


def mk(handler):
    conn = Connection(auth_type="pat", site_url="https://x.atlassian.net",
                      email="e", api_token="t")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def test_looks_like_account_id():
    assert ca.looks_like_account_id("557058:1f2e3d4c-5b6a-7980-1234-567890abcdef")
    assert ca.looks_like_account_id("5b39d0d03aa72d2ded7dddd4")     # 24-hex legacy
    assert not ca.looks_like_account_id("someone@example.com")
    assert not ca.looks_like_account_id("Jane Doe")


def test_resolve_accountid_passthrough_no_http():
    # An accountId resolves without any HTTP call.
    def handler(req):
        raise AssertionError("should not call the API for an accountId")
    out = ca.resolve_identity(mk(handler), "5b39d0d03aa72d2ded7dddd4")
    assert out["status"] == "resolved"
    assert out["account_id"] == "5b39d0d03aa72d2ded7dddd4"


def test_resolve_email_hit():
    def handler(req):
        return httpx.Response(200, json=[{"accountId": "acc-9", "accountType":
            "atlassian", "active": True, "emailAddress": "a@b.c"}])
    out = ca.resolve_identity(mk(handler), "a@b.c")
    assert out["status"] == "resolved" and out["account_id"] == "acc-9"


def test_resolve_email_unresolved_and_ambiguous():
    def none_h(req): return httpx.Response(200, json=[])
    out = ca.resolve_identity(mk(none_h), "ghost@b.c")
    assert out["status"] == "unresolved" and out["account_id"] is None

    def two_h(req):
        return httpx.Response(200, json=[
            {"accountId": "a1", "accountType": "atlassian", "active": True},
            {"accountId": "a2", "accountType": "atlassian", "active": True}])
    out = ca.resolve_identity(mk(two_h), "dup@b.c")
    assert out["status"] == "ambiguous" and out["account_id"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cloneaccess.py -v`
Expected: FAIL — `auditor.cloneaccess` does not exist (ImportError).

- [ ] **Step 3: Implement resolution**

Create `auditor/cloneaccess.py`:

```python
"""User-access cloning engine: resolve → gather → plan → apply.

Additive only. Clones a Jira Cloud user's group memberships and direct
project-role memberships onto another account. UI/CLI agnostic — callers pass
a JiraClient and receive plain dict reports.
"""
from __future__ import annotations

import os
import re

# accountId shapes: Cloud "<digits>:<uuid>" and legacy 24-hex.
ACCOUNT_ID_RE = re.compile(r"^(\d+:[0-9a-fA-F-]{8,}|[0-9a-fA-F]{24})$")


def looks_like_account_id(value: str) -> bool:
    return bool(ACCOUNT_ID_RE.match((value or "").strip()))


def resolve_identity(client, value: str) -> dict:
    """Resolve a raw input (accountId or email) to an accountId.

    accountId -> passthrough (no HTTP). email -> user-search for exactly one
    active atlassian account. Returns status resolved | unresolved | ambiguous;
    never guesses.
    """
    v = (value or "").strip()
    out = {"input": v, "account_id": None, "status": "unresolved", "reason": None}
    if not v:
        out["reason"] = "empty value"
        return out
    if looks_like_account_id(v):
        out.update(account_id=v, status="resolved")
        return out
    users, err = client.search_users(v)
    if err:
        out["reason"] = f"lookup failed: {err}"
        return out
    hits = [u for u in users
            if u.get("accountType") == "atlassian" and u.get("active")]
    # Prefer an exact email match when the API exposes emails.
    exact = [u for u in hits
             if (u.get("emailAddress") or "").lower() == v.lower()]
    pool = exact or hits
    if len(pool) == 1:
        out.update(account_id=pool[0]["accountId"], status="resolved")
    elif len(pool) > 1:
        out["status"] = "ambiguous"
        out["reason"] = f"{len(pool)} accounts match '{v}' — use an accountId"
    else:
        out["reason"] = f"no active account matches '{v}'"
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cloneaccess.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /mnt/d/Atlassian-Products/Migration-auditor
git add auditor/cloneaccess.py tests/test_cloneaccess.py
git commit -m "feat(cloneaccess): identity resolution (accountId or email)"
```

---

### Task 3: Engine — role-actor index (concurrent instance scan)

**Files:**
- Modify: `auditor/cloneaccess.py`
- Test: `tests/test_cloneaccess.py`

**Interfaces:**
- Consumes: `JiraClient.all_projects`, `project_role_map`, `project_role_actors` (Task 1); `auditor.envaudit._pool.map_results`.
- Produces:
  - `build_role_index(client, progress=None) -> dict` → `{account_id: [{"project": str, "role_id": str, "role": str}]}` — only `atlassian-user-role-actor` actors (direct user grants).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cloneaccess.py`:

```python
def _role_scan_handler():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/project/search"):
            return httpx.Response(200, json={"values": [
                {"key": "AC"}, {"key": "MS"}], "isLast": True, "total": 2})
        if p.endswith("/project/AC/role"):
            return httpx.Response(200, json={
                "Administrators": "https://x.atlassian.net/rest/api/3/project/AC/role/10002"})
        if p.endswith("/project/MS/role"):
            return httpx.Response(200, json={
                "Members": "https://x.atlassian.net/rest/api/3/project/MS/role/10001"})
        if p.endswith("/project/AC/role/10002"):
            return httpx.Response(200, json={"actors": [
                {"type": "atlassian-user-role-actor", "actorUser": {"accountId": "main-1"}},
                {"type": "atlassian-group-role-actor", "actorGroup": {"name": "g1"}}]})
        if p.endswith("/project/MS/role/10001"):
            return httpx.Response(200, json={"actors": [
                {"type": "atlassian-user-role-actor", "actorUser": {"accountId": "main-1"}},
                {"type": "atlassian-user-role-actor", "actorUser": {"accountId": "clone-1"}}]})
        return httpx.Response(404)
    return handler


def test_build_role_index_user_actors_only(monkeypatch):
    monkeypatch.setenv("MA_GATHER_WORKERS", "1")
    idx = ca.build_role_index(mk(_role_scan_handler()))
    # main-1 is a direct user actor in AC/Administrators and MS/Members
    roles = sorted((r["project"], r["role"]) for r in idx["main-1"])
    assert roles == [("AC", "Administrators"), ("MS", "Members")]
    # group actor (g1) is NOT indexed; clone-1 only in MS/Members
    assert [(r["project"], r["role"]) for r in idx["clone-1"]] == [("MS", "Members")]


def test_build_role_index_seq_vs_parallel_identical(monkeypatch):
    monkeypatch.setenv("MA_GATHER_WORKERS", "1")
    seq = ca.build_role_index(mk(_role_scan_handler()))
    monkeypatch.setenv("MA_GATHER_WORKERS", "8")
    par = ca.build_role_index(mk(_role_scan_handler()))
    assert seq == par
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cloneaccess.py -k build_role_index -v`
Expected: FAIL — `build_role_index` not defined.

- [ ] **Step 3: Implement the scan**

Add to `auditor/cloneaccess.py` (add the import at top with the others):

```python
from auditor.envaudit._pool import map_results
```

```python
def build_role_index(client, progress=None) -> dict:
    """Scan every project's role actors once and index direct USER actors:
    {account_id: [{"project","role_id","role"}]}. Group actors are excluded
    (group membership is cloned separately and already grants those roles).
    Concurrent (MA_GATHER_WORKERS); the merge is deterministic (main thread,
    sorted), so output is identical regardless of worker count.
    """
    projects, err = client.all_projects()
    if err:
        raise CloneError(f"could not list projects: {err}")
    keys = [p.get("key") for p in projects if p.get("key")]
    if progress:
        progress(f"scanning {len(keys)} projects for direct role grants")

    def _project_actor_rows(key):
        rmap, e = client.project_role_map(key)
        if e:
            return []
        rows = []
        for role_name, role_id in rmap.items():
            actors, e2 = client.project_role_actors(key, role_id)
            if e2:
                continue
            for a in actors:
                if a.get("type") == "atlassian-user-role-actor":
                    aid = (a.get("actorUser") or {}).get("accountId")
                    if aid:
                        rows.append((aid, {"project": key, "role_id": role_id,
                                           "role": role_name}))
        return rows

    results = map_results(keys, _project_actor_rows)
    index: dict = {}
    # Merge in project order, then sort each list, for determinism.
    for res in results:
        if isinstance(res, Exception):
            continue
        for aid, row in res:
            index.setdefault(aid, []).append(row)
    for aid in index:
        index[aid].sort(key=lambda r: (r["project"], r["role"]))
    return index
```

And add the exception class near the top of the module (after the imports):

```python
class CloneError(Exception):
    """A fatal clone-run error (e.g. cannot enumerate projects)."""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cloneaccess.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /mnt/d/Atlassian-Products/Migration-auditor
git add auditor/cloneaccess.py tests/test_cloneaccess.py
git commit -m "feat(cloneaccess): concurrent project role-actor index (direct user grants)"
```

---

### Task 4: Engine — plan a pair (additive diff)

**Files:**
- Modify: `auditor/cloneaccess.py`
- Test: `tests/test_cloneaccess.py`

**Interfaces:**
- Consumes: `JiraClient.user_groups` (Task 1); `build_role_index` output (Task 3).
- Produces:
  - `plan_pair(client, main_id, clone_id, role_index=None) -> dict` → `{"groups_add": [{"name","groupId"}], "groups_already": [str], "roles_add": [{"project","role_id","role"}], "roles_already": [str], "roles_scanned": bool}`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cloneaccess.py`:

```python
def test_plan_pair_groups_additive_diff():
    def handler(req):
        aid = req.url.params.get("accountId")
        if aid == "main-1":
            return httpx.Response(200, json=[{"name": "g1", "groupId": "gid1"},
                                             {"name": "g2", "groupId": "gid2"}])
        if aid == "clone-1":
            return httpx.Response(200, json=[{"name": "g2", "groupId": "gid2"}])
        return httpx.Response(404)
    role_index = {"main-1": [{"project": "AC", "role_id": "10002", "role": "Administrators"},
                             {"project": "MS", "role_id": "10001", "role": "Members"}],
                  "clone-1": [{"project": "MS", "role_id": "10001", "role": "Members"}]}
    plan = ca.plan_pair(mk(handler), "main-1", "clone-1", role_index)
    assert [g["groupId"] for g in plan["groups_add"]] == ["gid1"]   # g2 already
    assert plan["groups_already"] == ["g2"]
    assert [r["role"] for r in plan["roles_add"]] == ["Administrators"]  # MS already
    assert plan["roles_already"] == ["MS/Members"]
    assert plan["roles_scanned"] is True


def test_plan_pair_without_role_index_skips_roles():
    def handler(req):
        return httpx.Response(200, json=[])    # both users have no groups
    plan = ca.plan_pair(mk(handler), "main-1", "clone-1", role_index=None)
    assert plan["groups_add"] == [] and plan["roles_add"] == []
    assert plan["roles_scanned"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cloneaccess.py -k plan_pair -v`
Expected: FAIL — `plan_pair` not defined.

- [ ] **Step 3: Implement plan_pair**

Add to `auditor/cloneaccess.py`:

```python
def _role_key(r: dict) -> str:
    return f"{r['project']}/{r['role']}"


def plan_pair(client, main_id: str, clone_id: str, role_index=None) -> dict:
    """Additive diff for one pair: what groups/roles the main has that the
    clone lacks. role_index None => roles not scanned (groups-only preview).
    """
    main_groups, _ = client.user_groups(main_id)
    clone_groups, _ = client.user_groups(clone_id)
    clone_gids = {g.get("groupId") for g in clone_groups}
    groups_add, groups_already = [], []
    for g in main_groups:
        (groups_already.append(g["name"]) if g.get("groupId") in clone_gids
         else groups_add.append({"name": g["name"], "groupId": g["groupId"]}))

    roles_add, roles_already = [], []
    scanned = role_index is not None
    if scanned:
        clone_roles = {_role_key(r) for r in role_index.get(clone_id, [])}
        for r in role_index.get(main_id, []):
            (roles_already.append(_role_key(r)) if _role_key(r) in clone_roles
             else roles_add.append(r))
    return {"groups_add": groups_add, "groups_already": sorted(groups_already),
            "roles_add": roles_add, "roles_already": sorted(roles_already),
            "roles_scanned": scanned}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cloneaccess.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /mnt/d/Atlassian-Products/Migration-auditor
git add auditor/cloneaccess.py tests/test_cloneaccess.py
git commit -m "feat(cloneaccess): additive plan_pair (group + role diff)"
```

---

### Task 5: Engine — apply + run orchestrator (dry-run, idempotent, breaker)

**Files:**
- Modify: `auditor/cloneaccess.py`
- Test: `tests/test_cloneaccess.py`

**Interfaces:**
- Consumes: `JiraClient.add_user_to_group`, `add_user_to_project_role` (Task 1); `resolve_identity` (T2), `build_role_index` (T3), `plan_pair` (T4).
- Produces:
  - `class CloneAborted(CloneError)`
  - `breaker_threshold() -> int` (reads `MA_BREAKER_THRESHOLD`, default 5, `0` disables)
  - `run_clone(client, pairs, *, dry_run: bool, scan_roles: bool, progress=None) -> dict` → the **report** (see File Structure).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cloneaccess.py`:

```python
def _apply_handler(writes):
    """Records POST writes; serves groups so main has g1 (clone lacks it)."""
    def handler(req):
        p = str(req.url.path); m = req.method
        if p.endswith("/user/search"):
            # emails resolve to <local>-id
            q = req.url.params.get("query")
            return httpx.Response(200, json=[{"accountId": q.split("@")[0] + "-id",
                "accountType": "atlassian", "active": True, "emailAddress": q}])
        if p.endswith("/user/groups"):
            aid = req.url.params.get("accountId")
            return httpx.Response(200, json=[{"name": "g1", "groupId": "gid1"}]
                                  if aid == "main-id" else [])
        if p.endswith("/project/search"):
            return httpx.Response(200, json={"values": [], "isLast": True, "total": 0})
        if m == "POST" and p.endswith("/group/user"):
            writes.append(("group", req.url.params.get("groupId"),
                           json.loads(req.content)["accountId"]))
            return httpx.Response(201, json={})
        return httpx.Response(200, json={})
    import json
    return handler


def test_run_clone_dry_run_writes_nothing():
    import json
    writes = []
    rep = ca.run_clone(mk(_apply_handler(writes)),
                       [("main@x.y", "clone@x.y")],
                       dry_run=True, scan_roles=False)
    assert writes == []
    pair = rep["pairs"][0]
    assert pair["status"] == "ok"
    assert [g for g in pair["groups"]["added"]] == ["g1"]   # planned, not written
    assert rep["summary"]["groups_added"] == 1


def test_run_clone_apply_writes_missing_group_idempotently():
    writes = []
    rep = ca.run_clone(mk(_apply_handler(writes)),
                       [("main@x.y", "clone@x.y")],
                       dry_run=False, scan_roles=False)
    assert writes == [("group", "gid1", "clone-id")]
    assert rep["pairs"][0]["groups"]["added"] == ["g1"]
    assert rep["summary"]["failed"] == 0


def test_run_clone_blocks_unresolved_and_noops_self():
    def handler(req):
        if str(req.url.path).endswith("/user/search"):
            return httpx.Response(200, json=[])       # nothing resolves
        return httpx.Response(200, json=[])
    rep = ca.run_clone(mk(handler), [("ghost@x.y", "who@x.y")],
                       dry_run=True, scan_roles=False)
    assert rep["pairs"][0]["status"] == "blocked"
    assert rep["summary"]["blocked"] == 1

    # self-clone (same accountId both sides) -> noop, no writes
    rep2 = ca.run_clone(mk(handler), [("5b39d0d03aa72d2ded7dddd4",
                                       "5b39d0d03aa72d2ded7dddd4")],
                        dry_run=False, scan_roles=False)
    assert rep2["pairs"][0]["status"] == "noop"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cloneaccess.py -k run_clone -v`
Expected: FAIL — `run_clone` not defined.

- [ ] **Step 3: Implement apply + orchestrator**

Add to `auditor/cloneaccess.py`:

```python
class CloneAborted(CloneError):
    """Circuit breaker tripped: too many server-side write failures."""


def breaker_threshold() -> int:
    raw = os.environ.get("MA_BREAKER_THRESHOLD")
    if raw is None:
        return 5
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 5
    return n if n >= 0 else 5


def _server_side(status: int) -> bool:
    return status < 0 or status == 429 or 500 <= status < 600


def run_clone(client, pairs, *, dry_run: bool, scan_roles: bool,
              progress=None) -> dict:
    """Resolve, plan, and (unless dry_run) apply each pair additively.

    Groups are always planned. Roles are planned+applied only when scan_roles
    (a one-time instance scan precedes the per-pair work). Idempotent; a
    breaker aborts after MA_BREAKER_THRESHOLD consecutive server-side write
    failures, returning the partial report via CloneAborted.partial.
    """
    role_index = None
    if scan_roles:
        role_index = build_role_index(client, progress)

    thr = breaker_threshold()
    consec = 0
    report = {"dry_run": dry_run, "scanned_roles": scan_roles, "pairs": [],
              "summary": {"pairs": 0, "blocked": 0, "groups_added": 0,
                          "roles_added": 0, "failed": 0}}

    def _do_write(call):
        nonlocal consec
        st, d = call()
        ok = 200 <= st < 300
        if ok:
            consec = 0
        elif _server_side(st):
            consec += 1
            if thr and consec >= thr:
                raise CloneAborted(
                    f"circuit breaker: {consec} consecutive server-side "
                    f"failures (>= {thr})")
        return ok, st, d

    for main_raw, clone_raw in pairs:
        if progress:
            progress(f"{main_raw} -> {clone_raw}")
        rec = {"main": main_raw, "clone": clone_raw, "main_id": None,
               "clone_id": None, "status": "ok", "reason": None,
               "groups": {"added": [], "already": [], "failed": []},
               "roles": {"added": [], "already": [], "failed": [],
                         "scanned": scan_roles}}
        m = resolve_identity(client, main_raw)
        c = resolve_identity(client, clone_raw)
        rec["main_id"], rec["clone_id"] = m["account_id"], c["account_id"]
        if m["status"] != "resolved" or c["status"] != "resolved":
            rec["status"] = "blocked"
            rec["reason"] = "; ".join(
                x["reason"] for x in (m, c) if x["status"] != "resolved")
            report["summary"]["blocked"] += 1
            report["pairs"].append(rec)
            continue
        if m["account_id"] == c["account_id"]:
            rec["status"] = "noop"
            rec["reason"] = "main and clone are the same account"
            report["pairs"].append(rec)
            continue

        plan = plan_pair(client, m["account_id"], c["account_id"], role_index)
        rec["groups"]["already"] = plan["groups_already"]
        rec["roles"]["already"] = plan["roles_already"]
        try:
            for g in plan["groups_add"]:
                if dry_run:
                    rec["groups"]["added"].append(g["name"])
                    continue
                ok, st, d = _do_write(
                    lambda g=g: client.add_user_to_group(g["groupId"],
                                                         c["account_id"]))
                if ok:
                    rec["groups"]["added"].append(g["name"])
                else:
                    err = d.get("_error") if isinstance(d, dict) else str(d)
                    # An "already a member" 4xx is success, not a failure.
                    if isinstance(d, dict) and "already" in (err or "").lower():
                        rec["groups"]["already"].append(g["name"])
                    else:
                        rec["groups"]["failed"].append({"group": g["name"],
                                                        "error": f"{st} {err}"})
            for r in plan["roles_add"]:
                if dry_run:
                    rec["roles"]["added"].append(_role_key(r))
                    continue
                ok, st, d = _do_write(
                    lambda r=r: client.add_user_to_project_role(
                        r["project"], r["role_id"], c["account_id"]))
                if ok:
                    rec["roles"]["added"].append(_role_key(r))
                else:
                    err = d.get("_error") if isinstance(d, dict) else str(d)
                    rec["roles"]["failed"].append({"role": _role_key(r),
                                                   "error": f"{st} {err}"})
        except CloneAborted as exc:
            rec["reason"] = str(exc)
            report["pairs"].append(rec)
            report["summary"]["pairs"] = len(report["pairs"])
            _tally(report)
            exc.partial = report
            raise
        report["pairs"].append(rec)

    report["summary"]["pairs"] = len(report["pairs"])
    _tally(report)
    return report


def _tally(report: dict) -> None:
    s = report["summary"]
    s["groups_added"] = sum(len(p["groups"]["added"]) for p in report["pairs"])
    s["roles_added"] = sum(len(p["roles"]["added"]) for p in report["pairs"])
    s["failed"] = sum(len(p["groups"]["failed"]) + len(p["roles"]["failed"])
                      for p in report["pairs"])
    s["blocked"] = sum(1 for p in report["pairs"] if p["status"] == "blocked")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cloneaccess.py -v`
Expected: PASS (all engine tests).

- [ ] **Step 5: Commit**

```bash
cd /mnt/d/Atlassian-Products/Migration-auditor
git add auditor/cloneaccess.py tests/test_cloneaccess.py
git commit -m "feat(cloneaccess): apply + run_clone orchestrator (dry-run, idempotent, breaker)"
```

---

### Task 6: CLI — `migration-auditor clone-access`

**Files:**
- Modify: `webapp/main.py` (add the subparser + handler in `cli()` ~line 1127; add a `run_clone_cli(args)` helper near `run_audit_cli`)
- Test: `tests/test_clone_cli.py`

**Interfaces:**
- Consumes: `auditor.cloneaccess.run_clone`, `CloneAborted`; `Store.list_saved_connections`, `get_saved_connection`, `saved_connection_secret`; `auditor.connectors.get_connector`; `auditor.client.Connection`.
- Produces: `webapp.main.run_clone_cli(args) -> int` (exit code); `parse_pairs_csv(path) -> list[tuple[str,str]]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_clone_cli.py`:

```python
import csv
from webapp.main import parse_pairs_csv


def test_parse_pairs_csv_header_and_extra_columns(tmp_path):
    p = tmp_path / "pairs.csv"
    p.write_text("main,clone,note\n"
                 "a@x.y,a@z.y,hi\n"
                 "557058:1-2,557058:3-4,\n"
                 "\n", encoding="utf-8")          # blank row ignored
    pairs = parse_pairs_csv(str(p))
    assert pairs == [("a@x.y", "a@z.y"), ("557058:1-2", "557058:3-4")]


def test_parse_pairs_csv_requires_columns(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("user,target\na,b\n", encoding="utf-8")
    try:
        parse_pairs_csv(str(p))
        assert False, "expected ValueError"
    except ValueError as e:
        assert "main" in str(e) and "clone" in str(e)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_clone_cli.py -v`
Expected: FAIL — `parse_pairs_csv` not importable.

- [ ] **Step 3: Implement the CSV parser, CLI handler, and subparser**

In `webapp/main.py`, add a module-level helper (near `run_audit_cli`):

```python
def parse_pairs_csv(path: str) -> list:
    """Read a CSV with header columns 'main' and 'clone' (case-insensitive;
    extra columns ignored). Returns [(main, clone), ...], skipping blank rows.
    """
    import csv
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {(c or "").strip().lower(): c for c in (reader.fieldnames or [])}
        if "main" not in cols or "clone" not in cols:
            raise ValueError(
                f"CSV must have 'main' and 'clone' columns; got "
                f"{reader.fieldnames}")
        out = []
        for row in reader:
            main = (row.get(cols["main"]) or "").strip()
            clone = (row.get(cols["clone"]) or "").strip()
            if main or clone:
                out.append((main, clone))
        return out


def run_clone_cli(args) -> int:
    """Headless clone-access. Builds a Jira client from a saved connection and
    runs the engine. Exit: 0 clean, 1 operational error, 2 blocked-or-failed."""
    import sys
    from auditor.cloneaccess import run_clone, CloneAborted, CloneError
    from auditor.client import Connection
    from auditor.connectors import get_connector
    cfg = load_config()
    store = Store(db_path=cfg.db_path, key_path=cfg.key_path,
                  secret_key=cfg.secret_key)
    # Resolve the saved connection by id (numeric) or name.
    rows = store.list_saved_connections("jira")
    match = [r for r in rows
             if str(r["id"]) == str(args.conn) or r["name"] == args.conn]
    if not match:
        print(f"no jira saved connection matching {args.conn!r}", file=sys.stderr)
        return 1
    row = match[0]
    secret = store.saved_connection_secret(row)
    conn = Connection(auth_type="pat", site_url=row["site_url"],
                      deployment=row["deployment"] or "cloud",
                      email=secret.get("email") or None,
                      api_token=secret.get("token"))
    connector = get_connector("jira")
    client = connector.make_client(conn, None)

    if args.csv:
        try:
            pairs = parse_pairs_csv(args.csv)
        except (OSError, ValueError) as e:
            print(f"CSV error: {e}", file=sys.stderr)
            return 1
    elif args.main and args.clone:
        pairs = [(args.main, args.clone)]
    else:
        print("provide --main and --clone, or --csv", file=sys.stderr)
        return 1

    dry_run = not args.apply
    scan_roles = args.apply or args.dry_run     # preview (default) = groups only
    try:
        report = run_clone(client, pairs, dry_run=dry_run, scan_roles=scan_roles,
                           progress=lambda m: print(f"… {m}", file=sys.stderr))
    except CloneError as e:
        partial = getattr(e, "partial", None)
        print(f"aborted: {e}", file=sys.stderr)
        if partial and args.json:
            _write_clone_json(args.json, partial)
        return 1

    _print_clone_summary(report, dry_run)
    if args.json:
        _write_clone_json(args.json, report)
    s = report["summary"]
    return 2 if (s["blocked"] or s["failed"]) else 0


def _print_clone_summary(report, dry_run) -> None:
    mode = "DRY-RUN" if dry_run else "APPLIED"
    s = report["summary"]
    print(f"[{mode}] pairs={s['pairs']} blocked={s['blocked']} "
          f"groups+={s['groups_added']} roles+={s['roles_added']} "
          f"failed={s['failed']}"
          + ("" if report["scanned_roles"] else "  (roles not scanned — preview)"))
    for p in report["pairs"]:
        head = f"  {p['main']} -> {p['clone']} [{p['status']}]"
        if p["status"] in ("blocked", "noop"):
            print(head + f": {p['reason']}")
            continue
        print(head + f": +{len(p['groups']['added'])} groups, "
              f"+{len(p['roles']['added'])} roles "
              f"({len(p['groups']['already'])}/{len(p['roles']['already'])} already)")
        for f in p["groups"]["failed"] + p["roles"]["failed"]:
            print(f"      FAILED {f}")


def _write_clone_json(path, report) -> None:
    import sys
    if path == "-":
        json.dump(report, sys.stdout, indent=2); print()
    else:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
```

In `cli()`, register the subparser (next to the `audit` subparser):

```python
    cp = sub.add_parser("clone-access",
                        help="additively clone a user's groups & project roles "
                             "onto another account (single pair or --csv)")
    cp.add_argument("--conn", required=True,
                    help="saved jira connection name or id (the instance)")
    cp.add_argument("--main", help="source account (accountId or email)")
    cp.add_argument("--clone", help="target account (accountId or email)")
    cp.add_argument("--csv", help="CSV with 'main,clone' columns (bulk)")
    cp.add_argument("--apply", action="store_true",
                    help="perform writes (default is a groups-only preview)")
    cp.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="full plan incl. role scan, write nothing")
    cp.add_argument("--json", nargs="?", const="-", default=None, metavar="PATH",
                    help="write the full JSON report (no PATH or '-' = stdout)")
```

And dispatch it early in `cli()` (next to the `audit` dispatch, before `load_config()` for serve/backup):

```python
    if args.command == "clone-access":
        import sys
        sys.exit(run_clone_cli(args))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_clone_cli.py -v && pytest -q`
Expected: PASS (CSV parser tests + full suite green).

- [ ] **Step 5: Commit**

```bash
cd /mnt/d/Atlassian-Products/Migration-auditor
git add webapp/main.py tests/test_clone_cli.py
git commit -m "feat(cli): migration-auditor clone-access (single pair + CSV, preview/dry-run/apply)"
```

---

## Verification (after all tasks) — shippable checkpoint

- [ ] `pytest -q` green.
- [ ] Manual smoke (operator): `migration-auditor clone-access --conn <gresham> --main <email-or-id> --clone <email-or-id>` → prints a groups-only preview; `--dry-run` adds the role scan; `--apply` performs writes. `--csv pairs.csv` for bulk.
- [ ] README: add a short "Clone user access" section documenting the subcommand, the additive/idempotent guarantees, the non-goals, and `MA_BREAKER_THRESHOLD` / `MA_GATHER_WORKERS` reuse. (Fold into the Task 6 commit or a `docs:` commit.)

**At this point the "proper script" is fully usable.** Phase 2 (web UI) is a separate plan layered on `run_clone`.

## Self-Review

- **Spec coverage (Phase-1 scope):** identity auto-detect → T2; additive group clone → T4/T5; direct project-role clone via scan → T3/T4/T5; preview/dry-run/apply modes → T5/T6; CSV → T6; CLI surface + exit codes → T6; breaker + idempotency → T5; concurrency-equivalent role scan → T3. Web UI + saved-run persistence are **explicitly Phase 2** (separate plan).
- **Placeholder scan:** none — every step has complete code and exact commands.
- **Type consistency:** `run_clone(client, pairs, *, dry_run, scan_roles, progress)`, `plan_pair(client, main_id, clone_id, role_index)`, `build_role_index(client, progress)`, `resolve_identity(client, value)` and the report shape are used identically across T2–T6. Client methods (`user_groups`, `add_user_to_group`, `project_role_map`, `project_role_actors`, `add_user_to_project_role`, `search_users`) match their T1 definitions.
- **Note:** `_apply_handler` test helper has a stray inner `import json`; the implementer should hoist `import json` to the test module top (the brief's code is correct logic, fix the import placement).

## Execution Handoff

Two options: **Subagent-Driven (recommended)** — fresh subagent per task + review; or **Inline**. 
