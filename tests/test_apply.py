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
    """Planner-level guard: a source-side FixAction raises before any HTTP."""
    bad = _create_action(); bad.side = "source"
    with pytest.raises(ValueError, match="target"):
        apply_plan(mk(lambda r: httpx.Response(200, json=[])),
                   FixPlan(actions=[bad]), lambda x: None)


def test_api_base_mismatch_raises_before_any_http():
    """I1 — runtime identity guard: apply_plan raises when the client's
    api_base does not match expected_api_base, before touching the network."""
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=[])

    client = mk(handler)
    # client.api_base == "https://t.atlassian.net"
    with pytest.raises(ValueError, match="api_base"):
        apply_plan(client, FixPlan(actions=[_create_action()]), lambda x: None,
                   expected_api_base="https://wrong.atlassian.net")
    assert calls == [], "no HTTP must be made when api_base does not match"


def test_api_base_correct_does_not_raise():
    """I1 — matching expected_api_base lets apply_plan proceed normally."""
    def handler(req):
        p = str(req.url.path)
        if p == "/rest/api/3/status":
            return httpx.Response(200, json=[])
        if p == "/rest/api/3/statuses":
            return httpx.Response(200, json=[{"id": "10010", "name": "Triage"}])
        return httpx.Response(404, json={})
    client = mk(handler)
    log = []
    apply_plan(client, FixPlan(actions=[_create_action()]), log.append,
               expected_api_base=client.api_base)   # correct base — no raise
    assert log[0]["ok"]


def test_failed_write_is_logged_and_run_continues():
    def handler(req):
        if str(req.url.path) == "/rest/api/3/status":
            return httpx.Response(200, json=[])
        return httpx.Response(400, json={"_error": "bad"})
    log = []
    apply_plan(mk(handler), FixPlan(actions=[_create_action(), _create_action()]),
               log.append)
    assert len(log) == 2 and all(not a["ok"] for a in log)


def test_link_type_precheck_handles_issuelinktypes_wrapper():
    # The idempotent pre-check must see an existing link type inside the
    # {issueLinkTypes:[...]} wrapper and log a no-op rather than re-creating it.
    def handler(req):
        if str(req.url.path) == "/rest/api/3/issueLinkType":
            return httpx.Response(200, json={"issueLinkTypes": [{"name": "Blocks"}]})
        raise AssertionError("must not POST when the link type already exists")
    action = FixAction(
        fix_id="jira.link_type.create", tier="create", risk="low",
        object_name="Blocks", area="link_types", finding_ref="link_types/Blocks",
        payload={"name": "Blocks", "inward": "is blocked by", "outward": "blocks"})
    log = []
    apply_plan(mk(handler), FixPlan(actions=[action]), log.append)
    assert log[0]["ok"] and log[0]["status"] == 0 and log[0]["error"] == "exists"


# --- C1/I2: wire + populate must resolve the TARGET field id by NAME, never
# trust payload['field_id'] (which is the captured SOURCE id). --------------

def _wire_action():
    return FixAction(
        fix_id="jira.custom_field.wire_screen", tier="wire", risk="medium",
        object_name="Severity", area="custom_fields",
        finding_ref="custom_fields/Severity",
        # field_id here is the SOURCE id — apply must ignore it.
        payload={"field_id": "customfield_SOURCE",
                 "screens": [{"screen_id": "5", "tab_id": "9"}]})


def test_wire_resolves_target_field_id_by_name_not_source():
    seen = {}
    def handler(req):
        p = str(req.url.path)
        if p == "/rest/api/3/field":
            return httpx.Response(200, json=[
                {"id": "customfield_TARGET", "name": "Severity", "custom": True}])
        if p == "/rest/api/3/screens/5/tabs/9/fields":
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected call to {p}")
    log = []
    apply_plan(mk(handler), FixPlan(actions=[_wire_action()]), log.append)
    # The screen wire used the TARGET id resolved by name, not the source id.
    assert seen["body"]["fieldId"] == "customfield_TARGET"
    assert log[0]["ok"]


def test_wire_fails_loud_when_target_field_absent():
    def handler(req):
        if str(req.url.path) == "/rest/api/3/field":
            return httpx.Response(200, json=[])   # field never created on target
        raise AssertionError("must not POST a screen wire when field is absent")
    log = []
    apply_plan(mk(handler), FixPlan(actions=[_wire_action()]), log.append)
    assert not log[0]["ok"] and "not found" in log[0]["error"]


