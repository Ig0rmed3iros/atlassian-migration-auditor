import httpx
from fastapi.testclient import TestClient
from webapp.main import create_app
from webapp.config import Config


def _app(tmp_path):
    cfg = Config(data_dir=str(tmp_path), bind_host="127.0.0.1", bind_port=8484,
                 public_base_url="http://localhost:8484", secret_key=None)
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


def test_post_fix_with_unknown_fix_id_does_not_require_confirm(tmp_path):
    # jira.status.wire_workflow was removed (C3/I4): posting an unknown fix id
    # no longer triggers the confirm gate (needs_confirm is False when no known
    # fix requires confirmation).
    app = _app(tmp_path); store = app.state.store
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    store.update_run(rid, status="done")
    c = TestClient(app)
    # "jira.custom_field.wire_screen" still exists and does NOT require_confirm
    r = c.post(f"/runs/{rid}/fix",
               data={"fix_ids": "jira.custom_field.wire_screen"},
               follow_redirects=False)
    # Redirects to fix-run or to an error, but NOT to a confirm error
    assert r.status_code == 303
    assert "confirm" not in r.headers["location"].lower()


def test_fix_screen_shows_disabled_row_for_payload_less_finding(tmp_path):
    """I12: a finding without fix_payload must render as a disabled row with
    're-run the audit to capture fix data' notice, not as a live checkbox."""
    app = _app(tmp_path)
    store = app.state.store
    mid = store.create_migration("m", product="jira")
    rid = store.create_run(mid, {})
    # No fix_payload on this finding
    store.insert_findings_config(rid, [
        {"area": "statuses", "name": "Triage", "kind": "missing_in_tgt",
         "detail": {}}])
    store.update_run(rid, status="done", verdict="GAPS_FOUND")
    c = TestClient(app)
    r = c.get(f"/runs/{rid}/fix")
    assert r.status_code == 200
    # Must show the disabled notice, not a live checkbox for this finding
    assert "re-run the audit" in r.text.lower() or "capture fix data" in r.text.lower()
    # There must be no enabled checkbox for jira.status.create when all findings
    # for it lack payload (the group has no payload-bearing finding).
    # We accept that the checkbox is absent or disabled.
    import re
    # A live (non-disabled) checkbox for this fix_id must NOT appear
    assert not re.search(
        r'<input[^>]+type="checkbox"[^>]+value="jira\.status\.create"[^>]*>(?!.*disabled)',
        r.text, re.DOTALL)


def test_nothing_applied_verdict_shows_on_fix_run_page(tmp_path):
    """I10: fix_run.html page must display the NOTHING_APPLIED verdict."""
    app = _app(tmp_path)
    store = app.state.store
    mid = store.create_migration("m", product="jira")
    audit = store.create_run(mid, {})
    store.update_run(audit, status="done", verdict="GAPS_FOUND")
    fix_rid = store.create_run(mid, {"fix_ids": ["jira.status.create"]},
                               kind="fix", source_run_id=audit)
    store.update_run(fix_rid, status="done", verdict="NOTHING_APPLIED",
                     stats={"closed": 0, "still_open": 0, "unchanged": 0,
                            "actions": 0, "failed": 0,
                            "headlines": ["Nothing was applied. Re-run the audit to capture fix data."]})
    c = TestClient(app)
    r = c.get(f"/fix-runs/{fix_rid}")
    assert r.status_code == 200
    assert "NOTHING_APPLIED" in r.text or "nothing" in r.text.lower()


def test_analysis_has_fix_options_button(tmp_path):
    app = _app(tmp_path); store = app.state.store
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    store.update_run(rid, status="done", verdict="GAPS_FOUND")
    r = TestClient(app).get(f"/runs/{rid}/analysis")
    assert f"/runs/{rid}/fix" in r.text and "Fix options" in r.text
