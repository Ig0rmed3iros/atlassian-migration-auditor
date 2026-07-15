# User Access Cloning — Phase 2 (Web UI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A "Clone access" web page that drives the Phase-1 engine: pick a saved Jira connection, enter a single `main→clone` pair or upload a `main,clone` CSV, get a read-only **preview**, then **apply** as a background run with a live log + results.

**Architecture:** A `clone_runs` table + CRUD, a small `CloneRunner` (daemon thread calling `auditor.cloneaccess.run_clone`, persisting log + report), a FastAPI router (`make_clone_router`), and two Jinja templates. The engine's safety (no-write-in-preview, additive, identity-gated, breaker) is already done and reused unchanged. Spec: `docs/superpowers/specs/2026-06-24-user-access-cloning-design.md`.

**Tech Stack:** Python ≥3.11, FastAPI + Jinja2, SQLite (the app's `Store`), `pytest` + `TestClient`. No new dependencies.

## Global Constraints

- **Reuse the engine unchanged.** All cloning goes through `auditor.cloneaccess.run_clone(client, pairs, *, dry_run, scan_roles, progress)`. Do not reimplement clone logic in the web layer.
- **Preview is the default, read-only path.** The page's primary action is a groups-only **preview** (`dry_run=True, scan_roles=False`) that writes nothing. **Apply** is a separate, explicit action.
- **Apply runs in the background.** `--apply` equivalent = `dry_run=False, scan_roles=True` in a daemon thread; the page polls for progress + final report. A **dry-run** apply (`dry_run=True, scan_roles=True`) is offered via a checkbox.
- **Single-instance.** A clone run targets ONE saved Jira connection (`saved_connections`, product `jira`). Both main and clone must be accounts on it.
- **Build the client like `_verify_saved`:** `Connection(auth_type="pat", site_url=row["site_url"], deployment=row["deployment"] or "cloud", email=secret.get("email") or None, api_token=secret.get("token"))` then `connector.make_client(conn, http)` where `http = app.state.http`.
- **Schema:** add `clone_runs` via an idempotent `CREATE TABLE IF NOT EXISTS` in `Store._migrate`, and bump `SCHEMA_VERSION` 1 → 2.
- **No new dependencies; Python ≥3.11.** `pytest -q` green before each task's commit.
- Repo root: `/mnt/d/Atlassian-Products/Migration-auditor`. Branch: `feat/clone-access-ui`.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `webapp/store.py` | persistence | `clone_runs` table + CRUD; bump `SCHEMA_VERSION` |
| `webapp/clone_runner.py` | background runner + client builder + sync preview | Create |
| `webapp/clone_routes.py` | FastAPI router (page, preview, apply, status) | Create |
| `webapp/main.py` | app wiring | instantiate runner, `include_router` |
| `webapp/templates/clone.html` | the Clone access page | Create |
| `webapp/templates/clone_run.html` | run progress + results (polling) | Create |
| `webapp/templates/base.html` | nav | add "Clone access" link |
| `tests/test_clone_store_runner.py` | store + runner tests | Create |
| `tests/test_clone_routes.py` | route smoke tests | Create |

`clone_runs` columns: `id INTEGER PK, conn_id INTEGER, status TEXT, phase TEXT, params_json TEXT, report_json TEXT, log_json TEXT, created_at REAL, finished_at REAL`.

---

### Task 1: Store `clone_runs` + `CloneRunner` + client builder

**Files:**
- Modify: `webapp/store.py` (`_migrate` table add + bump `SCHEMA_VERSION`; CRUD methods near the run methods ~line 490)
- Create: `webapp/clone_runner.py`
- Test: `tests/test_clone_store_runner.py`

**Interfaces:**
- Produces (Store): `create_clone_run(conn_id:int, params:dict)->int`, `get_clone_run(run_id:int)->dict|None`, `list_clone_runs(limit:int=50)->list[dict]`, `update_clone_run(run_id:int, *, status=None, phase=None, report:dict|None=None, finished:bool=False)->None`, `append_clone_log(run_id:int, line:str)->None`.
- Produces (`webapp/clone_runner.py`): `build_clone_client(store, conn_id, http) -> tuple(client, connector, dict)` (raises `ValueError` if no such jira connection); `run_preview(store, conn_id, pairs, http) -> dict` (synchronous, groups-only); `class CloneRunner` with `__init__(self, store, http)` and `start(self, conn_id:int, pairs:list, *, dry_run:bool, scan_roles:bool) -> int` (spawns a daemon thread, returns the clone_run id).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_clone_store_runner.py`:

```python
import time
import httpx
from webapp.store import Store
from webapp import clone_runner as cr


def _store(tmp_path):
    return Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))


