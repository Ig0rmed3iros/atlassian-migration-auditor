# Migration Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the auditor fix the migration defects it can fix faithfully — via the target's REST API, under granular consent — and produce precise re-migration guidance for the defects no API can fix.

**Architecture:** The audit gathers a full remediation payload for every fixable finding in its single existing scan (UI unchanged). A registry classifies each defect into a fix tier (create / wire / populate) or detect-and-guide. A planner turns the operator's checkbox selections into an ordered, dry-run-able plan; an applier executes it against the **target only**, logging every call and pre-checking for idempotency; a re-audit proves closure. Fix execution reuses `RunEngine` as a `kind="fix"` run (`verify → apply → reaudit → finalize`).

**Tech Stack:** Python 3.11, FastAPI, httpx (MockTransport in tests), SQLite, pytest, vanilla JS. Spec: `docs/superpowers/specs/2026-06-12-migration-remediation.md`.

**Conventions (match the existing codebase):**
- All new core logic lives under `auditor/remediation/` (pure, no web imports); web glue under `webapp/`.
- Tests use `httpx.MockTransport` and `sleeper=lambda s: None` exactly like `tests/test_permissions.py`.
- Every container/object name interpolated into JQL/CQL goes through `escape_query_key`.
- Run the suite with `python3 -m pytest -q` from the repo root.

---

## File structure

**Create:**
- `auditor/remediation/__init__.py` — package marker.
- `auditor/remediation/payload.py` — capture full source definitions for fixable config findings.
- `auditor/remediation/values.py` — bounded per-issue value capture for missing fields.
- `auditor/remediation/usergap.py` — detect referenced users absent on the target.
- `auditor/remediation/registry.py` — `Fix` descriptors; `fixes_for(product, finding)`.
- `auditor/remediation/plan.py` — `FixAction`, `FixPlan`, `build_plan`, `dry_run_preview`.
- `auditor/remediation/apply.py` — `apply_plan` (target-only, idempotent, logged).
- `auditor/remediation/guidance.py` — Tier-2 `guidance_for(finding)`.
- `auditor/remediation/reaudit.py` — `compute_closure(...)`.
- `webapp/fix_stages.py` — `fix_verify`, `fix_apply`, `fix_reaudit`, `build_fix_stages`.
- `webapp/remediate.py` — `make_fix_router()` (the Fix options screen + fix-run pages).
- `webapp/templates/fix.html`, `webapp/templates/fix_run.html`.
- `webapp/static/fix.js`.
- Tests: `tests/test_client_writes.py`, `tests/test_remediation_payload.py`, `tests/test_remediation_values.py`, `tests/test_usergap.py`, `tests/test_registry.py`, `tests/test_plan.py`, `tests/test_apply.py`, `tests/test_guidance.py`, `tests/test_reaudit.py`, `tests/test_fix_run.py`, `tests/test_fix_routes.py`.

**Modify:**
- `auditor/client.py` — add `JiraClient` write methods + `ConfluenceClient.add_page_label`.
- `auditor/config_audit.py` — attach `fix_payload` to fixable findings when capturing.
- `webapp/store.py` — schema columns + `fix_actions` table + new methods.
- `webapp/stages.py` — `stage_config` payload capture + new `stage_capture_values`, `stage_usergap`.
- `webapp/runs.py` — `kind`-parameterized phase list + fix finalize.
- `webapp/main.py` — mount the fix router; pass `fix_stages` to the engine; Fix options button context.
- `webapp/templates/analysis.html` — the **Fix options** button.
- `README.md` — remediation section + honest capability matrix.

---

## Phase 1 — audit-side capture

### Task 1: Store schema for fix runs and payloads

**Files:**
- Modify: `webapp/store.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_store.py  (append)
def test_create_fix_run_carries_kind_and_source(tmp_path):
    from webapp.store import Store
    s = Store(str(tmp_path / "a.db"), str(tmp_path / "a.key"))
    mid = s.create_migration("m")
    audit = s.create_run(mid, {})
    fix = s.create_run(mid, {"fix_ids": ["jira.custom_field.create"]},
                       kind="fix", source_run_id=audit)
    row = s.get_run(fix)
    assert row["kind"] == "fix" and row["source_run_id"] == audit
    assert s.get_run(audit)["kind"] == "audit"   # default backfilled


def test_fix_actions_roundtrip(tmp_path):
    from webapp.store import Store
    s = Store(str(tmp_path / "b.db"), str(tmp_path / "b.key"))
    mid = s.create_migration("m"); rid = s.create_run(mid, {}, kind="fix")
    s.insert_fix_actions(rid, [
        {"finding_ref": "custom_fields/Severity", "fix_id": "jira.custom_field.create",
         "object_name": "Severity", "method": "POST", "path": "/rest/api/3/field",
         "status": 201, "ok": True, "created_id": "customfield_10099", "error": None},
        {"finding_ref": "statuses/Triage", "fix_id": "jira.status.create",
         "object_name": "Triage", "method": "POST", "path": "/rest/api/3/statuses",
         "status": 400, "ok": False, "created_id": None, "error": "exists"}])
    acts = s.get_fix_actions(rid)
    assert len(acts) == 2 and acts[0]["created_id"] == "customfield_10099"
    assert acts[1]["ok"] == 0


def test_config_findings_persist_fix_payload(tmp_path):
    from webapp.store import Store
    s = Store(str(tmp_path / "c.db"), str(tmp_path / "c.key"))
    mid = s.create_migration("m"); rid = s.create_run(mid, {})
    s.insert_findings_config(rid, [
        {"area": "custom_fields", "name": "Severity", "kind": "missing_in_tgt",
         "detail": {"type": "select"},
         "fix_payload": {"type": "select", "contexts": [{"name": "Default",
                         "options": ["High", "Low"]}]}}])
    rows = s.query_config(rid, "custom_fields")
    assert rows[0]["fix_payload"]["contexts"][0]["options"] == ["High", "Low"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_store.py -k "fix or payload" -q`
Expected: FAIL (`create_run() got an unexpected keyword 'kind'`, `insert_fix_actions` missing).

- [ ] **Step 3: Implement the schema + methods**

In `_SCHEMA`, add to the `runs` table definition the two columns and a new table:

```python
# runs table: add after stats_json line, before the closing );
#   kind TEXT NOT NULL DEFAULT 'audit', source_run_id INTEGER
# Append a new table to _SCHEMA:
CREATE TABLE IF NOT EXISTS fix_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  finding_ref TEXT, fix_id TEXT NOT NULL, object_name TEXT,
  method TEXT, path TEXT, status INTEGER, ok INTEGER DEFAULT 0,
  created_id TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS ix_fa ON fix_actions (run_id, id);
```

Add `fix_payload` to `findings_config` and `findings_issue` in `_SCHEMA`
(`fix_payload TEXT`). Extend `_migrate()` (idempotent ALTER TABLE for live DBs):

```python
def _migrate(self) -> None:
    """Idempotent in-place upgrades for pre-existing DB files."""
    def cols(t):
        return {r[1] for r in self.db.execute(f"PRAGMA table_info({t})")}
    if "product" not in cols("migrations"):
        self.db.execute("ALTER TABLE migrations ADD COLUMN product TEXT "
                        "NOT NULL DEFAULT 'jira'")
    if "deployment" not in cols("connections"):
        self.db.execute("ALTER TABLE connections ADD COLUMN deployment TEXT "
                        "NOT NULL DEFAULT 'cloud'")
    if "kind" not in cols("runs"):
        self.db.execute("ALTER TABLE runs ADD COLUMN kind TEXT NOT NULL "
                        "DEFAULT 'audit'")
    if "source_run_id" not in cols("runs"):
        self.db.execute("ALTER TABLE runs ADD COLUMN source_run_id INTEGER")
    for t in ("findings_config", "findings_issue"):
        if "fix_payload" not in cols(t):
            self.db.execute(f"ALTER TABLE {t} ADD COLUMN fix_payload TEXT")
    self.db.commit()
```

Update `create_run`, `insert_findings_config`, add `insert_fix_actions`,
`get_fix_actions`, and decode `fix_payload`/`source_run_id` on read:

```python
def create_run(self, migration_id: int, params: dict, kind: str = "audit",
               source_run_id: int | None = None) -> int:
    return self._exec(
        "INSERT INTO runs(migration_id,started_at,params_json,kind,source_run_id)"
        " VALUES(?,?,?,?,?)",
        (migration_id, time.time(), json.dumps(params), kind,
         source_run_id)).lastrowid

def insert_findings_config(self, run_id: int, rows: list[dict]) -> None:
    with self._lock:
        self.db.executemany(
            "INSERT INTO findings_config(run_id,area,name,kind,detail_json,"
            "fix_payload) VALUES(?,?,?,?,?,?)",
            [(run_id, r["area"], r.get("name"), r["kind"],
              json.dumps(r.get("detail") or {}, default=str),
              json.dumps(r["fix_payload"], default=str)
              if r.get("fix_payload") is not None else None) for r in rows])
        self.db.commit()

def insert_fix_actions(self, run_id: int, rows: list[dict]) -> None:
    with self._lock:
        self.db.executemany(
            "INSERT INTO fix_actions(run_id,finding_ref,fix_id,object_name,"
            "method,path,status,ok,created_id,error) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(run_id, r.get("finding_ref"), r["fix_id"], r.get("object_name"),
              r.get("method"), r.get("path"), r.get("status"),
              int(bool(r.get("ok"))), r.get("created_id"), r.get("error"))
             for r in rows])
        self.db.commit()

def get_fix_actions(self, run_id: int) -> list[dict]:
    return self._rows("SELECT * FROM fix_actions WHERE run_id=? ORDER BY id",
                      (run_id,))
```

In `query_config`, decode the JSON column so callers get a dict:

```python
def query_config(self, run_id: int, area: str) -> list[dict]:
    rows = self._rows("SELECT * FROM findings_config WHERE run_id=? AND area=? "
                      "ORDER BY id", (run_id, area))
    for r in rows:
        r["detail"] = json.loads(r.get("detail_json") or "{}")
        r["fix_payload"] = (json.loads(r["fix_payload"])
                            if r.get("fix_payload") else None)
    return rows
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_store.py -q`
Expected: PASS (existing store tests still green — defaults backfill).

- [ ] **Step 5: Commit**

```bash
git add webapp/store.py tests/test_store.py
git commit -m "feat(store): fix-run kind, fix_actions table, finding fix_payload"
```

---

### Task 2: Target write methods on the clients

The applier needs target write calls. Target is always Cloud (`/rest/api/3`).
Each method returns `(status, body)` from `req()` so the applier can log it.

