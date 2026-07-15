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

> NOTE from Task 3 review: Store serializes ALL statements behind one RLock — engine writes and SSE/API reads are safe to interleave, but hold no Store call open across long work.

## Post-review amendments (applied)

- **Echo clamped page**: `/issues` handler now computes `page = max(1, page)` once at the top of the handler body, so the echoed `"page"` in the response reflects the actual clamped value (e.g. a client sending `page=0` receives `"page": 1` back).
- **Tolerate malformed detail_json (never 500 a findings page)**: Replaced inline `json.loads(row.pop("detail_json") or "{}")` in both `/issues` and `/config` handlers with a module-level `_detail(row)` helper that catches `ValueError`/`TypeError` and returns `{"_unparseable": str(raw)[:200]}` instead of propagating a 500. A single corrupt DB row can no longer break a 40k-finding page.
- **6 gap tests added** to `tests/test_analysis.py`:
  - `test_issues_size_capped_at_200` — size=500 is clamped to 200 in the echoed response
  - `test_issues_page_zero_echoes_one` — page=0 echoes `"page": 1` (Fix 1 regression guard)
  - `test_issues_page_beyond_end_is_empty_not_error` — page=999 returns empty rows, not an error
  - `test_all_endpoints_404_on_unknown_run` — all 6 sub-paths return 404 on run 424242
  - `test_summary_empty_stats_json_graceful` — run with default `stats_json='{}'` returns empty headlines/project_stats/areas
  - `test_malformed_detail_json_does_not_500` — corrupt `detail_json` in DB returns `{"_unparseable": ...}` not 500