def test_clone_run_crud_roundtrip(tmp_path):
    s = _store(tmp_path)
    cid = s.create_saved_connection("acme", "jira", "cloud",
                                    "https://acme.atlassian.net",
                                    {"email": "e@x.y", "token": "tok"})
    rid = s.create_clone_run(cid, {"pairs": [["a", "b"]], "dry_run": True})
    row = s.get_clone_run(rid)
    assert row["status"] == "running" and row["conn_id"] == cid
    s.append_clone_log(rid, "hello")
    s.append_clone_log(rid, "world")
    s.update_clone_run(rid, phase="groups")
    row = s.get_clone_run(rid)
    import json
    assert json.loads(row["log_json"]) == ["hello", "world"]
    assert row["phase"] == "groups"
    s.update_clone_run(rid, status="done",
                       report={"summary": {"pairs": 1}}, finished=True)
    row = s.get_clone_run(rid)
    assert row["status"] == "done" and row["finished_at"]
    assert json.loads(row["report_json"])["summary"]["pairs"] == 1
    assert any(r["id"] == rid for r in s.list_clone_runs())


def test_runner_executes_and_persists_report(tmp_path, monkeypatch):
    s = _store(tmp_path)
    cid = s.create_saved_connection("acme", "jira", "cloud",
                                    "https://acme.atlassian.net",
                                    {"email": "e@x.y", "token": "tok"})

    # Stub the engine so no real client is needed; assert the runner wires
    # progress -> log and persists the returned report + status=done.
    def fake_run_clone(client, pairs, *, dry_run, scan_roles, progress=None):
        if progress:
            progress("scanning")
        return {"dry_run": dry_run, "scanned_roles": scan_roles, "pairs": [],
                "summary": {"pairs": 0, "blocked": 0, "groups_added": 0,
                            "roles_added": 0, "failed": 0, "partial": 0}}
    monkeypatch.setattr(cr, "run_clone", fake_run_clone)
    monkeypatch.setattr(cr, "build_clone_client",
                        lambda store, conn_id, http: (object(), object(), {"id": conn_id}))

    runner = cr.CloneRunner(s, http=None)
    rid = runner.start(cid, [("a", "b")], dry_run=True, scan_roles=False)
    # join the worker thread deterministically
    for _ in range(200):
        if s.get_clone_run(rid)["status"] in ("done", "failed"):
            break
        time.sleep(0.02)
    row = s.get_clone_run(rid)
    assert row["status"] == "done"
    import json
    assert "scanning" in json.loads(row["log_json"])
    assert json.loads(row["report_json"])["summary"]["pairs"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_clone_store_runner.py -v`
Expected: FAIL — `create_clone_run` / `webapp.clone_runner` missing.

- [ ] **Step 3: Implement store CRUD + runner**

In `webapp/store.py`: bump the version constant — change `SCHEMA_VERSION = 1` to `SCHEMA_VERSION = 2`. In `_migrate`, near the existing guarded `CREATE TABLE IF NOT EXISTS saved_connections (...)`, add:

```python
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS clone_runs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, conn_id INTEGER, "
            "status TEXT DEFAULT 'running', phase TEXT, params_json TEXT, "
            "report_json TEXT, log_json TEXT DEFAULT '[]', "
            "created_at REAL, finished_at REAL)")