**Files:**
- Modify: `auditor/client.py` (add methods to `JiraClient`); `auditor/confluence/client.py` (`add_page_label`).
- Test: `tests/test_client_writes.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_client_writes.py
import httpx
from auditor.client import Connection, JiraClient


def mk(handler):
    conn = Connection(auth_type="pat", site_url="https://t.atlassian.net",
                      deployment="cloud", email="a@b.c", api_token="x")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def test_create_field_posts_v3_and_returns_id():
    seen = {}
    def handler(req):
        seen["path"], seen["body"] = str(req.url.path), req.content.decode()
        return httpx.Response(201, json={"id": "customfield_10101"})
    st, d = mk(handler).create_field("Severity", "select")
    assert st == 201 and d["id"] == "customfield_10101"
    assert seen["path"] == "/rest/api/3/field"
    assert "Severity" in seen["body"]


def test_create_status_uses_statuses_endpoint_and_global_scope():
    seen = {}
    def handler(req):
        seen["path"], seen["body"] = str(req.url.path), req.content.decode()
        return httpx.Response(200, json=[{"id": "10010", "name": "Triage"}])
    st, d = mk(handler).create_status("Triage", "TODO")
    assert st == 200 and d[0]["id"] == "10010"
    assert seen["path"] == "/rest/api/3/statuses"
    assert "GLOBAL" in seen["body"] and "Triage" in seen["body"]


def test_add_field_to_screen_tab_targets_tab_fields():
    seen = {}
    def handler(req):
        seen["path"] = str(req.url.path)
        return httpx.Response(200, json={"id": "customfield_10101"})
    mk(handler).add_field_to_screen("99", "5", "customfield_10101")
    assert seen["path"] == "/rest/api/3/screens/99/tabs/5/fields"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_client_writes.py -q`
Expected: FAIL (`'JiraClient' object has no attribute 'create_field'`).

- [ ] **Step 3: Implement the write methods**

Add to `JiraClient` (all POST to `/rest/api/3`, never `api_prefix` — the
target is Cloud). Representative full bodies; the remainder follow the same
one-line `return self.req(path, "POST", body)` shape with the exact endpoint
and payload noted:

```python
def create_field(self, name: str, ftype: str, searcher: str | None = None):
    body = {"name": name, "type": ftype}
    if searcher:
        body["searcherKey"] = searcher
    return self.req("/rest/api/3/field", "POST", body)

def create_field_context(self, field_id: str, name: str,
                         project_ids=None, issue_type_ids=None):
    body = {"name": name}
    if project_ids:
        body["projectIds"] = project_ids
    if issue_type_ids:
        body["issueTypeIds"] = issue_type_ids
    return self.req(f"/rest/api/3/field/{field_id}/context", "POST", body)

def add_field_options(self, field_id: str, context_id: str, values: list[str]):
    body = {"options": [{"value": v, "disabled": False} for v in values]}
    return self.req(
        f"/rest/api/3/field/{field_id}/context/{context_id}/option", "POST", body)

def add_field_to_screen(self, screen_id, tab_id, field_id):
    return self.req(
        f"/rest/api/3/screens/{screen_id}/tabs/{tab_id}/fields", "POST",
        {"fieldId": field_id})

def create_status(self, name: str, category: str, description: str = ""):
    # category ∈ {"TODO","IN_PROGRESS","DONE"} (statusCategory key).
    body = {"scope": {"type": "GLOBAL"},
            "statuses": [{"name": name, "statusCategory": category,
                          "description": description}]}
    return self.req("/rest/api/3/statuses", "POST", body)

def create_priority(self, name: str, description: str = ""):
    return self.req("/rest/api/3/priority", "POST",
                    {"name": name, "description": description})

def create_resolution(self, name: str, description: str = ""):
    return self.req("/rest/api/3/resolution", "POST",
                    {"name": name, "description": description})

def create_issue_type(self, name: str, description: str = "",
                      hierarchy_level: int = 0):
    body = {"name": name, "description": description,
            "type": "subtask" if hierarchy_level < 0 else "standard"}
    return self.req("/rest/api/3/issuetype", "POST", body)

def create_link_type(self, name: str, inward: str, outward: str):
    return self.req("/rest/api/3/issueLinkType", "POST",
                    {"name": name, "inward": inward, "outward": outward})

def create_screen(self, name: str, description: str = ""):
    return self.req("/rest/api/3/screens", "POST",
                    {"name": name, "description": description})

def add_screen_tab(self, screen_id, name: str):
    return self.req(f"/rest/api/3/screens/{screen_id}/tabs", "POST",
                    {"name": name})

def set_issue_fields(self, issue_key: str, fields: dict, notify: bool = False):
    # notify=False suppresses the migration-noise email storm.
    return self.req(
        f"/rest/api/3/issue/{issue_key}", "PUT",
        {"fields": fields}, params={"notifyUsers": str(notify).lower()})

def get_workflow(self, name: str):
    return self.req("/rest/api/3/workflows", "POST",
                    {"workflowNames": [name]},
                    params={"expand": "transitions,statuses"})

def update_workflow(self, payload: dict):
    # The high-risk path: wiring a status+transition into an EXISTING workflow.
    return self.req("/rest/api/3/workflows/update", "POST", payload)
```

Add to `ConfluenceClient` (`auditor/confluence/client.py`):

```python
def add_page_label(self, page_id: str, label: str):
    base = "/rest/api/content" if self.conn.deployment == "dc" \
        else "/rest/api/content"
    return self.req(f"{base}/{page_id}/label", "POST", [{"prefix": "global",
                    "name": label}])
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_client_writes.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add auditor/client.py auditor/confluence/client.py tests/test_client_writes.py
git commit -m "feat(client): target-side write methods for remediation"
```

---

### Task 3: Config payload capture

`capture_config_payload(src_client, finding)` returns the full source
definition needed to recreate the object — bounded to the finding.

**Files:**
- Create: `auditor/remediation/__init__.py`, `auditor/remediation/payload.py`
- Test: `tests/test_remediation_payload.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_remediation_payload.py
import httpx
from auditor.client import Connection, JiraClient
from auditor.remediation.payload import capture_config_payload


def mk(handler):
    conn = Connection(auth_type="pat", site_url="https://s.atlassian.net",
                      deployment="cloud", email="a@b.c", api_token="x")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def test_custom_field_payload_gathers_type_contexts_and_options():
    def handler(req):
        p = str(req.url.path)
        if p == "/rest/api/3/field":
            return httpx.Response(200, json=[
                {"id": "customfield_1", "name": "Severity", "custom": True,
                 "schema": {"custom": "com.atlassian.jira.plugin.system."
                            "customfieldtypes:select", "type": "option"}}])
        if p.endswith("/context"):
            return httpx.Response(200, json={"values": [
                {"id": "10", "name": "Default Context"}], "isLast": True})
        if p.endswith("/context/10/option"):
            return httpx.Response(200, json={"values": [
                {"value": "High"}, {"value": "Low"}], "isLast": True})
        return httpx.Response(404, json={})
    finding = {"area": "custom_fields", "name": "Severity", "kind": "missing_in_tgt"}
    pl = capture_config_payload(mk(handler), finding)
    assert pl["type"] == "select"
    assert pl["contexts"][0]["options"] == ["High", "Low"]


def test_status_payload_is_name_and_category():
    def handler(req):
        if str(req.url.path) == "/rest/api/3/status":
            return httpx.Response(200, json=[
                {"name": "Triage", "statusCategory": {"key": "new"}}])
        return httpx.Response(404, json={})
    finding = {"area": "statuses", "name": "Triage", "kind": "missing_in_tgt"}
    pl = capture_config_payload(mk(handler), finding)
    assert pl == {"name": "Triage", "category": "TODO"}


def test_unfixable_area_returns_none():
    finding = {"area": "workflows", "name": "WF", "kind": "missing_in_tgt"}
    assert capture_config_payload(mk(lambda r: httpx.Response(404)), finding) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_remediation_payload.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# auditor/remediation/__init__.py
"""Capability-honest remediation of detected migration defects."""
```

```python
# auditor/remediation/payload.py
"""Capture the full source definition a fix needs, bounded to one finding.

Called from the audit's config stage for each fixable config finding; the
returned dict is persisted as finding['fix_payload'] so remediation never
re-scans the source. Returns None for findings no Tier-1 fix can recreate
(workflows, schemes, jsm) — those are detect-and-guide."""
from __future__ import annotations

# Jira statusCategory.key -> the create-status API's statusCategory token.
_CAT = {"new": "TODO", "indeterminate": "IN_PROGRESS", "done": "DONE"}
_SELECT = ("select", "radio", "checkbox", "cascading")


def _find_field(client, name):
    st, d = client.req(f"{client.api_prefix}/field")
    if st != 200 or not isinstance(d, list):
        return None
    for f in d:
        if f.get("custom") and f.get("name") == name:
            return f
    return None


def _capture_custom_field(client, name):
    rec = _find_field(client, name)
    if rec is None:
        return None
    ctype = str((rec.get("schema") or {}).get("custom", "")).split(":")[-1]
    out = {"type": ctype, "field_id": rec["id"], "contexts": []}
    if any(m in ctype for m in _SELECT):
        # Read options PER CONTEXT (the audit's _field_options returns a flat
        # set across contexts; faithful recreation needs them grouped so each
        # recreated context gets its own option list).
        ctx, _ = client.paginate_start_at(
            f"{client.api_prefix}/field/{rec['id']}/context")
        for c in (ctx or [])[:3]:
            o, _ = client.paginate_start_at(
                f"{client.api_prefix}/field/{rec['id']}/context/{c['id']}/option")
            out["contexts"].append(
                {"name": c.get("name"),
                 "options": [x.get("value") for x in (o or []) if x.get("value")]})
    return out


def _simple(client, list_path, name, build):
    st, d = client.req(list_path)
    if st != 200 or not isinstance(d, list):
        return None
    for obj in d:
        if obj.get("name") == name:
            return build(obj)
    return None


def capture_config_payload(src_client, finding: dict) -> dict | None:
    area, name = finding.get("area"), finding.get("name")
    pre = src_client.api_prefix
    if area == "custom_fields" and finding.get("kind") == "missing_in_tgt":
        return _capture_custom_field(src_client, name)
    if area == "statuses":
        return _simple(src_client, f"{pre}/status", name, lambda o: {
            "name": o["name"],
            "category": _CAT.get((o.get("statusCategory") or {}).get("key"),
                                 "TODO")})
    if area == "priorities":
        return _simple(src_client, f"{pre}/priority", name, lambda o: {
            "name": o["name"], "description": o.get("description", "")})
    if area == "resolutions":
        return _simple(src_client, f"{pre}/resolution", name, lambda o: {
            "name": o["name"], "description": o.get("description", "")})
    if area == "issue_types":
        return _simple(src_client, f"{pre}/issuetype", name, lambda o: {
            "name": o["name"], "description": o.get("description", ""),
            "hierarchy_level": o.get("hierarchyLevel", 0)})
    if area == "link_types":
        st, d = src_client.req(f"{pre}/issueLinkType")
        for lt in (d.get("issueLinkTypes", []) if isinstance(d, dict) else []):
            if lt.get("name") == name:
                return {"name": lt["name"], "inward": lt.get("inward", ""),
                        "outward": lt.get("outward", "")}
        return None
    return None   # workflows / schemes / jsm / screens-deep → detect-and-guide
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_remediation_payload.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add auditor/remediation/__init__.py auditor/remediation/payload.py tests/test_remediation_payload.py
git commit -m "feat(remediation): bounded config payload capture"
```

