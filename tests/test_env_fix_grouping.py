"""The env-fix options screen groups app-tier findings BY PROBLEM TYPE.

Instead of a flat list of N checkboxes (unusable at 200+ orphaned screens), each
problem type is one group with a select-all parent checkbox, a count, the shared
fix detail shown once, and an expander revealing the individual items so an
operator can fine-tune which specific objects to fix.
"""
from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from webapp.config import Config
from webapp.main import create_app
from webapp.env_fix_routes import _group_findings


def _app(tmp_path):
    cfg = Config(data_dir=str(tmp_path), bind_host="127.0.0.1", bind_port=8485,
                 public_base_url="http://localhost:8485", secret_key=None)
    return create_app(cfg, http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))))


def _env_finding(kind, name, area="schemes"):
    from auditor.envaudit.fixes import _FIXES, category_for
    import copy
    fix = copy.copy(_FIXES.get(kind, {"tier": "human", "title": kind}))
    return {"area": area, "name": name, "kind": kind, "severity": "low",
            "detail": {"fix": fix, "category": category_for(kind),
                       "severity": "low"}}


def _seed(tmp_path):
    app = _app(tmp_path)
    store = app.state.store
    mid = store.create_migration("env", product="jira", audit_type="environment")
    audit = store.create_run(mid, {}, kind="env_audit")
    findings = [
        _env_finding("scheme_unused", "Scheme A"),
        _env_finding("scheme_unused", "Scheme B"),
        _env_finding("scheme_unused", "Scheme C"),
        _env_finding("empty_group", "g1", area="groups"),
        _env_finding("empty_group", "g2", area="groups"),
        _env_finding("duplicate_field", "Severity"),   # human — not grouped here
    ]
    for f in findings:
        f.setdefault("detail", {})["severity"] = f.get("severity")
    store.insert_findings_config(audit, findings)
    store.update_run(audit, status="done", verdict="NEEDS_ATTENTION",
                     stats={"health_score": 70, "grade": "C"})
    return app, audit


# ---------------------------------------------------------------------------
# _group_findings — pure grouping (tier-neutral)
# ---------------------------------------------------------------------------

def test_group_findings_groups_by_kind_with_counts():
    items = [
        {"kind": "scheme_unused", "name": "A", "ref": "scheme_unused:A",
         "severity": "low", "fix": {"label": "Delete unused workflow scheme",
                                    "risk": "low", "detail": "d"}},
        {"kind": "scheme_unused", "name": "B", "ref": "scheme_unused:B",
         "severity": "low", "fix": {"label": "Delete unused workflow scheme"}},
        {"kind": "empty_group", "name": "g1", "ref": "empty_group:g1",
         "severity": "high", "fix": {"label": "Delete empty group"}},
    ]
    groups = _group_findings(items)
    by_kind = {g["kind"]: g for g in groups}
    assert by_kind["scheme_unused"]["count"] == 2
    assert by_kind["empty_group"]["count"] == 1
    assert {i["ref"] for i in by_kind["scheme_unused"]["items"]} == {
        "scheme_unused:A", "scheme_unused:B"}
    assert by_kind["scheme_unused"]["label"] == "Delete unused workflow scheme"
    # worst-severity group leads (empty_group high > scheme_unused low)
    assert groups[0]["kind"] == "empty_group"


def test_group_findings_empty_is_empty():
    assert _group_findings([]) == []


# ---------------------------------------------------------------------------
# Rendered fix screen
# ---------------------------------------------------------------------------

def test_fix_screen_renders_groups_with_select_all_and_expander(tmp_path):
    app, audit = _seed(tmp_path)
    t = TestClient(app).get(f"/runs/{audit}/env-fix").text
    # a parent "select all" checkbox per problem type (>=2: scheme_unused + group)
    assert 'class="env-group-box"' in t
    assert t.count("env-group-box") >= 2
    # the scheme group shows its count and is collapsible
    assert "(3)" in t
    assert "<details" in t
    # individual items remain real checkboxes (inside the expander) for fine-tuning
    assert 'value="scheme_unused:Scheme A"' in t
    assert 'value="scheme_unused:Scheme C"' in t
    assert "env-fixbox" in t


