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

## Post-review amendments (applied)

- Membership pre-check in `apply_elevation`: before each POST, a GET on the project role is issued to detect existing membership. If the account is already a member, the grant row is recorded with `added: False` and the POST is skipped. This is a plan-level defect inherited from the reference `grant_admin.py` — the original code always POSTed, which caused `undo_elevation` to potentially delete memberships it didn't create.
- `undo_elevation` skip condition changed from `if not g.get("ok")` to `if not g.get("added")` — pre-existing memberships (added=False) are never deleted even though their ok=True.
- Error bodies surfaced on failed grant and undo rows: `row["error"] = (d or {}).get("_error")`.
- `detect_blind_spots` now emits `"indeterminate": True` when `search_count is None` (approx_count errored) but `insight_count` is a positive int — callers can surface a "could not verify" warning without incorrectly flagging a blind spot.
- 4 new tests added: `test_already_member_is_never_undone`, `test_undo_skips_unadded_grants_explicitly`, `test_blind_spot_indeterminate_when_count_errors`, `test_blind_spot_threshold_boundaries`. Result: 9 tests in test_permissions.py, 44 total.