```

Add these methods to `Store` (near `create_run`/`update_run`):

```python
    # ---------------------------------------------------------- clone runs
    def create_clone_run(self, conn_id: int, params: dict) -> int:
        return self._exec(
            "INSERT INTO clone_runs(conn_id,status,params_json,log_json,created_at)"
            " VALUES(?,?,?,?,?)",
            (conn_id, "running", json.dumps(params), "[]", time.time())).lastrowid

    def get_clone_run(self, run_id: int) -> dict | None:
        return self._row("SELECT * FROM clone_runs WHERE id=?", (run_id,))

    def list_clone_runs(self, limit: int = 50) -> list:
        return self._rows("SELECT * FROM clone_runs ORDER BY id DESC LIMIT ?",
                          (limit,))

    def update_clone_run(self, run_id: int, *, status=None, phase=None,
                         report: dict | None = None, finished: bool = False) -> None:
        sets, args = [], []
        if status is not None:
            sets.append("status=?"); args.append(status)
        if phase is not None:
            sets.append("phase=?"); args.append(phase)
        if report is not None:
            sets.append("report_json=?"); args.append(json.dumps(report, default=str))
        if finished or status in ("done", "failed"):
            sets.append("finished_at=?"); args.append(time.time())
        if sets:
            args.append(run_id)
            self._exec(f"UPDATE clone_runs SET {','.join(sets)} WHERE id=?", args)

    def append_clone_log(self, run_id: int, line: str) -> None:
        with self._txn() as db:
            row = db.execute("SELECT log_json FROM clone_runs WHERE id=?",
                             (run_id,)).fetchone()
            log = json.loads(row["log_json"]) if row and row["log_json"] else []
            log.append(line)
            db.execute("UPDATE clone_runs SET log_json=? WHERE id=?",
                       (json.dumps(log), run_id))
```

(If `Store` has no `_txn` context manager, use `self._exec` with a read-then-write; the project's `_txn` exists — mirror its use elsewhere in the file.)

Create `webapp/clone_runner.py`:

```python
"""Background runner + saved-connection client builder for user-access cloning.

Wraps auditor.cloneaccess.run_clone in a daemon thread, persisting progress to
clone_runs.log_json and the final report to report_json. A synchronous
run_preview is exposed for the read-only groups-only preview path.
"""
from __future__ import annotations

import threading

from auditor.client import Connection
from auditor.cloneaccess import run_clone, CloneError
from auditor.connectors import get_connector


def build_clone_client(store, conn_id: int, http):
    """Build a Jira client from a saved connection. Raises ValueError if the
    connection is missing or not a jira connection."""
    row = store.get_saved_connection(conn_id)
    if row is None or row["product"] != "jira":
        raise ValueError(f"no jira saved connection with id {conn_id}")
    secret = store.saved_connection_secret(row)
    conn = Connection(auth_type="pat", site_url=row["site_url"],
                      deployment=row["deployment"] or "cloud",
                      email=secret.get("email") or None,
                      api_token=secret.get("token"))
    connector = get_connector("jira")
    return connector.make_client(conn, http), connector, row


def run_preview(store, conn_id: int, pairs: list, http) -> dict:
    """Synchronous, read-only, groups-only preview (no writes, no role scan)."""
    client, _, _ = build_clone_client(store, conn_id, http)
    return run_clone(client, pairs, dry_run=True, scan_roles=False)