def test_apply_button_is_danger_styled_and_confirm_gated(tmp_path):
    # No-bias review (UI criticals): the live Apply button fires PRODUCTION
    # DELETEs but was styled like Save with NO confirm dialog, while deleting a
    # harmless LOCAL row gets confirm(). The most dangerous action must carry
    # danger styling AND a confirm gate; Preview (writes nothing) stays benign.
    import re
    app, audit = _seed(tmp_path)
    t = TestClient(app).get(f"/runs/{audit}/env-fix").text
    m = re.search(r'<button[^>]*id="env-apply-btn"[^>]*>', t)
    assert m, "apply button present"
    btn = m.group(0)
    assert "btn-danger" in btn
    assert "data-confirm" in btn
    pm = re.search(r'<button[^>]*id="env-preview-btn"[^>]*>', t)
    assert pm and "btn-danger" not in pm.group(0) and "data-confirm" not in pm.group(0)
    # Fail-safe default: the hidden dry_run field defaults to preview ("1") so an
    # un-confirmed submit (e.g. Enter-key) can never reach the live-delete path.
    dm = re.search(r'<input[^>]*id="env-dry-run-field"[^>]*>', t)
    assert dm and 'value="1"' in dm.group(0)


def test_fix_screen_group_box_carries_kind_for_select_all(tmp_path):
    app, audit = _seed(tmp_path)
    t = TestClient(app).get(f"/runs/{audit}/env-fix").text
    # the parent checkbox must identify its kind so the JS can toggle its children
    assert 'data-group-kind="scheme_unused"' in t
    # children carry the same kind so they can be matched to their group
    assert 'data-kind="scheme_unused"' in t


# ---------------------------------------------------------------------------
# Human + unfixable tiers must ALSO be grouped (read-only, no checkboxes)
# ---------------------------------------------------------------------------

def _seed_all_tiers(tmp_path):
    app = _app(tmp_path)
    store = app.state.store
    mid = store.create_migration("env", product="jira", audit_type="environment")
    audit = store.create_run(mid, {}, kind="env_audit")
    findings = [
        _env_finding("component_no_lead", "Comp A", area="components"),
        _env_finding("component_no_lead", "Comp B", area="components"),
        _env_finding("component_no_lead", "Comp C", area="components"),  # human x3
        _env_finding("duplicate_field", "Severity"),                    # human x1
        _env_finding("migration_artifact", "WF1 (migrated)"),
        _env_finding("migration_artifact", "WF2 (migrated)"),           # unfixable x2
    ]
    for f in findings:
        f.setdefault("detail", {})["severity"] = f.get("severity")
    store.insert_findings_config(audit, findings)
    store.update_run(audit, status="done", verdict="NEEDS_ATTENTION",
                     stats={"health_score": 70, "grade": "C"})
    return app, audit


def test_human_tier_is_grouped_by_kind_read_only(tmp_path):
    app, audit = _seed_all_tiers(tmp_path)
    t = TestClient(app).get(f"/runs/{audit}/env-fix").text
    # the human section groups by problem type with a count + a collapsible list
    assert "guide-group" in t                  # read-only group marker
    assert "(3)" in t                          # 3 component_no_lead
    assert "Comp A" in t and "Comp C" in t     # affected objects listed in expander
    # human items are guidance, never selectable: no checkbox machinery for them
    assert 'data-group-kind="component_no_lead"' not in t
    assert 'value="component_no_lead:Comp A"' not in t


def test_unfixable_tier_is_grouped_by_kind(tmp_path):
    app, audit = _seed_all_tiers(tmp_path)
    t = TestClient(app).get(f"/runs/{audit}/env-fix").text
    assert "(2)" in t                          # 2 migration_artifact
    assert "WF1 (migrated)" in t and "WF2 (migrated)" in t


def test_human_and_unfixable_collapse_long_lists(tmp_path):
    """Both read-only tiers use an expander so a 700-item kind doesn't flood
    the page — the affected names live inside <details>, not inline."""
    app, audit = _seed_all_tiers(tmp_path)
    t = TestClient(app).get(f"/runs/{audit}/env-fix").text
    # at least: 1 human group (component_no_lead) + 1 unfixable group both expandable
    assert t.count("<details") >= 2
