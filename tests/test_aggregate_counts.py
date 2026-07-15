"""Tests for Bug 1 fix: aggregate fidelity counts from SQL (no full row scan).

Verifies:
1. store.issue_finding_counts returns correct aggregated data
2. derive_fidelity_from_counts produces the same results as derive_fidelity
   (on a small fixture where both can be computed)
3. The summary route does NOT call all_issue_findings for migration runs
   (proves the full scan is avoided)
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auditor.aggregate import (
    SYS_ABS_FLOOR, SYS_FRAC, derive_fidelity, derive_fidelity_from_counts,
)
from webapp.analysis import make_router
from webapp.store import Store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _prow(key, common, mismatched):
    raw = round(100.0 * (common - mismatched) / common, 2) if common else None
    return {"key": key, "common": common, "issues_with_mismatches": mismatched,
            "fidelity_pct": raw}


def _fm(project, key, field, src, tgt, kind="field_mismatch"):
    return {"project": project, "kind": kind, "src_key": key, "tgt_key": key,
            "field": field, "summary": f"{field} differs",
            "detail": {"src": src, "tgt": tgt, "sev": "med"}}


N = SYS_ABS_FLOOR + 50        # 250 — clears the absolute floor
COMMON = int(N / SYS_FRAC) - 50  # ~783


# ---------------------------------------------------------------------------
# Store.issue_finding_counts tests
# ---------------------------------------------------------------------------

def test_store_issue_finding_counts_returns_field_aggregates(tmp_path):
    """issue_finding_counts returns per-(project, kind, field) aggregates in
    field_agg with correct affected_issues and empty_issues counts, plus
    scalar holes and src_tails."""
    store = Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})

    findings = []
    # 10 issues with environment empty on target (systematic candidate)
    for i in range(10):
        findings.append({
            "project": "AC", "kind": "field_mismatch",
            "src_key": f"AC-{i}", "tgt_key": f"AC-{i}",
            "field": "environment", "summary": "env differs",
            "detail": {"src": "prod", "tgt": None}})
    # 3 issues with status non-empty on target
    for i in range(3):
        findings.append({
            "project": "AC", "kind": "field_mismatch",
            "src_key": f"AC-{100 + i}", "tgt_key": f"AC-{100 + i}",
            "field": "status", "summary": "status differs",
            "detail": {"src": "Open", "tgt": "Done"}})
    # 2 issues missing in target (holes)
    for i in range(2):
        findings.append({
            "project": "AC", "kind": "missing_in_tgt",
            "src_key": f"AC-{200 + i}", "tgt_key": None,
            "field": None, "summary": "missing", "detail": {}})
    store.insert_findings_issue(rid, findings)

    counts = store.issue_finding_counts(rid)

    # holes scalar
    assert counts["holes"] == 2
    assert counts["src_tails"] == 0

    field_agg = counts["field_agg"]

    # environment row: 10 affected, 10 empty
    env_rows = [r for r in field_agg
                if r["kind"] == "field_mismatch" and r["field"] == "environment"]
    assert len(env_rows) == 1
    assert env_rows[0]["affected_issues"] == 10
    assert env_rows[0]["empty_issues"] == 10

    # status row: 3 affected, 0 empty (tgt="Done" is non-empty)
    st_rows = [r for r in field_agg
               if r["kind"] == "field_mismatch" and r["field"] == "status"]
    assert len(st_rows) == 1
    assert st_rows[0]["affected_issues"] == 3
    assert st_rows[0]["empty_issues"] == 0


def test_store_issue_finding_counts_deduplicates_same_issue_multiple_findings(tmp_path):
    """If the same issue appears twice for the same (project, kind, field),
    affected_issues must count it once, not twice."""
    store = Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})

    # Same issue AC-1 appears twice with the same field
    findings = [
        {"project": "AC", "kind": "field_mismatch",
         "src_key": "AC-1", "tgt_key": "AC-1", "field": "priority",
         "summary": "x", "detail": {"src": "High", "tgt": None}},
        {"project": "AC", "kind": "field_mismatch",
         "src_key": "AC-1", "tgt_key": "AC-1", "field": "priority",
         "summary": "y", "detail": {"src": "High", "tgt": None}},
    ]
    store.insert_findings_issue(rid, findings)
    counts = store.issue_finding_counts(rid)
    field_agg = counts["field_agg"]
    pr_rows = [r for r in field_agg
               if r["kind"] == "field_mismatch" and r["field"] == "priority"]
    assert len(pr_rows) == 1
    # AC-1 counted once, not twice
    assert pr_rows[0]["affected_issues"] == 1
    assert pr_rows[0]["empty_issues"] == 1


def test_store_issue_finding_counts_source_tails_only(tmp_path):
    """Source-direction tails are counted as src_tails; target-direction tails are NOT."""
    store = Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    findings = [
        # source tail (direction=source)
        {"project": "AC", "kind": "tail_post_cutover",
         "src_key": "AC-1", "tgt_key": None, "field": None,
         "summary": "src tail", "detail": {"direction": "source"}},
        # target tail — must NOT count
        {"project": "AC", "kind": "tail_post_cutover",
         "src_key": None, "tgt_key": "AC-2", "field": None,
         "summary": "tgt tail", "detail": {"direction": "target"}},
        # legacy tail with src_key (no direction) — counts as source tail
        {"project": "AC", "kind": "tail_post_cutover",
         "src_key": "AC-3", "tgt_key": None, "field": None,
         "summary": "legacy tail", "detail": {}},
    ]
    store.insert_findings_issue(rid, findings)
    counts = store.issue_finding_counts(rid)
    # src_tails: AC-1 (direction=source) + AC-3 (legacy, has src_key) = 2
    assert counts["src_tails"] == 2


# ---------------------------------------------------------------------------
# derive_fidelity_from_counts equivalence tests
# ---------------------------------------------------------------------------

def _build_store_with_findings(tmp_path, findings, stats):
    """Helper: build a Store+run with given findings and stats."""
    store = Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    store.insert_findings_issue(rid, findings)
    store.update_run(rid, status="done", verdict="GAPS_FOUND", stats=stats)
    return store, rid


def test_derive_fidelity_from_counts_matches_no_gaps(tmp_path):
    """With no systematic gaps, from_counts output matches the original."""
    findings = [_fm("AC", f"AC-{i}", "status", "Open", "Done")
                for i in range(5)]
    common = 100
    project_rows = [_prow("AC", common, 5)]
    # via original
    old = derive_fidelity(project_rows, findings)

    store, rid = _build_store_with_findings(tmp_path, findings, {
        "project_stats": {"AC": {"common": common, "fidelity_pct": 95.0,
                                  "issues_with_mismatches": 5}},
        "issues_src_total": common,
    })
    counts = store.issue_finding_counts(rid)
    new = derive_fidelity_from_counts(project_rows, counts,
                                      _store=store, _run_id=rid)

    assert new["overall"]["fidelity_core"] == old["overall"]["fidelity_core"]
    assert new["overall"]["fidelity_raw"] == old["overall"]["fidelity_raw"]
    assert (new["overall"]["core_mismatched_total"]
            == old["overall"]["core_mismatched_total"])
    assert new["systematic_gaps"] == old["systematic_gaps"]


def test_derive_fidelity_from_counts_matches_with_systematic_gap(tmp_path):
    """With a systematic gap, from_counts produces the same gap detection and
    core/raw fidelity as the original row-by-row path."""
    findings = []
    n_gap = N           # 250 issues with environment empty
    for i in range(n_gap):
        findings.append(_fm("AC", f"AC-{i}", "environment", "prod", None))
    # one real mismatch
    findings.append(_fm("AC", "AC-999", "status", "Open", "Done"))

    mismatched = n_gap + 1
    project_rows = [_prow("AC", COMMON, mismatched)]
    old = derive_fidelity(project_rows, findings, audited=COMMON)

    store, rid = _build_store_with_findings(tmp_path, findings, {
        "project_stats": {"AC": {"common": COMMON, "fidelity_pct": None,
                                  "issues_with_mismatches": mismatched}},
        "issues_src_total": COMMON,
    })
    counts = store.issue_finding_counts(rid)
    new = derive_fidelity_from_counts(project_rows, counts, audited=COMMON,
                                      _store=store, _run_id=rid)

    # Gap detection matches
    assert len(new["systematic_gaps"]) == len(old["systematic_gaps"]) == 1
    old_gap = old["systematic_gaps"][0]
    new_gap = new["systematic_gaps"][0]
    assert new_gap["project"] == old_gap["project"] == "AC"
    assert new_gap["field"] == old_gap["field"] == "environment"
    assert new_gap["affected_issues"] == old_gap["affected_issues"] == n_gap

    # Core fidelity matches
    old_pp = {p["key"]: p for p in old["per_project"]}["AC"]
    new_pp = {p["key"]: p for p in new["per_project"]}["AC"]
    assert new_pp["core_mismatched"] == old_pp["core_mismatched"] == 1
    assert new_pp["fidelity_core"] == old_pp["fidelity_core"]

    # Overall matches
    assert new["overall"]["fidelity_core"] == old["overall"]["fidelity_core"]
    assert new["overall"]["fidelity_raw"] == old["overall"]["fidelity_raw"]


def test_derive_fidelity_from_counts_holes_and_tails(tmp_path):
    """Holes and source-direction tails are correctly counted."""
    findings = [
        {"project": "AC", "kind": "missing_in_tgt",
         "src_key": "AC-1", "tgt_key": None, "field": None,
         "summary": "x", "detail": {}},
        {"project": "AC", "kind": "tail_post_cutover",
         "src_key": "AC-2", "tgt_key": None, "field": None,
         "summary": "x", "detail": {"direction": "source"}},
        # target tail - must NOT subtract
        {"project": "AC", "kind": "tail_post_cutover",
         "src_key": None, "tgt_key": "AC-99", "field": None,
         "summary": "x", "detail": {"direction": "target"}},
    ]
    project_rows = [{"key": "AC", "common": 10, "fidelity_pct": 100.0,
                     "issues_with_mismatches": 0}]
    old = derive_fidelity(project_rows, findings, audited=10)

    store, rid = _build_store_with_findings(tmp_path, findings, {
        "project_stats": {"AC": {"common": 10, "fidelity_pct": 100.0,
                                  "issues_with_mismatches": 0}},
        "issues_src_total": 10,
    })
    counts = store.issue_finding_counts(rid)
    new = derive_fidelity_from_counts(project_rows, counts, audited=10,
                                      _store=store, _run_id=rid)

    assert new["overall"]["fidelity_core"] == old["overall"]["fidelity_core"]
    assert new["overall"]["fidelity_raw"] == old["overall"]["fidelity_raw"]


# ---------------------------------------------------------------------------
# Summary route does NOT call all_issue_findings
# ---------------------------------------------------------------------------

def test_summary_uses_aggregate_not_all_findings(tmp_path):
    """The summary endpoint must use the aggregate path (issue_finding_counts),
    NOT the full-scan all_issue_findings, for migration runs."""
    store = Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    store.insert_findings_issue(rid, [
        {"project": "AC", "kind": "field_mismatch",
         "src_key": "AC-1", "tgt_key": "AC-1", "field": "status",
         "summary": "x", "detail": {"src": "Open", "tgt": "Done"}}])
    store.update_run(rid, status="done", verdict="CLEAN", stats={
        "project_stats": {"AC": {"common": 10, "fidelity_pct": 90.0,
                                  "issues_with_mismatches": 1}},
        "issues_src_total": 10, "headlines": [], "areas": {}})

    # Spy on all_issue_findings — the summary must NOT call it
    original_all = store.all_issue_findings
    call_log = []

    def spy_all(*args, **kwargs):
        call_log.append(("all_issue_findings", args, kwargs))
        return original_all(*args, **kwargs)

    store.all_issue_findings = spy_all

    app = FastAPI()
    app.state.store = store
    app.include_router(make_router())
    resp = TestClient(app).get(f"/api/runs/{rid}/summary")
    assert resp.status_code == 200
    # The summary must NOT have called all_issue_findings
    assert call_log == [], (
        f"summary() called all_issue_findings {len(call_log)} time(s) — "
        "it must use the aggregate path instead")


def test_projects_route_uses_aggregate_not_all_findings(tmp_path):
    """The /projects list route must also use the aggregate path."""
    store = Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    store.insert_findings_issue(rid, [
        {"project": "AC", "kind": "field_mismatch",
         "src_key": "AC-1", "tgt_key": "AC-1", "field": "status",
         "summary": "x", "detail": {"src": "Open", "tgt": "Done"}}])
    store.set_run_projects(rid, [{"key": "AC", "name": "AC", "src_count": 10,
                                   "tgt_count": 10, "missing": 0, "tail_count": 0,
                                   "fidelity_pct": 90.0, "blind_spot": 0,
                                   "status": "compared"}])
    store.update_run(rid, status="done", verdict="CLEAN", stats={
        "project_stats": {"AC": {"common": 10, "fidelity_pct": 90.0,
                                  "issues_with_mismatches": 1}},
        "issues_src_total": 10, "headlines": [], "areas": {}})

    call_log = []
    original_all = store.all_issue_findings

    def spy_all(*args, **kwargs):
        call_log.append(("all_issue_findings", args, kwargs))
        return original_all(*args, **kwargs)

    store.all_issue_findings = spy_all

    app = FastAPI()
    app.state.store = store
    app.include_router(make_router())
    resp = TestClient(app).get(f"/api/runs/{rid}/projects")
    assert resp.status_code == 200
    assert call_log == [], (
        f"projects() called all_issue_findings {len(call_log)} time(s) — "
        "it must use the aggregate path instead")


def test_summary_uses_cached_derived_fidelity_no_aggregate(tmp_path):
    """When stats carries a precomputed `derived_fidelity` (set at finalize for
    large runs), the summary route serves it O(1) and must NOT re-aggregate
    via issue_finding_counts."""
    store = Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    store.insert_findings_issue(rid, [
        {"project": "AC", "kind": "field_mismatch", "src_key": "AC-1",
         "tgt_key": "AC-1", "field": "status",
         "summary": "x", "detail": {"src": "Open", "tgt": "Done"}}])
    cached = {
        "overall": {"fidelity_core": 95.0, "fidelity_raw": 90.0,
                    "core_mismatched_total": 1},
        "per_project": [{"key": "AC", "fidelity_core": 95.0,
                         "fidelity_raw": 90.0, "core_mismatched": 1}],
        "systematic_gaps": []}
    store.update_run(rid, status="done", verdict="CLEAN", stats={
        "project_stats": {"AC": {"common": 10, "fidelity_pct": 90.0,
                                 "issues_with_mismatches": 1}},
        "issues_src_total": 10, "headlines": [], "areas": {},
        "derived_fidelity": cached})

    original = store.issue_finding_counts
    call_log = []
    store.issue_finding_counts = lambda *a, **k: (call_log.append(1)
                                                  or original(*a, **k))
    app = FastAPI()
    app.state.store = store
    app.include_router(make_router())
    resp = TestClient(app).get(f"/api/runs/{rid}/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fidelity"]["core"] == 95.0           # served from cache
    assert call_log == [], "summary re-aggregated despite a cached fidelity"