class CloneRunner:
    def __init__(self, store, http):
        self.store = store
        self.http = http

    def start(self, conn_id: int, pairs: list, *, dry_run: bool,
              scan_roles: bool) -> int:
        run_id = self.store.create_clone_run(
            conn_id, {"pairs": [list(p) for p in pairs],
                      "dry_run": dry_run, "scan_roles": scan_roles})
        t = threading.Thread(target=self._execute,
                             args=(run_id, conn_id, pairs, dry_run, scan_roles),
                             daemon=True, name=f"clone-{run_id}")
        t.start()
        return run_id

    def _execute(self, run_id, conn_id, pairs, dry_run, scan_roles):
        store = self.store
        try:
            client, _, _ = build_clone_client(store, conn_id, self.http)
        except ValueError as e:
            store.append_clone_log(run_id, f"error: {e}")
            store.update_clone_run(run_id, status="failed", finished=True)
            return

        def progress(msg):
            store.append_clone_log(run_id, msg)

        store.update_clone_run(run_id, phase="groups")
        try:
            report = run_clone(client, pairs, dry_run=dry_run,
                               scan_roles=scan_roles, progress=progress)
        except CloneError as e:
            partial = getattr(e, "partial", None)
            store.append_clone_log(run_id, f"aborted: {e}")
            store.update_clone_run(run_id, status="failed", report=partial,
                                   finished=True)
            return
        store.update_clone_run(run_id, status="done", phase="finalize",
                               report=report, finished=True)
        store.append_clone_log(run_id, "done")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_clone_store_runner.py -v && pytest -q`
Expected: PASS (new tests + full suite; the `SCHEMA_VERSION` bump must not break existing store tests).

- [ ] **Step 5: Commit**

```bash
cd /mnt/d/Atlassian-Products/Migration-auditor
git add webapp/store.py webapp/clone_runner.py tests/test_clone_store_runner.py
git commit -m "feat(clone-ui): clone_runs persistence + background CloneRunner + client builder"
```

---

### Task 2: Router + app wiring + nav + smoke tests

**Files:**
- Create: `webapp/clone_routes.py`
- Modify: `webapp/main.py` (instantiate `CloneRunner`, `include_router`)
- Modify: `webapp/templates/base.html` (nav link)
- Test: `tests/test_clone_routes.py`

**Interfaces:**
- Consumes: `Store` clone methods + `webapp.clone_runner` (`CloneRunner`, `run_preview`, `build_clone_client`) from Task 1; `webapp.main.parse_pairs_csv` (Phase 1) is NOT reused here (form/CSV parsing is inline — see below); `auditor.cloneaccess.run_clone`.
- Produces: `make_clone_router(store, runner, http_getter, templates) -> APIRouter`. Routes: `GET /clone`, `POST /clone/preview`, `POST /clone/apply`, `GET /clone/runs/{id}`, `GET /clone/runs/{id}/status`.

- [ ] **Step 1: Write the failing smoke tests**

Create `tests/test_clone_routes.py`:

```python
import httpx
from fastapi.testclient import TestClient
from webapp.main import create_app
from webapp.config import Config


def _client(tmp_path, handler):
    cfg = Config(data_dir=str(tmp_path), bind_host="127.0.0.1", bind_port=8484,
                 public_base_url="http://localhost:8484", secret_key=None)
    app = create_app(cfg)
    # inject a mock transport so any Jira call the preview makes is faked
    app.state.http = httpx.Client(transport=httpx.MockTransport(handler))
    return TestClient(app), app


def _ok_handler(req):
    p = str(req.url.path)
    if p.endswith("/user/groups"):
        aid = req.url.params.get("accountId")
        return httpx.Response(200, json=[{"name": "g1", "groupId": "gid1"}]
                              if aid == "main-id" else [])
    if p.endswith("/user/search"):
        q = req.url.params.get("query")
        return httpx.Response(200, json=[{"accountId": q.split("@")[0] + "-id",
            "accountType": "atlassian", "active": True, "emailAddress": q}])
    return httpx.Response(200, json={})


def test_clone_page_renders(tmp_path):
    client, app = _client(tmp_path, _ok_handler)
    r = client.get("/clone")
    assert r.status_code == 200 and "Clone access" in r.text


def test_clone_preview_renders_plan(tmp_path):
    client, app = _client(tmp_path, _ok_handler)
    cid = app.state.store.create_saved_connection(
        "acme", "jira", "cloud", "https://acme.atlassian.net",
        {"email": "e@x.y", "token": "tok"})
    r = client.post("/clone/preview", data={"conn_id": str(cid),
                    "main": "main@x.y", "clone": "clone@x.y"})
    assert r.status_code == 200
    assert "main@x.y" in r.text and "g1" in r.text     # the group to add
```

(Note: `app.state.store` must be reachable — if `create_app` does not stash the store on `app.state`, the test should build the `Store` directly from `cfg.db_path`/`cfg.key_path` instead. The implementer picks whichever the app exposes.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_clone_routes.py -v`
Expected: FAIL — `/clone` 404 (router not wired).