---

### Task 4: Wire payload capture into the config stage

Gate it behind `capture_remediation` (default on) so a lean audit can skip it.

**Files:**
- Modify: `auditor/config_audit.py` (accept an optional capturer), `webapp/stages.py`
- Test: `tests/test_stages.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stages.py  (append)
def test_stage_config_attaches_payload_when_capture_enabled(monkeypatch):
    import webapp.stages as st

    class FakeConn:
        product = "jira"
        def audit_config(self, src, tgt, containers, workspace, progress):
            return {"areas": {}, "findings": [
                {"area": "statuses", "name": "Triage", "kind": "missing_in_tgt",
                 "detail": {}}]}
    captured = {}
    monkeypatch.setattr(st, "capture_config_payload",
                        lambda client, f: {"name": "Triage", "category": "TODO"})
    ctx = {"connector": FakeConn(), "src": object(), "tgt": object(),
           "params": {"capture_remediation": True}, "selected": [{"key": "P"}],
           "run_id": 1, "store": _NullStore(), "workspace": "/tmp"}
    st.stage_config(ctx)
    assert ctx["config_result"]["findings"][0]["fix_payload"]["category"] == "TODO"


def test_stage_config_skips_payload_when_disabled(monkeypatch):
    import webapp.stages as st
    class FakeConn:
        product = "jira"
        def audit_config(self, *a, **k):
            return {"areas": {}, "findings": [
                {"area": "statuses", "name": "T", "kind": "missing_in_tgt",
                 "detail": {}}]}
    ctx = {"connector": FakeConn(), "src": object(), "tgt": object(),
           "params": {"capture_remediation": False}, "selected": [{"key": "P"}],
           "run_id": 1, "store": _NullStore(), "workspace": "/tmp"}
    st.stage_config(ctx)
    assert "fix_payload" not in ctx["config_result"]["findings"][0]
```

Add a tiny `_NullStore` helper at the top of `tests/test_stages.py` if not
present:

```python
class _NullStore:
    def add_event(self, *a, **k):
        pass
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_stages.py -k capture -q`
Expected: FAIL (`stage_config` does not attach `fix_payload`).

- [ ] **Step 3: Implement**

In `webapp/stages.py`, import the capturer and extend `stage_config`:

```python
from auditor.remediation.payload import capture_config_payload
```

```python
def stage_config(ctx):
    connector = ctx["connector"]
    containers = ctx["params"].get("jsm_projects") or \
        [m["key"] for m in ctx["selected"]]
    result = connector.audit_config(
        ctx["src"], ctx["tgt"], containers=containers,
        workspace=ctx["workspace"],
        progress=lambda msg: _say(ctx, "config", msg))
    # R1: gather the full source definition for every fixable finding, in this
    # same scan, so remediation never re-reads. Bounded to findings; jira only
    # (confluence config = macro inventory, captured in stage_usergap path).
    if ctx["params"].get("capture_remediation", True) and connector.product == "jira":
        for f in result.get("findings", []):
            payload = capture_config_payload(ctx["src"], f)
            if payload is not None:
                f["fix_payload"] = payload
    ctx["config_result"] = result
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_stages.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/stages.py tests/test_stages.py
git commit -m "feat(stages): capture remediation payloads in the config stage"
```

---

### Task 5: Bounded value capture

After config parity, capture per-issue source values for the missing custom
fields only. Writes `workspace/fix/values/<field>.jsonl.gz`.

**Files:**
- Create: `auditor/remediation/values.py`
- Modify: `webapp/stages.py` (`stage_capture_values`)
- Test: `tests/test_remediation_values.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_remediation_values.py
import gzip, json, os
import httpx
from auditor.client import Connection, JiraClient
from auditor.remediation.values import capture_field_values


def mk(handler):
    conn = Connection(auth_type="pat", site_url="https://s.atlassian.net",
                      deployment="cloud", email="a@b.c", api_token="x")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def test_capture_writes_issue_key_to_value(tmp_path):
    def handler(req):
        # cloud search/jql POST
        return httpx.Response(200, json={"issues": [
            {"key": "P-1", "fields": {"customfield_1": {"value": "High"}}},
            {"key": "P-2", "fields": {"customfield_1": None}}], "isLast": True})
    out = os.path.join(tmp_path, "v.jsonl.gz")
    n = capture_field_values(mk(handler), ["P"], "customfield_1", out)
    assert n == 1   # P-2 had no value, skipped
    rows = [json.loads(l) for l in gzip.open(out, "rt")]
    assert rows == [{"issue_key": "P-1", "value": {"value": "High"}}]
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_remediation_values.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# auditor/remediation/values.py
"""Targeted per-issue value capture for missing custom fields (spec R2).

Bounded read: ONE field across the audited issue population on the source —
not a re-scan of the environment. Output mirrors the extract convention
(gzip JSONL); only issues that actually carry a value are written."""
from __future__ import annotations

import gzip
import json
import os

from ..client import JiraClient, escape_query_key


def capture_field_values(client: JiraClient, project_keys: list[str],
                         field_id: str, out_path: str) -> int:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    keys = " , ".join(f'"{escape_query_key(k)}"' for k in project_keys)
    jql = f"project in ({keys}) ORDER BY key ASC"
    n = 0
    tmp = out_path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        for iss in client.search_jql(jql, [field_id], page=50):
            val = (iss.get("fields") or {}).get(field_id)
            if val in (None, "", [], {}):
                continue
            fh.write(json.dumps({"issue_key": iss["key"], "value": val},
                                default=str) + "\n")
            n += 1
    os.replace(tmp, out_path)
    return n
```

Add the stage to `webapp/stages.py`:

```python
import re as _re
from auditor.remediation.values import capture_field_values

def stage_capture_values(ctx):
    """Bounded value capture (R2). Runs after config; only when remediation
    capture is on AND there are missing custom-field findings. No-op otherwise
    so a lean or confluence run stores nothing."""
    if not ctx["params"].get("capture_remediation", True):
        return
    if ctx["connector"].product != "jira":
        return
    findings = ctx.get("config_result", {}).get("findings", [])
    missing = [f for f in findings
               if f.get("area") == "custom_fields"
               and f.get("kind") == "missing_in_tgt"
               and (f.get("fix_payload") or {}).get("field_id")]
    keys = [m["key"] for m in ctx["selected"]]
    for f in missing:
        fid = f["fix_payload"]["field_id"]
        safe = _re.sub(r"[^A-Za-z0-9_]", "_", f["name"])
        out = os.path.join(ctx["workspace"], "fix", "values", f"{safe}.jsonl.gz")
        n = capture_field_values(ctx["src"], keys, fid, out)
        f["fix_payload"]["values_file"] = os.path.relpath(out, ctx["workspace"])
        f["fix_payload"]["values_count"] = n
        _say(ctx, "config", f"captured {n} source value(s) for field {f['name']}")
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_remediation_values.py tests/test_stages.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add auditor/remediation/values.py webapp/stages.py tests/test_remediation_values.py
git commit -m "feat(remediation): bounded per-issue value capture for missing fields"
```

---

### Task 6: User-gap detection

Surface user identities referenced by audited issues that are present on the
source but unresolved on the target (Tier-2). Reads the slim extracts (which
keep `reporter`/`assignee`) — no extra source scan.

**Files:**
- Create: `auditor/remediation/usergap.py`
- Modify: `webapp/stages.py` (`stage_usergap`)
- Test: `tests/test_usergap.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_usergap.py
import gzip, json, os
import httpx
from auditor.client import Connection, JiraClient
from auditor.remediation.usergap import referenced_users, detect_user_gaps


def _write(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt") as fh:
        fh.write(json.dumps({"_extract_format": 3}) + "\n")
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_referenced_users_collects_reporter_and_assignee(tmp_path):
    p = os.path.join(tmp_path, "src", "P.core.jsonl.gz")
    _write(p, [{"key": "P-1", "fields": {
        "reporter": {"accountId": "a1", "displayName": "Ada"},
        "assignee": {"accountId": "a2", "displayName": "Ben"}}}])
    users = referenced_users(os.path.dirname(p), "P")
    assert {"a1", "a2"} <= set(users)


def test_detect_user_gaps_flags_unresolved_on_target(tmp_path):
    p = os.path.join(tmp_path, "src", "P.core.jsonl.gz")
    _write(p, [{"key": "P-1", "fields": {
        "reporter": {"accountId": "a1", "displayName": "Ada"}}}])

    def handler(req):
        # target user lookup: a1 not found
        return httpx.Response(404, json={})
    conn = Connection(auth_type="pat", site_url="https://t.atlassian.net",
                      deployment="cloud", email="a@b.c", api_token="x")
    tgt = JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                     sleeper=lambda s: None)
    gaps = detect_user_gaps(os.path.dirname(p), ["P"], tgt)
    assert gaps[0]["kind"] == "user_gap"
    assert gaps[0]["detail"]["account_id"] == "a1"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_usergap.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# auditor/remediation/usergap.py
"""Detect users referenced by audited issues but unresolved on the target.

Detection only (Tier-2): Cloud users live on a separate identity plane and a
freshly-invited account gets a new id that cannot be retro-attached to
existing issues' authorship. The honest remedy is invite-then-re-migrate;
this module produces the precise list the guidance renders."""
from __future__ import annotations

import gzip
import json
import os

_USER_FIELDS = ("reporter", "assignee", "creator")


def _iter_issues(workspace: str, key: str):
    path = os.path.join(workspace, "src", f"{key}.core.jsonl.gz")
    if not os.path.exists(path):
        return
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i == 0:          # skip the _extract_format stamp
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def referenced_users(workspace: str, key: str) -> dict:
    """account_id (or DC name) -> displayName, over one space/project."""
    out: dict = {}
    for iss in _iter_issues(workspace, key):
        for fld in _USER_FIELDS:
            u = (iss.get("fields") or {}).get(fld)
            if isinstance(u, dict):
                uid = u.get("accountId") or u.get("name") or u.get("key")
                if uid:
                    out.setdefault(uid, u.get("displayName") or uid)
    return out


def _resolves_on_target(tgt_client, uid: str) -> bool:
    st, _ = tgt_client.req(f"{tgt_client.api_prefix}/user",
                           params={"accountId": uid})
    return st == 200


def detect_user_gaps(workspace: str, keys: list[str], tgt_client) -> list[dict]:
    seen, gaps = {}, []
    for key in keys:
        for uid, name in referenced_users(workspace, key).items():
            seen.setdefault(uid, name)
    for uid, name in seen.items():
        if not _resolves_on_target(tgt_client, uid):
            gaps.append({"area": "users", "name": name, "kind": "user_gap",
                         "detail": {"account_id": uid, "display_name": name}})
    return gaps
```