def test_populate_writes_to_resolved_target_field_id(tmp_path):
    vfile = tmp_path / "vals.jsonl.gz"
    with gzip.open(vfile, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"issue_key": "ABC-1", "value": "High"}) + "\n")
    action = FixAction(
        fix_id="jira.custom_field.populate", tier="populate", risk="medium",
        object_name="Severity", area="custom_fields",
        finding_ref="custom_fields/Severity",
        payload={"field_id": "customfield_SOURCE", "values_file": "vals.jsonl.gz"})
    written = {}
    def handler(req):
        p = str(req.url.path)
        if p == "/rest/api/3/field":
            return httpx.Response(200, json=[
                {"id": "customfield_TARGET", "name": "Severity", "custom": True}])
        if p == "/rest/api/3/issue/ABC-1":
            written.update(json.loads(req.content)["fields"])
            return httpx.Response(204, json={})
        raise AssertionError(f"unexpected call to {p}")
    log = []
    apply_plan(mk(handler), FixPlan(actions=[action]), log.append,
               workspace=str(tmp_path))
    # The PUT set the TARGET field id, not the source id.
    assert "customfield_TARGET" in written and written["customfield_TARGET"] == "High"
    assert "customfield_SOURCE" not in written
    assert log[0]["ok"]


# --- C2/I5: add_options must add the missing options to the existing field. --

def _add_options_action():
    return FixAction(
        fix_id="jira.custom_field.add_options", tier="create", risk="low",
        object_name="Severity", area="custom_fields",
        finding_ref="custom_fields/Severity",
        payload={"field_name": "Severity", "missing_options": ["Blocker"]})


def test_add_options_resolves_field_and_posts_to_its_context():
    posted = {}
    def handler(req):
        p = str(req.url.path)
        if p == "/rest/api/3/field":
            return httpx.Response(200, json=[
                {"id": "customfield_T", "name": "Severity", "custom": True}])
        if p == "/rest/api/3/field/customfield_T/context":
            return httpx.Response(200, json={"values": [{"id": "100"}]})
        if p == "/rest/api/3/field/customfield_T/context/100/option":
            posted["body"] = json.loads(req.content)
            return httpx.Response(200, json={"options": [{"id": "1"}]})
        raise AssertionError(f"unexpected call to {p}")
    log = []
    apply_plan(mk(handler), FixPlan(actions=[_add_options_action()]), log.append)
    assert posted["body"]["options"][0]["value"] == "Blocker"
    assert log[0]["ok"] and log[0]["method"] == "POST"


def test_add_options_not_skipped_by_create_precheck():
    # The field EXISTS on the target (precheck would log a no-op for a plain
    # create) — add_options must still write the missing options.
    calls = []
    def handler(req):
        p = str(req.url.path)
        calls.append(p)
        if p == "/rest/api/3/field":
            return httpx.Response(200, json=[
                {"id": "customfield_T", "name": "Severity", "custom": True}])
        if p == "/rest/api/3/field/customfield_T/context":
            return httpx.Response(200, json={"values": [{"id": "100"}]})
        if p == "/rest/api/3/field/customfield_T/context/100/option":
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected call to {p}")
    log = []
    apply_plan(mk(handler), FixPlan(actions=[_add_options_action()]), log.append)
    assert "/rest/api/3/field/customfield_T/context/100/option" in calls
    assert log[0]["ok"]


def test_add_options_with_empty_delta_is_logged_failure():
    a = _add_options_action()
    a.payload = {"field_name": "Severity", "missing_options": []}
    def handler(req):
        raise AssertionError("must not call the API with an empty delta")
    log = []
    apply_plan(mk(handler), FixPlan(actions=[a]), log.append)
    assert not log[0]["ok"] and "no missing options" in log[0]["error"]


def test_add_options_field_absent_is_logged_failure():
    def handler(req):
        if str(req.url.path) == "/rest/api/3/field":
            return httpx.Response(200, json=[])
        raise AssertionError("must not POST options when the field is absent")
    log = []
    apply_plan(mk(handler), FixPlan(actions=[_add_options_action()]), log.append)
    assert not log[0]["ok"] and "not found" in log[0]["error"]


# --- populate live-write safety: blast-radius cap + circuit breaker ----------

def _populate_action():
    return FixAction(
        fix_id="jira.custom_field.populate", tier="populate", risk="medium",
        object_name="Severity", area="custom_fields",
        finding_ref="custom_fields/Severity",
        payload={"field_id": "customfield_SOURCE", "values_file": "vals.jsonl.gz"})


def _write_values(tmp_path, n):
    vfile = tmp_path / "vals.jsonl.gz"
    with gzip.open(vfile, "wt", encoding="utf-8") as fh:
        for i in range(n):
            fh.write(json.dumps({"issue_key": f"ABC-{i}", "value": "X"}) + "\n")


def test_populate_refuses_above_blast_radius_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("MA_MAX_POPULATE", "2")
    _write_values(tmp_path, 3)
    puts = []
    def handler(req):
        p = str(req.url.path)
        if p == "/rest/api/3/field":
            return httpx.Response(200, json=[
                {"id": "customfield_TARGET", "name": "Severity", "custom": True}])
        if p.startswith("/rest/api/3/issue/"):
            puts.append(p)
            return httpx.Response(204, json={})
        raise AssertionError(p)
    log = []
    apply_plan(mk(handler), FixPlan(actions=[_populate_action()]), log.append,
               workspace=str(tmp_path))
    assert puts == []                                    # NOT one value written
    assert not log[0]["ok"] and "cap" in (log[0]["error"] or "").lower()


