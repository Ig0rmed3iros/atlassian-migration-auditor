"""Run-over-run diff (audit -> fix -> re-audit: what changed)."""
from __future__ import annotations

import copy

import httpx
from fastapi.testclient import TestClient

from webapp.config import Config
from webapp.main import create_app
from webapp.compare import compare_runs, candidate_base_runs


def _app(tmp_path):
    cfg = Config(data_dir=str(tmp_path), bind_host="127.0.0.1", bind_port=8488,
                 public_base_url="http://localhost:8488", secret_key=None)
    return create_app(cfg, http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))))


def _f(kind, name, area="schemes", severity="low"):
    from auditor.envaudit.fixes import _FIXES, category_for
    fix = copy.copy(_FIXES.get(kind, {"tier": "human", "title": kind}))
    return {"area": area, "name": name, "kind": kind, "severity": severity,
            "detail": {"fix": fix, "category": category_for(kind),
                       "severity": severity}}


def _env_run(store, mid, findings):
    run = store.create_run(mid, {}, kind="env_audit")
    store.insert_findings_config(run, findings)
    store.update_run(run, status="done", verdict="NEEDS_ATTENTION", stats={})
    return run


def test_compare_runs_new_resolved_unchanged(tmp_path):
    app = _app(tmp_path)
    store = app.state.store
    mid = store.create_migration("Env", audit_type="environment")
    base = _env_run(store, mid, [_f("scheme_unused", "Old"),
                                 _f("empty_group", "g1", "groups"),
                                 _f("orphaned_pages", "DOCS", "spaces", "high")])
    new = _env_run(store, mid, [_f("empty_group", "g1", "groups"),
                                _f("orphaned_pages", "DOCS", "spaces", "high"),
                                _f("unused_custom_field", "F1", "fields")])
    d = compare_runs(store, base, new)
    assert d["audit_type"] == "env"
    assert {r["kind"] for r in d["new"]} == {"unused_custom_field"}
    assert {r["kind"] for r in d["resolved"]} == {"scheme_unused"}
    assert d["unchanged_count"] == 2
    assert d["base_count"] == 3 and d["new_count"] == 3


def test_compare_runs_incompatible_types_is_none(tmp_path):
    app = _app(tmp_path)
    store = app.state.store
    em = store.create_migration("Env", audit_type="environment")
    mm = store.create_migration("Mig", audit_type="migration")
    env = _env_run(store, em, [_f("scheme_unused", "X")])
    migr = store.create_run(mm, {}, kind="audit")
    store.update_run(migr, status="done")
    assert compare_runs(store, env, migr) is None       # env vs migration
    assert compare_runs(store, 999, env) is None          # missing base


def test_candidate_base_runs_lists_prior_same_kind_done(tmp_path):
    app = _app(tmp_path)
    store = app.state.store
    mid = store.create_migration("Env", audit_type="environment")
    r1 = _env_run(store, mid, [_f("scheme_unused", "A")])
    r2 = _env_run(store, mid, [_f("scheme_unused", "B")])
    cands = candidate_base_runs(store, store.get_run(r2))
    assert [c["id"] for c in cands] == [r1]               # only the prior run


def test_diff_route_renders_picker_and_delta(tmp_path):
    app = _app(tmp_path)
    store = app.state.store
    mid = store.create_migration("Env", audit_type="environment")
    base = _env_run(store, mid, [_f("scheme_unused", "Old"),
                                 _f("empty_group", "g1", "groups")])
    new = _env_run(store, mid, [_f("empty_group", "g1", "groups"),
                                _f("unused_custom_field", "F1", "fields")])
    c = TestClient(app)
    assert "Compare" in c.get(f"/runs/{new}/diff").text
    t = c.get(f"/runs/{new}/diff?base={base}").text
    assert "unused_custom_field" in t      # new
    assert "scheme_unused" in t            # resolved
    assert "unchanged" in t


def test_analysis_page_links_to_diff(tmp_path):
    app = _app(tmp_path)
    store = app.state.store
    mid = store.create_migration("Env", audit_type="environment")
    run = _env_run(store, mid, [_f("scheme_unused", "X")])
    assert f"/runs/{run}/diff" in TestClient(app).get(f"/runs/{run}/analysis").text