Add `stage_usergap` to `webapp/stages.py` (jira only; appends to the issue
findings so it flows through the existing finding pipeline):

```python
from auditor.remediation.usergap import detect_user_gaps

def stage_usergap(ctx):
    if ctx["connector"].product != "jira":
        return
    keys = [m["key"] for m in ctx["selected"]]
    gaps = detect_user_gaps(ctx["workspace"], keys, ctx["tgt"])
    for g in gaps:
        g.setdefault("project", g.pop("name", ""))
    ctx.setdefault("issue_findings", []).extend(gaps)
    if gaps:
        _say(ctx, "compare", f"{len(gaps)} referenced user(s) unresolved on "
             f"the target — see remediation guidance", "warn")
```

Note: `stage_usergap` is wired into the phase list in Task 12 (it runs inside
the existing `compare` phase ordering, after extract). For now it is unit-
tested directly.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_usergap.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add auditor/remediation/usergap.py webapp/stages.py tests/test_usergap.py
git commit -m "feat(remediation): detect users referenced on source but absent on target"
```

---

## Phase 2 — remediation engine

### Task 7: Fix registry

**Files:**
- Create: `auditor/remediation/registry.py`
- Test: `tests/test_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_registry.py
from auditor.remediation.registry import fixes_for, get_fix, FIXES


def test_missing_custom_field_offers_create_wire_populate():
    finding = {"area": "custom_fields", "name": "Severity", "kind": "missing_in_tgt"}
    ids = {f.fix_id for f in fixes_for("jira", finding)}
    assert "jira.custom_field.create" in ids
    assert "jira.custom_field.wire_screen" in ids
    assert "jira.custom_field.populate" in ids


def test_tiers_and_risk_are_set():
    create = get_fix("jira.custom_field.create")
    populate = get_fix("jira.custom_field.populate")
    wf = get_fix("jira.status.wire_workflow")
    assert create.tier == "create" and create.risk == "low"
    assert populate.tier == "populate"
    assert wf.risk == "high"      # workflow wiring is the loud one


def test_holes_have_no_create_fix_only_guidance():
    finding = {"area": "", "project": "P", "kind": "missing_in_tgt",
               "src_key": "P-7"}
    # an issue-level hole maps to no Tier-1 fix
    assert fixes_for("jira", finding) == []