def test_populate_circuit_breaker_stops_after_repeated_server_errors(tmp_path,
                                                                     monkeypatch):
    monkeypatch.setenv("MA_BREAKER_THRESHOLD", "2")
    _write_values(tmp_path, 6)
    seen = set()   # DISTINCT issues attempted (one PUT retries internally)
    def handler(req):
        p = str(req.url.path)
        if p == "/rest/api/3/field":
            return httpx.Response(200, json=[
                {"id": "customfield_TARGET", "name": "Severity", "custom": True}])
        if p.startswith("/rest/api/3/issue/"):
            seen.add(p)
            return httpx.Response(500, json={})          # instance is failing
        raise AssertionError(p)
    log = []
    apply_plan(mk(handler), FixPlan(actions=[_populate_action()]), log.append,
               workspace=str(tmp_path))
    # After 2 server-side failures the breaker trips; the remaining 4 issues are
    # NEVER touched (the failing instance is not hammered for each one).
    assert seen == {"/rest/api/3/issue/ABC-0", "/rest/api/3/issue/ABC-1"}, seen
    assert not log[0]["ok"] and "breaker" in (log[0]["error"] or "").lower()


def test_populate_reports_partial_failures_without_tripping_on_4xx(tmp_path,
                                                                   monkeypatch):
    monkeypatch.setenv("MA_BREAKER_THRESHOLD", "2")
    _write_values(tmp_path, 3)
    def handler(req):
        p = str(req.url.path)
        if p == "/rest/api/3/field":
            return httpx.Response(200, json=[
                {"id": "customfield_TARGET", "name": "Severity", "custom": True}])
        if p == "/rest/api/3/issue/ABC-1":
            return httpx.Response(400, json={})          # object-level, NOT a trip
        if p.startswith("/rest/api/3/issue/"):
            return httpx.Response(204, json={})
        raise AssertionError(p)
    log = []
    apply_plan(mk(handler), FixPlan(actions=[_populate_action()]), log.append,
               workspace=str(tmp_path))
    # 4xx does not trip the breaker -> all 3 attempted, 1 failed, none skipped.
    assert not log[0]["ok"] and "fail" in (log[0]["error"] or "").lower()
    assert "breaker" not in (log[0]["error"] or "").lower()


def test_populate_transport_failure_is_failed_and_trips_breaker(tmp_path,
                                                                monkeypatch):
    # The instance DROPS connections (not 5xx): the client exhausts its
    # idempotent retries and returns st=-1. That write never landed, so it must
    # count as FAILED (never ok) AND trip the breaker — else a connection-drop
    # storm reads as all-success and hammers every remaining issue.
    monkeypatch.setenv("MA_BREAKER_THRESHOLD", "2")
    _write_values(tmp_path, 6)
    seen = set()
    def handler(req):
        p = str(req.url.path)
        if p == "/rest/api/3/field":
            return httpx.Response(200, json=[
                {"id": "customfield_TARGET", "name": "Severity", "custom": True}])
        if p.startswith("/rest/api/3/issue/"):
            seen.add(p)
            raise httpx.ConnectError("connection refused")
        raise AssertionError(p)
    log = []
    apply_plan(mk(handler), FixPlan(actions=[_populate_action()]), log.append,
               workspace=str(tmp_path))
    assert seen == {"/rest/api/3/issue/ABC-0", "/rest/api/3/issue/ABC-1"}, seen
    assert not log[0]["ok"]
    assert log[0]["created_id"] == "0"            # ZERO counted as written
    assert "breaker" in (log[0]["error"] or "").lower()


def test_populate_malformed_line_is_failed_not_a_crash(tmp_path):
    vfile = tmp_path / "vals.jsonl.gz"
    with gzip.open(vfile, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"issue_key": "ABC-0", "value": "X"}) + "\n")
        fh.write("{ this is not json\n")                       # corrupt capture
        fh.write(json.dumps({"issue_key": "ABC-2", "value": "Y"}) + "\n")
    def handler(req):
        p = str(req.url.path)
        if p == "/rest/api/3/field":
            return httpx.Response(200, json=[
                {"id": "customfield_TARGET", "name": "Severity", "custom": True}])
        if p.startswith("/rest/api/3/issue/"):
            return httpx.Response(204, json={})
        raise AssertionError(p)
    log = []
    apply_plan(mk(handler), FixPlan(actions=[_populate_action()]), log.append,
               workspace=str(tmp_path))
    assert log[0]["created_id"] == "2"            # the 2 valid rows wrote
    assert not log[0]["ok"] and "fail" in (log[0]["error"] or "").lower()
