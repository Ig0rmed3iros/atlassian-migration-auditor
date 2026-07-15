"""Tests for Bug 2 fix: user_gap findings must NOT pollute per-project distribution.

Verifies:
1. stage_usergap sets project="" (empty sentinel) on user_gap findings, not username
2. The username survives in detail so guidance._user_gap still lists users
3. user_gap findings with project="" do not appear as phantom projects in
   the distribution (query_issues project filter / kind counts)
"""
from __future__ import annotations

import gzip
import json
import os

import httpx
import pytest

from auditor.client import Connection, JiraClient
from auditor.remediation.usergap import detect_user_gaps
from auditor.remediation.guidance import guidance_for
from webapp.stages import stage_usergap
from webapp.store import Store


# ---------------------------------------------------------------------------
# detect_user_gaps output shape
# ---------------------------------------------------------------------------

def _write_gz(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt") as fh:
        fh.write(json.dumps({"_extract_format": 3}) + "\n")
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _make_tgt_client(tmp_path, always_404=True):
    def handler(req):
        return httpx.Response(404 if always_404 else 200, json={})
    conn = Connection(auth_type="pat", site_url="https://t.atlassian.net",
                      deployment="cloud", email="a@b.c", api_token="x")
    return JiraClient(conn,
                      http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def test_detect_user_gaps_has_detail_with_account_id_and_display_name(tmp_path):
    """detect_user_gaps must embed account_id and display_name in detail."""
    p = os.path.join(tmp_path, "src", "P.core.jsonl.gz")
    _write_gz(p, [{"key": "P-1", "fields": {
        "reporter": {"accountId": "a1", "displayName": "Ada Lovelace"}}}])
    tgt = _make_tgt_client(tmp_path)
    gaps = detect_user_gaps(str(tmp_path), ["P"], tgt)
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap["detail"]["account_id"] == "a1"
    assert gap["detail"]["display_name"] == "Ada Lovelace"


# ---------------------------------------------------------------------------
# stage_usergap project field
# ---------------------------------------------------------------------------

class _NullStore:
    def add_event(self, *a, **k):
        pass


class _NullConnector:
    product = "jira"


def _make_ctx(tmp_path, tgt, keys):
    return {
        "store": _NullStore(),
        "run_id": 1,
        "workspace": str(tmp_path),
        "connector": _NullConnector(),
        "selected": [{"key": k} for k in keys],
        "tgt": tgt,
        "issue_findings": [],
    }


def test_stage_usergap_sets_empty_project_not_username(tmp_path):
    """stage_usergap must set project='' on user_gap findings, NOT the username."""
    p = os.path.join(tmp_path, "src", "P.core.jsonl.gz")
    _write_gz(p, [{"key": "P-1", "fields": {
        "reporter": {"accountId": "a1", "displayName": "Ada Lovelace"}}}])
    tgt = _make_tgt_client(tmp_path, always_404=True)
    ctx = _make_ctx(tmp_path, tgt, ["P"])

    stage_usergap(ctx)

    gap_findings = [f for f in ctx["issue_findings"] if f.get("kind") == "user_gap"]
    assert len(gap_findings) == 1
    gap = gap_findings[0]
    # project must NOT be the username or display name
    assert gap["project"] != "Ada Lovelace", (
        "user_gap finding has username in project field — this creates a phantom project")
    assert gap["project"] != "a1", (
        "user_gap finding has account_id in project field — this creates a phantom project")
    # project must be empty (sentinel)
    assert gap["project"] == "", (
        f"user_gap project should be '' but got {gap['project']!r}")


def test_stage_usergap_username_survives_in_detail(tmp_path):
    """After stage_usergap, the username must still be accessible in detail
    so guidance._user_gap can list users correctly."""
    p = os.path.join(tmp_path, "src", "P.core.jsonl.gz")
    _write_gz(p, [{"key": "P-1", "fields": {
        "reporter": {"accountId": "a1", "displayName": "Ada Lovelace"}}}])
    tgt = _make_tgt_client(tmp_path, always_404=True)
    ctx = _make_ctx(tmp_path, tgt, ["P"])

    stage_usergap(ctx)

    gap_findings = [f for f in ctx["issue_findings"] if f.get("kind") == "user_gap"]
    assert len(gap_findings) == 1
    gap = gap_findings[0]
    detail = gap.get("detail", {})
    # username must survive in detail
    assert detail.get("display_name") == "Ada Lovelace", (
        "display_name lost from detail after stage_usergap")
    assert detail.get("account_id") == "a1", (
        "account_id lost from detail after stage_usergap")


def test_guidance_user_gap_still_lists_users_after_fix(tmp_path):
    """guidance_for('user_gap', ...) must still list users even when project=''.

    This verifies that the Bug 2 fix (project='') doesn't break the guidance
    which reads detail.display_name / detail.account_id."""
    # Simulate findings as stored after stage_usergap with the fix applied
    findings = [
        {"kind": "user_gap", "project": "",
         "detail": {"account_id": "a1", "display_name": "Ada Lovelace"}},
        {"kind": "user_gap", "project": "",
         "detail": {"account_id": "a2", "display_name": "Bob Smith"}},
    ]
    g = guidance_for("user_gap", findings)
    assert g is not None
    assert g["count"] == 2
    listed = str(g["missing"])
    assert "Ada Lovelace" in listed
    assert "Bob Smith" in listed
    assert "a1" in listed
    assert "a2" in listed


# ---------------------------------------------------------------------------
# Distribution: user_gap must not appear as a phantom project
# ---------------------------------------------------------------------------

def test_user_gap_findings_do_not_pollute_project_distribution(tmp_path):
    """user_gap findings with project='' must NOT create phantom projects in
    query_issues / kind_counts. Real projects (AC, MS) must be unaffected."""
    store = Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})

    # Two real project findings
    store.insert_findings_issue(rid, [
        {"project": "AC", "kind": "field_mismatch",
         "src_key": "AC-1", "tgt_key": "AC-1", "field": "status",
         "summary": "x", "detail": {"src": "Open", "tgt": "Done"}},
        {"project": "MS", "kind": "field_mismatch",
         "src_key": "MS-1", "tgt_key": "MS-1", "field": "priority",
         "summary": "y", "detail": {"src": "High", "tgt": "Low"}},
        # user_gap findings with project="" (sentinel)
        {"project": "", "kind": "user_gap",
         "src_key": None, "tgt_key": None, "field": None,
         "summary": "user gap",
         "detail": {"account_id": "a1", "display_name": "Ada Lovelace"}},
        {"project": "", "kind": "user_gap",
         "src_key": None, "tgt_key": None, "field": None,
         "summary": "user gap",
         "detail": {"account_id": "a2", "display_name": "Bob Smith"}},
    ])

    # Filter by project="AC" must only return AC findings
    rows, total = store.query_issues(rid, project="AC")
    assert total == 1 and rows[0]["project"] == "AC"

    # Filter by project="" would match user_gap sentinel but callers never
    # pass empty string as a real project. No filter = all rows.
    rows_all, total_all = store.query_issues(rid)
    assert total_all == 4  # all 4 rows present

    # Kind counts per project: AC must only have field_mismatch
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from webapp.analysis import make_router
    store.update_run(rid, status="done", verdict="CLEAN", stats={
        "project_stats": {
            "AC": {"common": 10, "fidelity_pct": 90.0, "issues_with_mismatches": 1},
            "MS": {"common": 5, "fidelity_pct": 80.0, "issues_with_mismatches": 1},
        },
        "issues_src_total": 15, "headlines": [], "areas": {}})

    app = FastAPI()
    app.state.store = store
    app.include_router(make_router())
    c = TestClient(app)

    # project_stats in summary must only have AC and MS (not "" or any username)
    d = c.get(f"/api/runs/{rid}/summary").json()
    ps = d["project_stats"]
    assert set(ps.keys()) == {"AC", "MS"}, (
        f"Expected only AC, MS in project_stats but got: {set(ps.keys())}")

    # kind counts for AC: only field_mismatch, NOT user_gap
    kinds = c.get(f"/api/runs/{rid}/issues/kinds",
                  params={"project": "AC"}).json()
    assert "user_gap" not in kinds, (
        "user_gap appears in kind counts for project AC — phantom project pollution")
    assert kinds.get("field_mismatch") == 1