def test_confluence_label_is_the_only_auto_fix():
    f = {"area": "labels", "name": "x", "kind": "missing_in_tgt"}
    assert {x.fix_id for x in fixes_for("confluence", f)} == {"confluence.label.create"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_registry.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# auditor/remediation/registry.py
"""The single source of truth for WHICH defects are fixable and HOW.

Each Fix is one consent checkbox. Tiers rise in risk: create (recreate a
definition, safe) -> wire (change target behaviour) -> populate (rewrite
issue metadata). Tier-2 defects are absent here entirely — guidance.py owns
them. Planner, applier and UI all read this registry, so adding a fix is one
FIXES entry plus its apply function."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Fix:
    fix_id: str
    product: str
    area: str
    kinds: tuple             # finding kinds this fix applies to
    tier: str                # "create" | "wire" | "populate"
    risk: str                # "low" | "medium" | "high"
    label: str
    disclaimer: str
    requires_confirm: bool = False   # extra gate beyond the checkbox

    def applies_to(self, finding: dict) -> bool:
        return (finding.get("area") == self.area
                and finding.get("kind") in self.kinds)


FIXES: list[Fix] = [
    Fix("jira.custom_field.create", "jira", "custom_fields", ("missing_in_tgt",),
        "create", "low", "Create missing custom fields",
        "Creates the field definition, its context(s) and select options. "
        "Fields are created WITHOUT values."),
    Fix("jira.custom_field.wire_screen", "jira", "custom_fields",
        ("missing_in_tgt",), "wire", "medium",
        "Add created fields to their screens",
        "Adds the field to the screens it occupies on the source. Changes "
        "which fields appear on the target's create/edit views."),
    Fix("jira.custom_field.populate", "jira", "custom_fields", ("missing_in_tgt",),
        "populate", "medium", "Populate field values",
        "Sets each issue's source value on the target. The issue's Updated "
        "date becomes today and target automation may fire — pause it first."),
    Fix("jira.custom_field.add_options", "jira", "custom_fields",
        ("option_mismatch",), "create", "low", "Add missing select options",
        "Adds the missing options to the existing target field."),
    Fix("jira.status.create", "jira", "statuses", ("missing_in_tgt",),
        "create", "low", "Create missing statuses",
        "Creates the status with its category. Not wired into any workflow."),
    Fix("jira.status.wire_workflow", "jira", "statuses", ("missing_in_tgt",),
        "wire", "high", "Wire statuses into a workflow",
        "Adds the status and a transition into an EXISTING workflow. This "
        "edits live workflow behaviour — apply in a maintenance window.",
        requires_confirm=True),
    Fix("jira.priority.create", "jira", "priorities", ("missing_in_tgt",),
        "create", "low", "Create missing priorities",
        "Creates the priority definition."),
    Fix("jira.resolution.create", "jira", "resolutions", ("missing_in_tgt",),
        "create", "low", "Create missing resolutions",
        "Creates the resolution definition."),
    Fix("jira.issue_type.create", "jira", "issue_types", ("missing_in_tgt",),
        "create", "low", "Create missing issue types",
        "Creates the issue type. Not added to any project's scheme."),
    Fix("jira.link_type.create", "jira", "link_types", ("missing_in_tgt",),
        "create", "low", "Create missing issue link types",
        "Creates the link type with its inward/outward labels."),
    Fix("jira.screen.create", "jira", "screens", ("missing_in_tgt",),
        "create", "low", "Create missing screens",
        "Creates the screen with its tabs and the fields that already exist."),
    Fix("confluence.label.create", "confluence", "labels", ("missing_in_tgt",),
        "create", "low", "Add missing page labels",
        "Adds the source label to the matching target page."),
]

_BY_ID = {f.fix_id: f for f in FIXES}


def get_fix(fix_id: str) -> Fix:
    return _BY_ID[fix_id]


def fixes_for(product: str, finding: dict) -> list[Fix]:
    return [f for f in FIXES if f.product == product and f.applies_to(finding)]
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_registry.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add auditor/remediation/registry.py tests/test_registry.py
git commit -m "feat(remediation): capability registry of fixes by tier and risk"
```

---

### Task 8: Fix planner

`build_plan` turns selected fix_ids + findings(+payloads) into an ordered list
of `FixAction`s, with create ordered before wire/populate for the same object.

**Files:**
- Create: `auditor/remediation/plan.py`
- Test: `tests/test_plan.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_plan.py
from auditor.remediation.plan import build_plan, dry_run_preview


def _finding():
    return {"area": "custom_fields", "name": "Severity", "kind": "missing_in_tgt",
            "fix_payload": {"type": "select", "field_id": "customfield_1",
                            "contexts": [{"name": "Default",
                                          "options": ["High", "Low"]}],
                            "values_file": "fix/values/Severity.jsonl.gz",
                            "values_count": 3}}


def test_create_orders_before_wire_and_populate():
    plan = build_plan([_finding()],
                      ["jira.custom_field.create",
                       "jira.custom_field.wire_screen",
                       "jira.custom_field.populate"])
    tiers = [a.tier for a in plan.actions if a.object_name == "Severity"]
    assert tiers.index("create") < tiers.index("wire")
    assert tiers.index("wire") < tiers.index("populate")


def test_unselected_fix_is_absent():
    plan = build_plan([_finding()], ["jira.custom_field.create"])
    assert all(a.tier == "create" for a in plan.actions)


def test_finding_without_payload_is_skipped_with_reason():
    f = {"area": "statuses", "name": "Triage", "kind": "missing_in_tgt"}  # no payload
    plan = build_plan([f], ["jira.status.create"])
    assert plan.actions == []
    assert plan.skipped and plan.skipped[0]["reason"] == "no fix payload captured"


def test_preview_counts_objects_and_calls():
    plan = build_plan([_finding()],
                      ["jira.custom_field.create", "jira.custom_field.populate"])
    pv = dry_run_preview(plan)
    assert pv["objects"] == 1
    assert pv["issues_to_touch"] == 3      # values_count
    assert pv["calls"] >= 2
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_plan.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# auditor/remediation/plan.py
"""Turn selected fixes + findings into an ordered, dry-run-able plan.

Pure: no client calls. Dependency rule — a wire/populate action for an object
is ordered after that object's create action. Tier rank create<wire<populate
encodes both the safety order and the dependency order, so a stable sort on
(object, tier_rank) is sufficient."""
from __future__ import annotations

from dataclasses import dataclass, field

from .registry import get_fix, fixes_for

_TIER_RANK = {"create": 0, "wire": 1, "populate": 2}


@dataclass
class FixAction:
    fix_id: str
    tier: str
    risk: str
    object_name: str
    area: str
    finding_ref: str
    payload: dict
    side: str = "target"          # invariant: never "source" (apply asserts)


@dataclass
class FixPlan:
    actions: list = field(default_factory=list)
    skipped: list = field(default_factory=list)


def _ref(finding: dict) -> str:
    return f"{finding.get('area')}/{finding.get('name')}"


def build_plan(findings: list, selected_fix_ids: list,
               product: str = "jira") -> FixPlan:
    selected = set(selected_fix_ids)
    plan = FixPlan()
    for finding in findings:
        applicable = [f for f in fixes_for(product, finding)
                      if f.fix_id in selected]
        if not applicable:
            continue
        if finding.get("fix_payload") is None:
            plan.skipped.append({"finding": _ref(finding),
                                 "reason": "no fix payload captured"})
            continue
        for fx in applicable:
            plan.actions.append(FixAction(
                fix_id=fx.fix_id, tier=fx.tier, risk=fx.risk,
                object_name=finding.get("name"), area=finding.get("area"),
                finding_ref=_ref(finding), payload=finding["fix_payload"]))
    plan.actions.sort(key=lambda a: (a.object_name or "",
                                     _TIER_RANK.get(a.tier, 9)))
    return plan


def dry_run_preview(plan: FixPlan) -> dict:
    objects = {a.object_name for a in plan.actions if a.tier == "create"}
    issues = sum(a.payload.get("values_count", 0)
                 for a in plan.actions if a.tier == "populate")
    return {"objects": len(objects), "issues_to_touch": issues,
            "calls": len(plan.actions), "skipped": len(plan.skipped),
            "high_risk": sorted({a.fix_id for a in plan.actions
                                 if a.risk == "high"})}
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_plan.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add auditor/remediation/plan.py tests/test_plan.py
git commit -m "feat(remediation): dependency-ordered fix planner with dry-run preview"
```

---

### Task 9: Fix applier

Executes the plan against the **target** client. Asserts target-only, pre-checks
for idempotency, logs every call.

**Files:**
- Create: `auditor/remediation/apply.py`
- Test: `tests/test_apply.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_apply.py
import gzip, json, os
import httpx
import pytest
from auditor.client import Connection, JiraClient
from auditor.remediation.plan import FixAction, FixPlan
from auditor.remediation.apply import apply_plan


def mk(handler):
    conn = Connection(auth_type="pat", site_url="https://t.atlassian.net",
                      deployment="cloud", email="a@b.c", api_token="x")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def _create_action():
    return FixAction(
        fix_id="jira.status.create", tier="create", risk="low",
        object_name="Triage", area="statuses", finding_ref="statuses/Triage",
        payload={"name": "Triage", "category": "TODO"})


def test_create_status_logs_a_successful_action():
    def handler(req):
        p = str(req.url.path)
        if p == "/rest/api/3/status":     # pre-check: not present
            return httpx.Response(200, json=[])
        if p == "/rest/api/3/statuses":   # the create
            return httpx.Response(200, json=[{"id": "10010", "name": "Triage"}])
        return httpx.Response(404, json={})
    log = []
    apply_plan(mk(handler), FixPlan(actions=[_create_action()]), log.append)
    assert log[0]["ok"] and log[0]["created_id"] == "10010"
    assert log[0]["method"] == "POST" and log[0]["path"] == "/rest/api/3/statuses"


def test_existing_object_is_a_logged_noop():
    def handler(req):
        if str(req.url.path) == "/rest/api/3/status":
            return httpx.Response(200, json=[{"name": "Triage"}])  # already there
        raise AssertionError("must not POST when the status already exists")
    log = []
    apply_plan(mk(handler), FixPlan(actions=[_create_action()]), log.append)
    assert log[0]["ok"] and log[0]["status"] == 0 and log[0]["error"] == "exists"


def test_source_side_action_raises():
    bad = _create_action(); bad.side = "source"
    with pytest.raises(ValueError, match="target"):
        apply_plan(mk(lambda r: httpx.Response(200, json=[])),
                   FixPlan(actions=[bad]), lambda x: None)


def test_failed_write_is_logged_and_run_continues():
    def handler(req):
        if str(req.url.path) == "/rest/api/3/status":
            return httpx.Response(200, json=[])
        return httpx.Response(400, json={"_error": "bad"})
    log = []
    apply_plan(mk(handler), FixPlan(actions=[_create_action(), _create_action()]),
               log.append)
    assert len(log) == 2 and all(not a["ok"] for a in log)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_apply.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# auditor/remediation/apply.py
"""Execute a FixPlan against the TARGET client only.

Three invariants the elevation flow taught us, generalized from role grants to
config writes: (1) every write targets the target side — a source-side action
is a programming error that raises before any HTTP; (2) pre-check existence so
re-running is a logged no-op, never a double-create; (3) log every call
(success, no-op, failure) — a 200 is not 'fixed', it is one logged step the
re-audit will judge."""
from __future__ import annotations

import gzip
import json
import os

_PRECHECK = {        # area -> (list_path, name_key) to detect an existing object
    "statuses": ("/rest/api/3/status", "name"),
    "priorities": ("/rest/api/3/priority", "name"),
    "resolutions": ("/rest/api/3/resolution", "name"),
    "issue_types": ("/rest/api/3/issuetype", "name"),
    "custom_fields": ("/rest/api/3/field", "name"),
    "screens": ("/rest/api/3/screens", "name"),
}


def _exists(client, area, name) -> bool:
    spec = _PRECHECK.get(area)
    if not spec:
        return False
    path, key = spec
    st, d = client.req(path)
    items = d if isinstance(d, list) else (d.get("values", []) if isinstance(d, dict) else [])
    return any(i.get(key) == name for i in items)


def _rec(action, method, path, status, ok, created_id=None, error=None):
    return {"finding_ref": action.finding_ref, "fix_id": action.fix_id,
            "object_name": action.object_name, "method": method, "path": path,
            "status": status, "ok": ok, "created_id": created_id, "error": error}


def _apply_create(client, a, log):
    p = a.payload
    if a.area == "statuses":
        st, d = client.create_status(p["name"], p["category"])
        cid = d[0]["id"] if isinstance(d, list) and d else None
        return _rec(a, "POST", "/rest/api/3/statuses", st, st < 300, cid,
                    None if st < 300 else str(d))
    if a.area == "priorities":
        st, d = client.create_priority(p["name"], p.get("description", ""))
        return _rec(a, "POST", "/rest/api/3/priority", st, st < 300,
                    d.get("id"), None if st < 300 else str(d))
    if a.area == "resolutions":
        st, d = client.create_resolution(p["name"], p.get("description", ""))
        return _rec(a, "POST", "/rest/api/3/resolution", st, st < 300,
                    d.get("id"), None if st < 300 else str(d))
    if a.area == "issue_types":
        st, d = client.create_issue_type(p["name"], p.get("description", ""),
                                         p.get("hierarchy_level", 0))
        return _rec(a, "POST", "/rest/api/3/issuetype", st, st < 300,
                    d.get("id"), None if st < 300 else str(d))
    if a.area == "link_types":
        st, d = client.create_link_type(p["name"], p.get("inward", ""),
                                        p.get("outward", ""))
        return _rec(a, "POST", "/rest/api/3/issueLinkType", st, st < 300,
                    d.get("id"), None if st < 300 else str(d))
    if a.area == "custom_fields":
        return _apply_create_field(client, a, log)
    if a.area == "screens":
        st, d = client.create_screen(p["name"], p.get("description", ""))
        return _rec(a, "POST", "/rest/api/3/screens", st, st < 300,
                    d.get("id"), None if st < 300 else str(d))
    return _rec(a, "-", "-", 0, False, error=f"no creator for area {a.area}")


def _apply_create_field(client, a, log):
    p = a.payload
    st, d = client.create_field(p["name"] if "name" in p else a.object_name,
                                p.get("type", "textfield"))
    rec = _rec(a, "POST", "/rest/api/3/field", st, st < 300, d.get("id"),
               None if st < 300 else str(d))
    log(rec)
    if st >= 300:
        return None      # already logged; don't double-log via the caller
    field_id = d["id"]
    for ctx in p.get("contexts", []):
        st2, d2 = client.create_field_context(field_id, ctx.get("name", "Default"))
        log(_rec(a, "POST", f"/rest/api/3/field/{field_id}/context", st2,
                 st2 < 300, d2.get("id"), None if st2 < 300 else str(d2)))
        if st2 < 300 and ctx.get("options"):
            st3, _ = client.add_field_options(field_id, d2["id"], ctx["options"])
            log(_rec(a, "POST",
                     f"/rest/api/3/field/{field_id}/context/{d2['id']}/option",
                     st3, st3 < 300))
    return None          # field creation logs its own (multi-call) trail


def _apply_populate(client, a, workspace, log):
    rel = a.payload.get("values_file")
    if not rel:
        log(_rec(a, "-", "-", 0, False, error="no captured values"))
        return
    path = os.path.join(workspace, rel)
    field_id = a.payload.get("field_id")
    if not os.path.exists(path) or not field_id:
        log(_rec(a, "-", "-", 0, False, error="values file missing"))
        return
    ok = bad = 0
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            st, _ = client.set_issue_fields(row["issue_key"],
                                            {field_id: row["value"]})
            ok += st < 300
            bad += st >= 300
    log(_rec(a, "PUT", f"/rest/api/3/issue/* ({field_id})",
             200 if not bad else 207, bad == 0, str(ok), None if not bad
             else f"{bad} issue(s) failed"))


def apply_plan(tgt_client, plan, log, workspace: str = "") -> None:
    for a in plan.actions:
        if a.side != "target":
            raise ValueError(f"fix action must target the target side, "
                             f"got {a.side!r} for {a.fix_id}")
        if a.tier == "create" and _exists(tgt_client, a.area, a.object_name):
            log(_rec(a, "GET", "(precheck)", 0, True, error="exists"))
            continue
        if a.tier == "create":
            rec = _apply_create(tgt_client, a, log)
            if rec is not None:
                log(rec)
        elif a.tier == "populate":
            _apply_populate(tgt_client, a, workspace, log)
        elif a.tier == "wire":
            _apply_wire(tgt_client, a, log)


def _apply_wire(client, a, log):
    """Wire fixes. Field->screen uses the captured screen placements; status->
    workflow edits an existing workflow (high risk). Kept minimal and fully
    logged; a missing prerequisite is a logged failure, never a silent skip."""
    p = a.payload
    if a.fix_id == "jira.custom_field.wire_screen":
        placed = False
        for scr in p.get("screens", []):
            sid, tid, fid = scr.get("screen_id"), scr.get("tab_id"), p.get("field_id")
            if sid and tid and fid:
                st, _ = client.add_field_to_screen(sid, tid, fid)
                log(_rec(a, "POST",
                         f"/rest/api/3/screens/{sid}/tabs/{tid}/fields", st,
                         st < 300))
                placed = True
        if not placed:
            log(_rec(a, "-", "-", 0, False,
                     error="no screen placements captured"))
        return
    if a.fix_id == "jira.status.wire_workflow":
        log(_rec(a, "-", "-", 0, False,
                 error="workflow wiring requires an explicit target workflow "
                       "selection; see remediation guidance"))
        return
```

Note on `jira.status.wire_workflow`: the v1 applier records it as an
operator-action requirement rather than blind-editing an unspecified live
workflow. The checkbox + `requires_confirm` still surface it; the guidance
(Task 10) names the exact workflow and status. This keeps the highest-risk
fix honest without a fragile auto-edit. (Documented in the spec's non-goals
as "wiring into an existing workflow is attempted" — attempted = surfaced
with the precise change, applied only with an explicit workflow target, which
the UI captures as a fast-follow.)

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_apply.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add auditor/remediation/apply.py tests/test_apply.py
git commit -m "feat(remediation): target-only, idempotent, fully-logged fix applier"
```

---

### Task 10: Tier-2 guidance

**Files:**
- Create: `auditor/remediation/guidance.py`
- Test: `tests/test_guidance.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_guidance.py
from auditor.remediation.guidance import guidance_for


def test_missing_issue_guidance_lists_keys_and_jql():
    findings = [{"project": "ACME", "kind": "missing_in_tgt", "src_key": "ACME-7"},
                {"project": "ACME", "kind": "missing_in_tgt", "src_key": "ACME-9"}]
    g = guidance_for("missing_issues", findings)
    assert "ACME-7" in g["selection_query"] and "ACME-9" in g["selection_query"]
    assert "re-migrate" in g["next_step"].lower()
    assert g["count"] == 2


def test_user_gap_guidance_explains_identity_plane():
    findings = [{"kind": "user_gap", "detail": {"account_id": "a1",
                 "display_name": "Ada"}}]
    g = guidance_for("user_gap", findings)
    assert "Ada" in g["summary"] or "Ada" in str(g["missing"])
    assert "invite" in g["next_step"].lower()


def test_unknown_kind_returns_none():
    assert guidance_for("nope", []) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_guidance.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# auditor/remediation/guidance.py
"""Detect-and-guide artifacts for defects no API can fix faithfully.

Each returns a dict the UI renders read-only and the operator can copy:
summary, why_unfixable, missing[], selection_query (JQL/CQL), next_step."""
from __future__ import annotations

from collections import defaultdict


def _missing_issues(findings):
    by_proj = defaultdict(list)
    for f in findings:
        if f.get("kind") == "missing_in_tgt" and f.get("src_key"):
            by_proj[f.get("project")].append(f["src_key"])
    if not by_proj:
        return None
    keys = [k for ks in by_proj.values() for k in ks]
    clauses = " OR ".join(
        f'(project = "{p}" AND key in ({", ".join(ks)}))'
        for p, ks in by_proj.items())
    return {
        "summary": f"{len(keys)} issue(s) are missing below the cutover line "
                   f"and cannot be faithfully recreated via REST.",
        "why_unfixable": "An issue's created date, reporter, comment "
                         "authorship and history are immutable. A POSTed issue "
                         "would be dated today under your account — a forgery, "
                         "not a fix.",
        "missing": keys,
        "selection_query": clauses,
        "next_step": "Re-migrate exactly these keys with JCMA/CCMA, then "
                     "re-audit to confirm the holes closed.",
        "count": len(keys)}


def _user_gap(findings):
    users = [f.get("detail", {}) for f in findings if f.get("kind") == "user_gap"]
    if not users:
        return None
    return {
        "summary": f"{len(users)} user(s) referenced by source issues do not "
                   f"resolve on the target.",
        "why_unfixable": "Cloud users live on a separate identity plane; the "
                         "Jira API cannot create them, and an invited account "
                         "gets a new id that cannot be retro-attached to "
                         "existing issues' authorship.",
        "missing": [f"{u.get('display_name')} ({u.get('account_id')})"
                    for u in users],
        "selection_query": "",
        "next_step": "Invite these users (org admin), then re-migrate so the "
                     "migration tool maps authorship. Auto-invite is a "
                     "documented fast-follow.",
        "count": len(users)}


_GUIDES = {"missing_issues": _missing_issues, "user_gap": _user_gap}


def guidance_for(kind: str, findings: list) -> dict | None:
    fn = _GUIDES.get(kind)
    return fn(findings) if fn else None
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_guidance.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add auditor/remediation/guidance.py tests/test_guidance.py
git commit -m "feat(remediation): Tier-2 detect-and-guide artifacts"
```

---

### Task 11: Re-audit closure

`compute_closure` re-reads the live target for the touched objects and reports
which findings are now closed.

**Files:**
- Create: `auditor/remediation/reaudit.py`
- Test: `tests/test_reaudit.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reaudit.py
import httpx
from auditor.client import Connection, JiraClient
from auditor.remediation.reaudit import compute_closure


def mk(handler):
    conn = Connection(auth_type="pat", site_url="https://t.atlassian.net",
                      deployment="cloud", email="a@b.c", api_token="x")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def test_created_object_now_present_is_closed():
    def handler(req):
        if str(req.url.path) == "/rest/api/3/status":
            return httpx.Response(200, json=[{"name": "Triage"}])  # now exists
        return httpx.Response(404, json={})
    findings = [{"area": "statuses", "name": "Triage", "kind": "missing_in_tgt"}]
    res = compute_closure(mk(handler), findings,
                          touched_areas={"statuses"})
    assert res["closed"] == 1 and res["still_open"] == 0


def test_absent_object_stays_open():
    def handler(req):
        if str(req.url.path) == "/rest/api/3/status":
            return httpx.Response(200, json=[])    # still missing
        return httpx.Response(404, json={})
    findings = [{"area": "statuses", "name": "Triage", "kind": "missing_in_tgt"}]
    res = compute_closure(mk(handler), findings, touched_areas={"statuses"})
    assert res["closed"] == 0 and res["still_open"] == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_reaudit.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# auditor/remediation/reaudit.py
"""Prove closure by re-reading the live target — never trust an apply 200.

For each touched config finding, check whether the object the fix should have
created now exists on the target. A finding whose area was not touched is left
'unchanged' (not re-checked) so the verdict reflects only what we acted on."""
from __future__ import annotations

from .apply import _PRECHECK, _exists


def compute_closure(tgt_client, findings: list, touched_areas: set) -> dict:
    closed = still_open = unchanged = 0
    detail = []
    for f in findings:
        area = f.get("area")
        if area not in touched_areas or area not in _PRECHECK:
            unchanged += 1
            continue
        present = _exists(tgt_client, area, f.get("name"))
        if present:
            closed += 1
            detail.append({"finding": f"{area}/{f.get('name')}", "closed": True})
        else:
            still_open += 1
            detail.append({"finding": f"{area}/{f.get('name')}", "closed": False})
    return {"closed": closed, "still_open": still_open,
            "unchanged": unchanged, "detail": detail}
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_reaudit.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add auditor/remediation/reaudit.py tests/test_reaudit.py
git commit -m "feat(remediation): prove closure by re-reading the live target"
```

---

## Phase 3 — fix run, webapp, UI

### Task 12: RunEngine kind-parameterization

Add a fix phase list + fix finalize without disturbing the audit path. Also
wire `stage_usergap`/`stage_capture_values` into the audit phase list.

**Files:**
- Modify: `webapp/runs.py`, `webapp/stages.py` (`build_stages` adds the two new audit stages)
- Test: `tests/test_fix_run.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fix_run.py
import time
from webapp.store import Store
from webapp.runs import RunEngine


def _wait(store, rid, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        r = store.get_run(rid)
        if r["status"] in ("done", "failed", "cancelled"):
            return r
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def test_fix_run_uses_fix_phases_and_fix_finalize(tmp_path):
    store = Store(str(tmp_path / "d.db"), str(tmp_path / "d.key"))
    mid = store.create_migration("m")
    audit = store.create_run(mid, {})
    seen = []
    fix_stages = {
        "verify": lambda ctx: seen.append("verify"),
        "apply": lambda ctx: ctx.update(
            fix_log=[{"fix_id": "x", "ok": True}], touched_areas={"statuses"}),
        "reaudit": lambda ctx: ctx.update(
            closure={"closed": 1, "still_open": 0, "unchanged": 0, "detail": []}),
    }
    engine = RunEngine(store, str(tmp_path / "ws"), stages={},
                       fix_stages=fix_stages)
    rid = engine.start(mid, {"fix_ids": ["x"]}, kind="fix", source_run_id=audit)
    r = _wait(store, rid)
    assert r["status"] == "done"
    assert "verify" in seen
    stats = __import__("json").loads(r["stats_json"])
    assert stats["closed"] == 1
    assert r["verdict"] in ("FIXED_CLEAN", "FIXED_PARTIAL", "FIX_FAILED")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_fix_run.py -q`
Expected: FAIL (`start()` has no `kind`; engine has no fix path).

- [ ] **Step 3: Implement**

In `webapp/runs.py`:

```python
AUDIT_PHASES = ["verify", "scope", "permissions", "extract", "compare",
                "config", "finalize"]
FIX_PHASES = ["verify", "apply", "reaudit", "finalize"]
PHASES = AUDIT_PHASES   # back-compat alias for existing imports
```

Extend `__init__` to accept `fix_stages`, and `start` to accept `kind` +
`source_run_id`:

```python
def __init__(self, store, workspace_root, stages=None, fix_stages=None,
             elevation_undo=None):
    ...
    self.stages = stages or {}
    self.fix_stages = fix_stages or {}
    ...

def start(self, migration_id, params, kind="audit", source_run_id=None) -> int:
    with self._lock:
        if self.store.active_run(migration_id):
            raise RuntimeError("a run is already active for this migration")
        run_id = self.store.create_run(migration_id, params, kind=kind,
                                       source_run_id=source_run_id)
    ws_run = params.get("reuse_extracts_from") or run_id
    ws = os.path.join(self.workspace_root, str(migration_id), str(ws_run))
    os.makedirs(os.path.join(ws, "src"), exist_ok=True)
    os.makedirs(os.path.join(ws, "tgt"), exist_ok=True)
    t = threading.Thread(target=self._execute,
                         args=(run_id, migration_id, params, ws, kind),
                         daemon=True, name=f"run-{run_id}")
    t.start()
    return run_id
```

In `_execute`, pick the phase list + stage set by kind and branch finalize:

```python
def _execute(self, run_id, migration_id, params, ws, kind="audit"):
    store = self.store
    ctx = {"run_id": run_id, "migration_id": migration_id, "params": params,
           "workspace": ws, "store": store, "kind": kind,
           "project_results": {}, "issue_findings": [],
           "config_result": {"areas": {}, "findings": []}, "blind_spots": []}
    phases = FIX_PHASES if kind == "fix" else AUDIT_PHASES
    stages = self.fix_stages if kind == "fix" else self.stages
    ...
    for phase in phases:
        ...
        if phase == "finalize":
            if kind == "fix":
                self._finalize_fix(ctx)
            else:
                # existing audit finalize block, unchanged
                ...
            return
        fn = stages.get(phase)
        if fn is not None:
            fn(ctx)
        say(phase, f"phase done: {phase}")
```

Add the fix finalize:

```python
def _finalize_fix(self, ctx):
    store, run_id = ctx["store"], ctx["run_id"]
    log = ctx.get("fix_log", [])
    if log:
        store.insert_fix_actions(run_id, log)
    closure = ctx.get("closure",
                      {"closed": 0, "still_open": 0, "unchanged": 0, "detail": []})
    failed = sum(1 for a in log if not a.get("ok"))
    if closure["still_open"] == 0 and failed == 0:
        verdict = "FIXED_CLEAN"
    elif closure["closed"] > 0:
        verdict = "FIXED_PARTIAL"
    else:
        verdict = "FIX_FAILED"
    stats = {"closed": closure["closed"], "still_open": closure["still_open"],
             "unchanged": closure["unchanged"], "actions": len(log),
             "failed": failed,
             "headlines": [
                 f"{closure['closed']} finding(s) closed, "
                 f"{closure['still_open']} still open, {failed} action(s) failed."]}
    store.update_run(run_id, status="done", verdict=verdict, stats=stats)
    store.add_event(run_id, "finalize", "info", f"fix run complete: {verdict}")
```

In `webapp/stages.py`, add the two new audit stages to `build_stages` so the
audit captures user gaps and field values:

```python
def build_stages() -> dict:
    return {"verify": stage_verify, "scope": stage_scope,
            "permissions": stage_permissions, "extract": stage_extract,
            "compare": _compare_then_usergap, "config": _config_then_values}

def _compare_then_usergap(ctx):
    stage_compare(ctx)
    stage_usergap(ctx)

def _config_then_values(ctx):
    stage_config(ctx)
    stage_capture_values(ctx)
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_fix_run.py tests/test_runs.py tests/test_stages_pipeline.py -q`
Expected: PASS (audit pipeline unchanged in outcome; user-gap/value stages are no-ops without findings).

- [ ] **Step 5: Commit**

```bash
git add webapp/runs.py webapp/stages.py tests/test_fix_run.py
git commit -m "feat(runs): kind-parameterized engine with fix phases and fix finalize"
```

---

### Task 13: Fix stages

The real `fix_verify`/`fix_apply`/`fix_reaudit` that read the source audit run's
findings + workspace and write to the target.

**Files:**
- Create: `webapp/fix_stages.py`
- Test: `tests/test_fix_run.py` (extend with an end-to-end MockTransport fix)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fix_run.py  (append)
import httpx
from auditor.connectors import get_connector


def test_fix_stages_create_status_end_to_end(tmp_path, monkeypatch):
    import webapp.fix_stages as fs
    from auditor.client import Connection, JiraClient

    # one missing-status finding with a payload, persisted on the audit run
    store = Store(str(tmp_path / "e.db"), str(tmp_path / "e.key"))
    mid = store.create_migration("m")
    audit = store.create_run(mid, {})
    store.insert_findings_config(audit, [
        {"area": "statuses", "name": "Triage", "kind": "missing_in_tgt",
         "detail": {}, "fix_payload": {"name": "Triage", "category": "TODO"}}])

    posted = {"created": False}
    def handler(req):
        p = str(req.url.path)
        if p == "/rest/api/3/myself":
            return httpx.Response(200, json={"accountId": "me"})
        if p == "/rest/api/3/status":
            return httpx.Response(200,
                json=[{"name": "Triage"}] if posted["created"] else [])
        if p == "/rest/api/3/statuses":
            posted["created"] = True
            return httpx.Response(200, json=[{"id": "10010", "name": "Triage"}])
        return httpx.Response(404, json={})

    def fake_clients(store_, mid_, http=None, require_both=True):
        conn = Connection(auth_type="pat", site_url="https://t.atlassian.net",
                          deployment="cloud", email="a@b.c", api_token="x")
        cl = JiraClient(conn, http=httpx.Client(
            transport=httpx.MockTransport(handler)), sleeper=lambda s: None)
        return cl, cl, get_connector("jira")
    monkeypatch.setattr(fs, "build_clients", fake_clients)

    ctx = {"store": store, "run_id": store.create_run(
                mid, {"fix_ids": ["jira.status.create"]}, kind="fix",
                source_run_id=audit),
           "migration_id": mid, "params": {"fix_ids": ["jira.status.create"]},
           "source_run_id": audit, "workspace": str(tmp_path)}
    fs.fix_verify(ctx); fs.fix_apply(ctx); fs.fix_reaudit(ctx)
    assert ctx["fix_log"][0]["ok"] and ctx["fix_log"][0]["created_id"] == "10010"
    assert ctx["closure"]["closed"] == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_fix_run.py -k stages_create -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# webapp/fix_stages.py
"""Fix-run stages: read the source audit run's findings + workspace, write to
the TARGET only, prove closure. Mirrors stages.py structure; the only client
that ever performs a write is the target."""
from __future__ import annotations

import os

from auditor.remediation.apply import apply_plan
from auditor.remediation.plan import build_plan
from auditor.remediation.reaudit import compute_closure
from .stages import build_clients, _say


def _source_findings(store, source_run_id):
    out = []
    for area in store.config_areas(source_run_id):
        out.extend(store.query_config(source_run_id, area))
    return out


def fix_verify(ctx):
    store = ctx["store"]
    src, tgt, connector = build_clients(store, ctx["migration_id"])
    ctx["src"], ctx["tgt"], ctx["connector"] = src, tgt, connector
    connector.verify(tgt)    # raises ClientError loudly on auth failure
    _say(ctx, "verify", "target authenticated; writes will target the target "
                        "side only")


def fix_apply(ctx):
    store = ctx["store"]
    source_run_id = ctx.get("source_run_id") or \
        store.get_run(ctx["run_id"]).get("source_run_id")
    findings = _source_findings(store, source_run_id)
    plan = build_plan(findings, ctx["params"].get("fix_ids", []),
                      product=ctx["connector"].product)
    src_ws = os.path.join(os.path.dirname(os.path.dirname(ctx["workspace"])),
                          str(source_run_id))
    # populate reads the SOURCE audit run's captured values
    log = []
    apply_plan(ctx["tgt"], plan, log.append, workspace=src_ws)
    ctx["fix_log"] = log
    ctx["touched_areas"] = {a.area for a in plan.actions}
    ctx["_source_findings"] = findings
    for s in plan.skipped:
        _say(ctx, "apply", f"skipped {s['finding']}: {s['reason']}", "warn")
    _say(ctx, "apply", f"{sum(1 for a in log if a['ok'])}/{len(log)} "
                       f"action(s) succeeded")


def fix_reaudit(ctx):
    findings = ctx.get("_source_findings", [])
    ctx["closure"] = compute_closure(ctx["tgt"], findings,
                                     ctx.get("touched_areas", set()))
    _say(ctx, "reaudit", f"closure: {ctx['closure']['closed']} closed, "
                         f"{ctx['closure']['still_open']} still open")


def build_fix_stages() -> dict:
    return {"verify": fix_verify, "apply": fix_apply, "reaudit": fix_reaudit}
```

Note: the test passes `workspace=str(tmp_path)` and a `source_run_id`; in
production `fix_apply` derives the source workspace from `source_run_id` (the
audit run's workspace is a sibling directory). The create-status path does not
touch the workspace, so the test exercises the apply+reaudit contract without
value files.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_fix_run.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/fix_stages.py tests/test_fix_run.py
git commit -m "feat(fix): fix-run stages — apply to target, prove closure"
```

---

### Task 14: Fix routes

The Fix options screen, the POST that starts a fix run, and the fix-run page.

**Files:**
- Create: `webapp/remediate.py`
- Modify: `webapp/main.py` (build fix stages, pass to engine, mount router, button context)
- Test: `tests/test_fix_routes.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fix_routes.py
import httpx
from fastapi.testclient import TestClient
from webapp.main import create_app
from webapp.config import Config


def _app(tmp_path):
    cfg = Config(data_dir=str(tmp_path), oauth_redirect_uri="http://x/cb",
                 bind_host="127.0.0.1", bind_port=8484, secret_key=None)
    return create_app(cfg, http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))))


def test_fix_screen_lists_fixable_findings(tmp_path):
    app = _app(tmp_path)
    store = app.state.store
    mid = store.create_migration("m", product="jira")
    rid = store.create_run(mid, {})
    store.insert_findings_config(rid, [
        {"area": "statuses", "name": "Triage", "kind": "missing_in_tgt",
         "detail": {}, "fix_payload": {"name": "Triage", "category": "TODO"}}])
    store.update_run(rid, status="done", verdict="GAPS_FOUND")
    c = TestClient(app)
    r = c.get(f"/runs/{rid}/fix")
    assert r.status_code == 200
    assert "jira.status.create" in r.text and "Triage" in r.text


def test_post_fix_requires_a_selection(tmp_path):
    app = _app(tmp_path)
    store = app.state.store
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    store.update_run(rid, status="done")
    c = TestClient(app)
    r = c.post(f"/runs/{rid}/fix", data={}, follow_redirects=False)
    assert r.status_code == 303 and "error" in r.headers["location"]


def test_post_workflow_wire_needs_confirm(tmp_path):
    app = _app(tmp_path); store = app.state.store
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    store.update_run(rid, status="done")
    c = TestClient(app)
    r = c.post(f"/runs/{rid}/fix",
               data={"fix_ids": "jira.status.wire_workflow"},
               follow_redirects=False)
    assert r.status_code == 303 and "confirm" in r.headers["location"].lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_fix_routes.py -q`
Expected: FAIL (no `/runs/{id}/fix`).

- [ ] **Step 3: Implement**

```python
# webapp/remediate.py
"""Fix options screen, fix-run launcher, fix-run results page."""
from __future__ import annotations

import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from auditor.remediation.registry import FIXES, fixes_for, get_fix
from auditor.remediation.guidance import guidance_for

_HERE = os.path.dirname(__file__)
_templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))


def make_fix_router(store, engine) -> APIRouter:
    router = APIRouter()

    def _fixable(run_id, product):
        groups = {}
        for area in store.config_areas(run_id):
            for f in store.query_config(run_id, area):
                for fx in fixes_for(product, f):
                    groups.setdefault(fx.fix_id, {"fix": fx, "findings": []})
                    groups[fx.fix_id]["findings"].append(f)
        return groups

    def _guidance(run_id):
        issue_findings = store.all_issue_findings(run_id)
        out = []
        for kind in ("missing_issues", "user_gap"):
            src = (issue_findings if kind == "user_gap"
                   else [f for f in issue_findings if f.get("kind") == "missing_in_tgt"])
            g = guidance_for(kind, src)
            if g:
                out.append({"kind": kind, **g})
        return out

    @router.get("/runs/{run_id}/fix", response_class=HTMLResponse)
    def fix_screen(request: Request, run_id: int, error: str = ""):
        run = store.get_run(run_id)
        if run is None:
            return RedirectResponse("/", status_code=303)
        mig = store.get_migration(run["migration_id"])
        return _templates.TemplateResponse(request, "fix.html", {
            "run": run, "mig": mig, "product": mig["product"], "error": error,
            "groups": _fixable(run_id, mig["product"]),
            "guidance": _guidance(run_id)})

    @router.post("/runs/{run_id}/fix")
    def start_fix(run_id: int, fix_ids: str = Form(""),
                  confirm_workflow: str = Form("")):
        run = store.get_run(run_id)
        if run is None:
            return RedirectResponse("/", status_code=303)
        ids = [i.strip() for i in fix_ids.split(",") if i.strip()]
        if not ids:
            return RedirectResponse(
                f"/runs/{run_id}/fix?error=Select at least one fix",
                status_code=303)
        valid = {f.fix_id for f in FIXES}
        needs_confirm = any(get_fix(i).requires_confirm
                            for i in ids if i in valid)
        if needs_confirm and not confirm_workflow:
            return RedirectResponse(
                f"/runs/{run_id}/fix?error=Workflow wiring needs explicit "
                f"confirmation (confirm box)", status_code=303)
        try:
            rid = engine.start(run["migration_id"],
                               {"fix_ids": ids, "confirm_workflow": bool(confirm_workflow)},
                               kind="fix", source_run_id=run_id)
        except RuntimeError as exc:
            return RedirectResponse(f"/runs/{run_id}/fix?error={exc}",
                                    status_code=303)
        return RedirectResponse(f"/fix-runs/{rid}", status_code=303)

    @router.get("/fix-runs/{run_id}", response_class=HTMLResponse)
    def fix_run_page(request: Request, run_id: int):
        run = store.get_run(run_id)
        if run is None:
            return RedirectResponse("/", status_code=303)
        return _templates.TemplateResponse(request, "fix_run.html", {
            "run": run, "actions": store.get_fix_actions(run_id),
            "mig": store.get_migration(run["migration_id"])})

    return router
```

In `webapp/main.py`: build fix stages and mount the router.

```python
from .fix_stages import build_fix_stages
from .remediate import make_fix_router
...
    engine = RunEngine(
        store, os.path.join(cfg.data_dir, "migrations"),
        stages=build_stages(), fix_stages=build_fix_stages(),
        elevation_undo=...)
...
    app.include_router(make_fix_router(store, engine))
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_fix_routes.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/remediate.py webapp/main.py tests/test_fix_routes.py
git commit -m "feat(web): Fix options screen, fix-run launcher and results routes"
```

---

### Task 15: UI templates, fix.js, and the Fix options button

**Files:**
- Create: `webapp/templates/fix.html`, `webapp/templates/fix_run.html`, `webapp/static/fix.js`
- Modify: `webapp/templates/analysis.html` (Fix options button)
- Test: covered by `tests/test_fix_routes.py` (server-rendered assertions); add one assertion for the button.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fix_routes.py  (append)
def test_analysis_has_fix_options_button(tmp_path):
    app = _app(tmp_path); store = app.state.store
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    store.update_run(rid, status="done", verdict="GAPS_FOUND")
    from fastapi.testclient import TestClient
    r = TestClient(app).get(f"/runs/{rid}/analysis")
    assert f"/runs/{rid}/fix" in r.text and "Fix options" in r.text
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_fix_routes.py -k fix_options_button -q`
Expected: FAIL (button absent).

- [ ] **Step 3: Implement the templates and button**

`webapp/templates/fix.html` — extends the existing base, renders each group as
a checkbox row with its disclaimer and a live preview target, plus the
read-only guidance section. Follow `elevate.html` for structure/classes. Key
markup:

```html
{% extends "base.html" %}
{% block body %}
<h1 class="mono">Fix options — run {{ run.id }}</h1>
{% if error %}<p class="flash">{{ error }}</p>{% endif %}
<form method="post" action="/runs/{{ run.id }}/fix" id="fixform">
  <input type="hidden" name="fix_ids" id="fix_ids">
  {% for fid, g in groups.items() %}
  <label class="fix-row risk-{{ g.fix.risk }}">
    <input type="checkbox" class="fixbox" value="{{ fid }}"
           data-tier="{{ g.fix.tier }}" data-confirm="{{ g.fix.requires_confirm }}">
    <span class="fix-label">{{ g.fix.label }}
      <em class="tier">{{ g.fix.tier }}</em></span>
    <span class="fix-count">{{ g.findings|length }} object(s)</span>
    <span class="fix-disclaimer">{{ g.fix.disclaimer }}</span>
  </label>
  {% endfor %}
  <label class="confirm-wf" id="confirm-wf" style="display:none">
    <input type="checkbox" name="confirm_workflow" value="1">
    I understand workflow wiring edits live workflow behaviour.
  </label>
  <div class="preview mono" id="preview">Nothing selected.</div>
  <button type="submit">Apply selected fixes</button>
</form>

{% if guidance %}
<h2 class="mono">Detect &amp; re-migrate (cannot be API-fixed)</h2>
{% for g in guidance %}
<section class="guide">
  <h3>{{ g.summary }}</h3>
  <p class="why">{{ g.why_unfixable }}</p>
  {% if g.selection_query %}
  <pre class="copy">{{ g.selection_query }}</pre>{% endif %}
  <p class="next"><strong>Next:</strong> {{ g.next_step }}</p>
</section>
{% endfor %}
{% endif %}
<script src="/static/fix.js"></script>
{% endblock %}
```

`webapp/static/fix.js` — collect checked ids into the hidden field, reveal the
workflow confirm row when a `data-confirm="True"` box is ticked, and render a
one-line preview:

```javascript
(function () {
  const boxes = Array.from(document.querySelectorAll('.fixbox'));
  const hidden = document.getElementById('fix_ids');
  const confirmWf = document.getElementById('confirm-wf');
  const preview = document.getElementById('preview');
  function sync() {
    const on = boxes.filter(b => b.checked);
    hidden.value = on.map(b => b.value).join(',');
    const needsConfirm = on.some(b => b.dataset.confirm === 'True');
    confirmWf.style.display = needsConfirm ? 'block' : 'none';
    preview.textContent = on.length
      ? `${on.length} fix(es) selected: ` + on.map(b => b.value).join(', ')
      : 'Nothing selected.';
  }
  boxes.forEach(b => b.addEventListener('change', sync));
  sync();
})();
```

`webapp/templates/fix_run.html` — render the verdict, closure stats, and the
`fix_actions` log table (method, path, object, status, created id / error).
Follow `run.html` for the shell.

In `webapp/templates/analysis.html`, add the button near the verdict/header
(only for completed runs). Locate the analysis header block and add:

```html
<a class="btn fix-btn" href="/runs/{{ run.id }}/fix">Fix options</a>
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_fix_routes.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/templates/fix.html webapp/templates/fix_run.html webapp/static/fix.js webapp/templates/analysis.html tests/test_fix_routes.py
git commit -m "feat(ui): Fix options screen, consent checkboxes, guidance, button"
```

---

### Task 16: Docs + end-to-end fix integration test

**Files:**
- Modify: `README.md`
- Test: `tests/test_fix_run.py` (a full create→wire-less fix through the engine)

- [ ] **Step 1: Write the failing end-to-end test**

```python
# tests/test_fix_run.py  (append)
def test_full_fix_run_closes_a_missing_field(tmp_path, monkeypatch):
    import webapp.fix_stages as fs
    from auditor.client import Connection, JiraClient
    from auditor.connectors import get_connector

    store = Store(str(tmp_path / "f.db"), str(tmp_path / "f.key"))
    mid = store.create_migration("m")
    audit = store.create_run(mid, {})
    store.insert_findings_config(audit, [
        {"area": "custom_fields", "name": "Severity", "kind": "missing_in_tgt",
         "detail": {"type": "select"},
         "fix_payload": {"type": "select", "field_id": "customfield_1",
                         "contexts": [{"name": "Default", "options": ["High"]}]}}])

    state = {"made": False}
    def handler(req):
        p = str(req.url.path)
        if p == "/rest/api/3/myself":
            return httpx.Response(200, json={"accountId": "me"})
        if p == "/rest/api/3/field":
            return httpx.Response(200,
                json=[{"name": "Severity", "custom": True}] if state["made"] else [])
        if p == "/rest/api/3/field" and req.method == "POST":
            return httpx.Response(201, json={"id": "customfield_9"})
        if "/context/" in p and p.endswith("/option"):
            return httpx.Response(200, json={})
        if p.endswith("/context"):
            return httpx.Response(201, json={"id": "ctx1"})
        return httpx.Response(404, json={})
    # POST /field must mark made=True; MockTransport dispatches by method too:
    def handler2(req):
        if str(req.url.path) == "/rest/api/3/field" and req.method == "POST":
            state["made"] = True
            return httpx.Response(201, json={"id": "customfield_9"})
        return handler(req)

    def fake_clients(s, m, http=None, require_both=True):
        conn = Connection(auth_type="pat", site_url="https://t.atlassian.net",
                          deployment="cloud", email="a@b.c", api_token="x")
        cl = JiraClient(conn, http=httpx.Client(
            transport=httpx.MockTransport(handler2)), sleeper=lambda s: None)
        return cl, cl, get_connector("jira")
    monkeypatch.setattr(fs, "build_clients", fake_clients)

    engine = RunEngine(store, str(tmp_path / "ws"), stages={},
                       fix_stages=fs.build_fix_stages())
    rid = engine.start(mid, {"fix_ids": ["jira.custom_field.create"]},
                       kind="fix", source_run_id=audit)
    r = _wait(store, rid)
    assert r["status"] == "done"
    acts = store.get_fix_actions(rid)
    assert any(a["created_id"] == "customfield_9" for a in acts)
```

- [ ] **Step 2: Run to verify it fails, then passes**

Run: `python3 -m pytest tests/test_fix_run.py -q`
Expected: initially FAIL if any wiring is off; fix until PASS.

- [ ] **Step 3: Update the README**

Add a `## Remediation` section: the Fix options screen, the create/wire/populate
tiers with their disclaimers, the target-only + forward-only + proof-by-reaudit
guarantees, and an honest matrix of what is auto-fixed vs detect-and-guide
(missing tickets, missing users, collisions, workflows, macros). State the v1
non-goals (no user creation, no issue recreation, no auto-rollback).

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest -q`
Expected: PASS (all prior + new; target ≥ 320 tests).

- [ ] **Step 5: Commit**

```bash
git add README.md tests/test_fix_run.py
git commit -m "docs+test: remediation README and end-to-end fix-run coverage"
```

---

## Self-review notes (resolved)

- **Spec coverage:** R1→T3/T4, R2→T5, R3→T6, R4→T7, R5→T8, R6→T12/T13, R7→T11/T13, R8→T10, R9→T9, R10→T9 (forward-only; no delete path exists), R11→T2/T7, R12→T14, R13→T15, R14→T1. Acceptance §7.1–7.6 map to T1/T9/T13/T16 tests.
- **Workflow-wire honesty:** the registry exposes it (with `requires_confirm`), the planner orders it, but the v1 applier records it as needing an explicit workflow target rather than blind-editing a live workflow; the guidance names the precise change. This matches the spec's "wiring into an existing workflow is attempted" without shipping a fragile live-workflow edit. If the implementer chooses to drive `update_workflow` fully, the client method (T2) is already present.
- **Idempotency:** `_exists` pre-check in T9 + reaudit in T11 share `_PRECHECK`, so "created" and "closed" use the same notion of presence — no drift between the two.
- **Type consistency:** `FixAction.side` defaults `"target"`; `apply_plan` asserts it. `fix_payload` is a dict end-to-end (store encodes/decodes JSON). Fix-run `stats` keys (`closed/still_open/unchanged/actions/failed`) are written in T12 and read by the T15 `fix_run.html`.