- [ ] **Step 3: Implement the router + wire it**

Create `webapp/clone_routes.py`:

```python
"""Clone-access web routes: page, read-only preview, background apply, status."""
from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from webapp.clone_runner import run_preview


def _parse_form_pairs(main, clone, upload_bytes) -> list:
    """Pairs from a single main/clone OR an uploaded main,clone CSV."""
    if upload_bytes:
        text = upload_bytes.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        cols = {(c or "").strip().lower(): c for c in (reader.fieldnames or [])}
        if "main" not in cols or "clone" not in cols:
            raise ValueError("CSV must have 'main' and 'clone' columns")
        out = []
        for row in reader:
            m = (row.get(cols["main"]) or "").strip()
            c = (row.get(cols["clone"]) or "").strip()
            if m or c:
                out.append((m, c))
        return out
    if main and clone:
        return [(main.strip(), clone.strip())]
    raise ValueError("provide a main and clone, or upload a CSV")


def make_clone_router(store, runner, http_getter, templates) -> APIRouter:
    router = APIRouter()

    def _render(request, **ctx):
        base = {"connections": store.list_saved_connections("jira"),
                "runs": store.list_clone_runs(20), "active_nav": "clone"}
        base.update(ctx)
        return templates.TemplateResponse(request, "clone.html", base)

    @router.get("/clone", response_class=HTMLResponse)
    def clone_page(request: Request):
        return _render(request)

    @router.post("/clone/preview", response_class=HTMLResponse)
    async def clone_preview(request: Request, conn_id: int = Form(...),
                            main: str = Form(""), clone: str = Form(""),
                            csv_file: UploadFile | None = File(None)):
        data = await csv_file.read() if csv_file is not None else b""
        try:
            pairs = _parse_form_pairs(main, clone, data)
            report = run_preview(store, conn_id, pairs, http_getter())
        except ValueError as e:
            return _render(request, error=str(e), sel_conn=conn_id)
        return _render(request, report=report, sel_conn=conn_id, is_preview=True)

    @router.post("/clone/apply")
    async def clone_apply(request: Request, conn_id: int = Form(...),
                          main: str = Form(""), clone: str = Form(""),
                          dry_run: str = Form(""),
                          csv_file: UploadFile | None = File(None)):
        data = await csv_file.read() if csv_file is not None else b""
        try:
            pairs = _parse_form_pairs(main, clone, data)
        except ValueError as e:
            return _render(request, error=str(e), sel_conn=conn_id)
        is_dry = bool(dry_run)
        run_id = runner.start(conn_id, pairs, dry_run=is_dry, scan_roles=True)
        return RedirectResponse(f"/clone/runs/{run_id}", status_code=303)

    @router.get("/clone/runs/{run_id}", response_class=HTMLResponse)
    def clone_run_page(request: Request, run_id: int):
        row = store.get_clone_run(run_id)
        if row is None:
            return RedirectResponse("/clone", status_code=303)
        report = json.loads(row["report_json"]) if row["report_json"] else None
        log = json.loads(row["log_json"]) if row["log_json"] else []
        return templates.TemplateResponse(request, "clone_run.html",
            {"request": request, "run": row, "report": report, "log": log,
             "active_nav": "clone"})

    @router.get("/clone/runs/{run_id}/status")
    def clone_run_status(run_id: int):
        row = store.get_clone_run(run_id)
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({
            "status": row["status"], "phase": row["phase"],
            "log": json.loads(row["log_json"]) if row["log_json"] else [],
            "report": json.loads(row["report_json"]) if row["report_json"] else None})

    return router
```

In `webapp/main.py` `create_app`, after the other routers are included and `app.state.http` is set, instantiate the runner and include the router (the `templates` Jinja env already exists in `main.py` as `templates`; `http_getter` returns the live shared client):

```python
    from webapp.clone_routes import make_clone_router
    from webapp.clone_runner import CloneRunner
    _clone_runner = CloneRunner(store, app.state.http)
    app.include_router(make_clone_router(
        store, _clone_runner, lambda: app.state.http, templates))
```

In `webapp/templates/base.html`, add a nav link next to Connections:

```html
      <a href="/clone" {% if active_nav == 'clone' or request.url.path.startswith('/clone') %}class="on"{% endif %}>Clone access</a>
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_clone_routes.py -v && pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /mnt/d/Atlassian-Products/Migration-auditor
git add webapp/clone_routes.py webapp/main.py webapp/templates/base.html tests/test_clone_routes.py
git commit -m "feat(clone-ui): clone-access router (page, preview, apply, status) + nav"
```

---

### Task 3: Templates — `clone.html` + `clone_run.html`

**Files:**
- Create: `webapp/templates/clone.html`, `webapp/templates/clone_run.html`
- Verify: render screenshots via the running server.

**Interfaces:**
- Consumes: `make_clone_router` context — `connections`, `runs`, `report` (preview), `sel_conn`, `is_preview`, `error` (clone.html); `run`, `report`, `log` (clone_run.html).

- [ ] **Step 1: Create `clone.html`**

Extend `base.html` (match the structure of `connections.html`). It must contain the literal text "Clone access" (the route test asserts it), and render `report` (preview) when present. Structure:

```html
{% extends "base.html" %}
{% block body %}
<h1 class="h1">Clone access</h1>
<p class="sub">Additively clone a user's groups &amp; project roles onto another account. Preview is read-only; Apply writes.</p>
{% if error %}<div class="flash error">{{ error }}</div>{% endif %}

<form class="card" method="post" enctype="multipart/form-data" action="/clone/preview">
  <label>Instance (saved Jira connection)
    <select name="conn_id" required>
      {% for c in connections %}
      <option value="{{ c.id }}" {% if sel_conn == c.id %}selected{% endif %}>{{ c.name }} — {{ c.site_url }}</option>
      {% endfor %}
    </select>
  </label>
  <div class="row">
    <label>Main account (accountId or email)<input name="main" placeholder="admin@acme-source.example"></label>
    <label>Clone account (accountId or email)<input name="clone" placeholder="admin@acme-target.example"></label>
  </div>
  <label>…or upload a CSV with <code>main,clone</code> columns<input type="file" name="csv_file" accept=".csv"></label>
  <label class="check"><input type="checkbox" name="dry_run" value="1"> Dry-run apply (scan roles, write nothing)</label>
  <div class="actions">
    <button type="submit">Preview (no writes)</button>
    <button type="submit" formaction="/clone/apply" class="primary">Apply</button>
  </div>
</form>

{% if report %}
<div class="card">
  <h2>{{ 'Preview' if is_preview else 'Result' }} — {{ report.summary.pairs }} pair(s),
      +{{ report.summary.groups_added }} groups, +{{ report.summary.roles_added }} roles,
      {{ report.summary.blocked }} blocked, {{ report.summary.failed }} failed
      {% if not report.scanned_roles %}<span class="muted">(roles not scanned — preview)</span>{% endif %}</h2>
  <table>
    <thead><tr><th>Main</th><th>Clone</th><th>Status</th><th>Groups +</th><th>Roles +</th><th>Note</th></tr></thead>
    <tbody>
    {% for p in report.pairs %}
      <tr>
        <td class="mono">{{ p.main }}</td><td class="mono">{{ p.clone }}</td>
        <td><span class="badge {{ p.status }}">{{ p.status }}</span></td>
        <td>{{ p.groups.added|join(', ') }}</td>
        <td>{{ p.roles.added|join(', ') }}</td>
        <td class="sm">{{ p.reason or '' }}{% for f in p.groups.failed + p.roles.failed %} FAIL {{ f }}{% endfor %}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
</div>
{% endif %}

<div class="card">
  <h2>Recent clone runs</h2>
  <ul class="runs">
    {% for r in runs %}<li><a href="/clone/runs/{{ r.id }}">#{{ r.id }}</a> — {{ r.status }} {{ r.phase or '' }}</li>{% endfor %}
  </ul>
</div>
{% endblock %}
```

If the app's `base.html` uses a block name other than `body`, match it (inspect `connections.html` for the exact `{% block %}` name). Reuse existing CSS classes; add minimal styles to `webapp/static/app.css` only if a needed class is absent.

- [ ] **Step 2: Create `clone_run.html` (polling)**

```html
{% extends "base.html" %}
{% block body %}
<h1 class="h1">Clone run #{{ run.id }}</h1>
<p class="sub">Status: <b id="st">{{ run.status }}</b> · phase <span id="ph">{{ run.phase or '' }}</span>
   · <a href="/clone">← back</a></p>

<div class="card"><h2>Progress</h2><pre id="log">{{ log|join('\n') }}</pre></div>
<div class="card" id="resultwrap" {% if not report %}style="display:none"{% endif %}>
  <h2>Results</h2><div id="result"></div>
</div>

<script>
const RID = {{ run.id }};
function render(rep){
  if(!rep) return;
  document.getElementById('resultwrap').style.display='';
  const s = rep.summary;
  let h = `<p>${s.pairs} pair(s) · +${s.groups_added} groups · +${s.roles_added} roles · ${s.blocked} blocked · ${s.failed} failed</p>`;
  h += '<table><thead><tr><th>Main</th><th>Clone</th><th>Status</th><th>Groups+</th><th>Roles+</th><th>Note</th></tr></thead><tbody>';
  for(const p of rep.pairs){
    const note = (p.reason||'') + [...p.groups.failed,...p.roles.failed].map(f=>' FAIL '+JSON.stringify(f)).join('');
    h += `<tr><td class=mono>${p.main}</td><td class=mono>${p.clone}</td><td><span class="badge ${p.status}">${p.status}</span></td>`+
         `<td>${p.groups.added.join(', ')}</td><td>${p.roles.added.join(', ')}</td><td class=sm>${note}</td></tr>`;
  }
  document.getElementById('result').innerHTML = h + '</tbody></table>';
}
render({{ report|tojson }});
async function poll(){
  const r = await fetch(`/clone/runs/${RID}/status`); const d = await r.json();
  document.getElementById('st').textContent = d.status;
  document.getElementById('ph').textContent = d.phase || '';
  document.getElementById('log').textContent = (d.log||[]).join('\n');
  if(d.report) render(d.report);
  if(d.status === 'running') setTimeout(poll, 1500);
}
if('{{ run.status }}' === 'running') poll();
{% endblock %}
```

- [ ] **Step 3: Verify rendering (manual smoke via the running server)**

Run the server and load the page; confirm it renders (the route test in Task 2 already asserts 200 + "Clone access"; this step is a visual check). Capture a screenshot of `/clone`.

- [ ] **Step 4: Run the suite**

Run: `pytest -q`
Expected: PASS (templates don't break the route tests).

- [ ] **Step 5: Commit**

```bash
cd /mnt/d/Atlassian-Products/Migration-auditor
git add webapp/templates/clone.html webapp/templates/clone_run.html
git commit -m "feat(clone-ui): clone-access page + run/results templates"
```

---

## Verification (after all tasks)
- [ ] `pytest -q` green.
- [ ] Server smoke: start the app, open `/clone`, run a preview against a saved connection, confirm the plan renders; start an apply (dry-run) and watch the run page poll to completion.
- [ ] README: extend the "Clone user access" section with a line noting the web page at `/clone`.

## Self-Review
- **Spec coverage (Phase-2):** connection picker + single-pair + CSV → Task 2/3; read-only preview → preview route (groups-only) → Task 2; background apply with live progress + results → CloneRunner + run page polling → Task 1/2/3; reuse engine unchanged → all tasks call `run_clone`; persistence → `clone_runs` → Task 1.
- **Placeholder scan:** template block-name and `app.state.store` exposure are the two "match what exists" notes — flagged explicitly for the implementer, not placeholders.
- **Type consistency:** `CloneRunner(store, http).start(conn_id, pairs, *, dry_run, scan_roles)`, `run_preview(store, conn_id, pairs, http)`, `build_clone_client(store, conn_id, http)`, and the `clone_runs` columns are used identically across Tasks 1–3. The `report` shape is the Phase-1 engine's.

## Execution Handoff
Subagent-driven (continuing the same flow).
