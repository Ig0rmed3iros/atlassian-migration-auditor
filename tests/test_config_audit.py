import os

import httpx
import auditor.config_audit as _ca_mod
from auditor.client import Connection, JiraClient
from auditor.config_audit import audit_config, _dc_list_sliced, _norm_name


def mk(handler, site, deployment="cloud"):
    conn = Connection(auth_type="pat", site_url=site, email="e", api_token="t",
                      deployment=deployment)
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def make_pair(src_data, tgt_data):
    """src_data/tgt_data: dict path-suffix -> response json."""
    def build(data):
        def handler(req):
            p = str(req.url.path)
            for suffix, payload in data.items():
                if p.endswith(suffix):
                    return httpx.Response(200, json=payload)
            return httpx.Response(200, json={"values": [], "isLast": True})
        return handler
    return (mk(build(src_data), "https://s.atlassian.net"),
            mk(build(tgt_data), "https://t.atlassian.net"))


BASE = {
    "/rest/api/3/status": [{"name": "Open"}, {"name": "On Hold"}],
    "/rest/api/3/issuetype": [{"name": "Task"}],
    "/rest/api/3/priority": [{"name": "P1"}],
    "/rest/api/3/resolution": [{"name": "Done"}],
    "/rest/api/3/issueLinkType": {"issueLinkTypes": [{"name": "Blocks"}]},
    "/rest/api/3/role": [{"name": "Administrators", "id": 9}],
    "/rest/api/3/field": [],
    "/rest/api/3/workflow/search": {"values": [], "isLast": True},
    "/rest/api/3/screens": {"values": [], "isLast": True},
}


def test_simple_dimension_source_only_findings():
    tgt = dict(BASE); tgt["/rest/api/3/status"] = [{"name": "Open"}]
    src_cl, tgt_cl = make_pair(BASE, tgt)
    out = audit_config(src_cl, tgt_cl)
    st = out["areas"]["statuses"]
    assert st["src"] == 2 and st["tgt"] == 1 and st["in_both"] == 1
    f = [x for x in out["findings"] if x["area"] == "statuses"]
    assert f == [{"area": "statuses", "name": "On Hold",
                  "kind": "missing_in_tgt", "detail": {}}]


def test_custom_field_type_and_option_mismatches():
    src = dict(BASE)
    src["/rest/api/3/field"] = [
        {"name": "Squad", "id": "customfield_1", "custom": True,
         "schema": {"custom": "com.atlassian.jira.plugin:select"}},
        {"name": "Effort", "id": "customfield_2", "custom": True,
         "schema": {"custom": "com.atlassian.jira.plugin:float"}},
    ]
    src["/rest/api/3/field/customfield_1/context"] = {
        "values": [{"id": "ctx1"}], "isLast": True}
    src["/rest/api/3/field/customfield_1/context/ctx1/option"] = {
        "values": [{"value": "Alpha"}, {"value": "Beta"}], "isLast": True}
    tgt = dict(BASE)
    tgt["/rest/api/3/field"] = [
        {"name": "Squad", "id": "customfield_9", "custom": True,
         "schema": {"custom": "com.atlassian.jira.plugin:select"}},
        {"name": "Effort", "id": "customfield_8", "custom": True,
         "schema": {"custom": "com.atlassian.jira.plugin:textfield"}},
    ]
    tgt["/rest/api/3/field/customfield_9/context"] = {
        "values": [{"id": "ctxA"}], "isLast": True}
    tgt["/rest/api/3/field/customfield_9/context/ctxA/option"] = {
        "values": [{"value": "Alpha"}], "isLast": True}
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl)
    kinds = {(f["name"], f["kind"]) for f in out["findings"]
             if f["area"] == "custom_fields"}
    assert ("Effort", "type_mismatch") in kinds
    assert ("Squad", "option_mismatch") in kinds
    opt = next(f for f in out["findings"] if f["kind"] == "option_mismatch")
    assert opt["detail"]["missing_options_in_tgt"] == ["Beta"]


def test_workflow_structure_mismatch():
    src = dict(BASE)
    src["/rest/api/3/workflow/search"] = {"values": [
        {"id": {"name": "Flow"}, "transitions": [{"name": "Start"}, {"name": "Finish"}],
         "statuses": [{"id": "1"}, {"id": "2"}]}], "isLast": True}
    tgt = dict(BASE)
    tgt["/rest/api/3/workflow/search"] = {"values": [
        {"id": {"name": "Flow"}, "transitions": [{"name": "Start"}],
         "statuses": [{"id": "1"}]}], "isLast": True}
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl)
    f = next(x for x in out["findings"] if x["area"] == "workflows")
    assert f["kind"] == "structure_mismatch" and f["name"] == "Flow"
    assert f["detail"]["transitions_missing_in_tgt"] == ["Finish"]


def test_jsm_request_types_and_queues():
    src = dict(BASE)
    src["/rest/servicedeskapi/servicedesk"] = {
        "values": [{"id": "4", "projectKey": "AC"}], "isLastPage": True}
    src["/rest/servicedeskapi/servicedesk/4/requesttype"] = {
        "values": [{"name": "Bug"}, {"name": "Access"}], "isLastPage": True}
    src["/rest/servicedeskapi/servicedesk/4/queue"] = {
        "values": [{"name": "All open"}], "isLastPage": True}
    tgt = dict(BASE)
    tgt["/rest/servicedeskapi/servicedesk"] = {
        "values": [{"id": "7", "projectKey": "AC"}], "isLastPage": True}
    tgt["/rest/servicedeskapi/servicedesk/7/requesttype"] = {
        "values": [{"name": "Bug"}], "isLastPage": True}
    tgt["/rest/servicedeskapi/servicedesk/7/queue"] = {
        "values": [{"name": "All open"}], "isLastPage": True}
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    f = [x for x in out["findings"] if x["area"] == "jsm"]
    assert f == [{"area": "jsm", "name": "AC: request type 'Access'",
                  "kind": "missing_in_tgt",
                  "detail": {"project": "AC", "object": "request_type"}}]
    assert out["areas"]["jsm"]["AC"]["request_types"]["src"] == 2


def test_source_fetch_error_emits_area_error_not_clean():
    # source /status errors; target has fewer statuses -> must NOT read as clean
    def src_handler(req):
        p = str(req.url.path)
        if p.endswith("/rest/api/3/status"):
            return httpx.Response(503, text="down")
        return httpx.Response(200, json={"values": [], "isLast": True})
    def tgt_handler(req):
        return httpx.Response(200, json={"values": [], "isLast": True})
    src_cl = mk(src_handler, "https://s.atlassian.net")
    tgt_cl = mk(tgt_handler, "https://t.atlassian.net")
    out = audit_config(src_cl, tgt_cl)
    errs = [f for f in out["findings"] if f["kind"] == "area_error"]
    assert any(f["area"] == "statuses" and f["detail"]["side"] == "source"
               for f in errs)


def test_jsm_outage_is_scoped_not_total_abort():
    # statuses compare fine; JSM 500 must not lose the status findings
    def src_handler(req):
        p = str(req.url.path)
        if p.endswith("/rest/api/3/status"):
            return httpx.Response(200, json=[{"name": "Open"}, {"name": "On Hold"}])
        if "servicedesk" in p:
            return httpx.Response(503, text="down")
        return httpx.Response(200, json={"values": [], "isLast": True})
    def tgt_handler(req):
        p = str(req.url.path)
        if p.endswith("/rest/api/3/status"):
            return httpx.Response(200, json=[{"name": "Open"}])
        if "servicedesk" in p:
            return httpx.Response(503, text="down")
        return httpx.Response(200, json={"values": [], "isLast": True})
    src_cl = mk(src_handler, "https://s.atlassian.net")
    tgt_cl = mk(tgt_handler, "https://t.atlassian.net")
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    # status gap still found:
    assert any(f["area"] == "statuses" and f["name"] == "On Hold"
               for f in out["findings"])
    # JSM failure surfaced as area_error, not an exception:
    assert any(f["area"] == "jsm" and f["kind"] == "area_error"
               for f in out["findings"])


# ── name-normalization (TASK C) ────────────────────────────────────────────

def test_norm_name_strips_migrated_suffix_and_case_whitespace():
    # Migration tool's " (migrated)" suffix, case, and inner whitespace all fold
    assert _norm_name("Approved") == "approved"
    assert _norm_name("Approved (migrated)") == "approved"
    assert _norm_name("APPROVED") == "approved"
    assert _norm_name("Approved  (Migrated)") == "approved"
    assert _norm_name("  In   Progress  ") == "in progress"
    # only a TRAILING (migrated) token is stripped, not an inner occurrence
    assert _norm_name("Approved (migrated) plan") == "approved (migrated) plan"


def test_migrated_suffix_and_case_collapse_to_in_both():
    # Source status "Approved"; target same status renamed by the migration tool
    # to "Approved (migrated)" plus a case-only variant. Both must be treated as
    # the SAME object: no missing_in_tgt finding, in_both counts the match once.
    src = dict(BASE)
    src["/rest/api/3/status"] = [{"name": "Open"}, {"name": "Approved"}]
    tgt = dict(BASE)
    tgt["/rest/api/3/status"] = [{"name": "Open"}, {"name": "APPROVED (migrated)"}]
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl)
    st = out["areas"]["statuses"]
    # "Open" + "Approved"~"APPROVED (migrated)" both match -> in_both == 2
    assert st["in_both"] == 2
    assert st["source_only"] == []
    assert [f for f in out["findings"]
            if f["area"] == "statuses" and f["kind"] == "missing_in_tgt"] == []


def test_genuine_source_only_still_reported():
    # A status truly absent in target is still a gap, even with the new matcher
    src = dict(BASE)
    src["/rest/api/3/status"] = [{"name": "Open"}, {"name": "Escalated"}]
    tgt = dict(BASE)
    tgt["/rest/api/3/status"] = [{"name": "Open"}]
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl)
    f = [x for x in out["findings"]
         if x["area"] == "statuses" and x["kind"] == "missing_in_tgt"]
    assert f == [{"area": "statuses", "name": "Escalated",
                  "kind": "missing_in_tgt", "detail": {}}]


def test_different_names_are_not_merged():
    # "Approved" and "Rejected" are clearly different; the conservative matcher
    # must NOT fold them together -> Approved stays a genuine source-only gap.
    src = dict(BASE)
    src["/rest/api/3/status"] = [{"name": "Approved"}]
    tgt = dict(BASE)
    tgt["/rest/api/3/status"] = [{"name": "Rejected"}]
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl)
    names = {f["name"] for f in out["findings"]
             if f["area"] == "statuses" and f["kind"] == "missing_in_tgt"}
    assert names == {"Approved"}


def test_source_dupe_by_normalization_dedupes_keeps_first_original():
    # Two source statuses that normalize equal ("Approved" / "Approved (migrated)")
    # collapse to a single object; first original is kept for display.
    src = dict(BASE)
    src["/rest/api/3/status"] = [{"name": "Approved"},
                                 {"name": "Approved (migrated)"}]
    tgt = dict(BASE)
    tgt["/rest/api/3/status"] = [{"name": "Open"}]
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl)
    st = out["areas"]["statuses"]
    # de-duped: only ONE source-only object, displayed with the first original
    assert st["source_only"] == ["Approved"]
    assert st["src"] == 1
    miss = [f for f in out["findings"]
            if f["area"] == "statuses" and f["kind"] == "missing_in_tgt"]
    assert miss == [{"area": "statuses", "name": "Approved",
                     "kind": "missing_in_tgt", "detail": {}}]


def test_target_only_names_persisted_in_summary():
    # target_only original names are captured for future inspection, and
    # backward-compatible target_only_count is preserved.
    src = dict(BASE)
    src["/rest/api/3/status"] = [{"name": "Open"}]
    tgt = dict(BASE)
    tgt["/rest/api/3/status"] = [{"name": "Open"}, {"name": "Archived"}]
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl)
    st = out["areas"]["statuses"]
    assert st["target_only_count"] == 1
    assert st["target_only"] == ["Archived"]


# ── Data Center capability gating (Task 6) ─────────────────────────────────

def _seen_handler(data, seen):
    """make_pair's suffix-matching handler, plus a seen-paths set so tests can
    assert which api dialect was actually requested per side."""
    def handler(req):
        p = str(req.url.path)
        seen.add(p)
        for suffix, payload in data.items():
            if p.endswith(suffix):
                return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"values": [], "isLast": True})
    return handler


def make_dc_cloud_pair(src_data, tgt_data):
    """DC source + Cloud target, each with its own seen-paths set."""
    src_seen, tgt_seen = set(), set()
    src = mk(_seen_handler(src_data, src_seen), "https://s.example.com", "dc")
    tgt = mk(_seen_handler(tgt_data, tgt_seen), "https://t.atlassian.net")
    return src, tgt, src_seen, tgt_seen


DC_BASE = {
    "/rest/api/2/status": [{"name": "Open"}],
    "/rest/api/2/issuetype": [{"name": "Task"}],
    "/rest/api/2/priority": [{"name": "P1"}],
    "/rest/api/2/resolution": [{"name": "Done"}],
    "/rest/api/2/issueLinkType": {"issueLinkTypes": [{"name": "Blocks"}]},
    "/rest/api/2/role": [{"name": "Administrators"}],
    "/rest/api/2/issuetypescheme": {"schemes": []},
    "/rest/api/2/field": [],
    "/rest/api/2/workflow": [],
    "/rest/api/2/screens": [],
}

_CLOUD_ONLY_AREAS = ("issuetype_screen_schemes", "screen_schemes",
                     "field_configurations", "field_config_schemes",
                     "workflow_schemes")


def test_dc_source_skips_cloud_only_areas():
    src_data = dict(DC_BASE)
    src_data["/rest/api/2/status"] = [{"name": "Open"}, {"name": "On Hold"}]
    src_data["/rest/api/2/priority"] = [{"name": "P1"}, {"name": "P2"}]
    tgt_data = dict(BASE)
    tgt_data["/rest/api/3/status"] = [{"name": "Open"}]
    src_cl, tgt_cl, src_seen, tgt_seen = make_dc_cloud_pair(src_data, tgt_data)
    out = audit_config(src_cl, tgt_cl)
    for area in _CLOUD_ONLY_AREAS:
        assert out["areas"][area]["skipped"] is True
        assert out["areas"][area]["reason"]
        assert [f for f in out["findings"] if f["area"] == area] == []
    # skipped means skipped: no Cloud-only endpoint was ever requested
    assert not any(p.endswith("/workflowscheme") for p in src_seen | tgt_seen)
    # the dual-dialect audit still finds real gaps on both sides
    gaps = {(f["area"], f["name"]) for f in out["findings"]
            if f["kind"] == "missing_in_tgt"}
    assert ("statuses", "On Hold") in gaps
    assert ("priorities", "P2") in gaps
    # each side spoke its OWN dialect
    assert any(p.startswith("/rest/api/2/") for p in src_seen)
    assert not any(p.startswith("/rest/api/3/") for p in src_seen)
    assert any(p.startswith("/rest/api/3/") for p in tgt_seen)
    assert not any(p.startswith("/rest/api/2/") for p in tgt_seen)


def test_dc_issuetype_schemes_uses_schemes_key():
    src_data = dict(DC_BASE)
    src_data["/rest/api/2/issuetypescheme"] = {
        "schemes": [{"name": "Alpha Scheme"}, {"name": "Beta Scheme"}]}
    tgt_data = dict(BASE)
    tgt_data["/rest/api/3/issuetypescheme"] = {
        "values": [{"name": "Alpha Scheme"}], "isLast": True}
    src_cl, tgt_cl, _, _ = make_dc_cloud_pair(src_data, tgt_data)
    out = audit_config(src_cl, tgt_cl)
    its = out["areas"]["issuetype_schemes"]
    assert "skipped" not in its
    assert its["src"] == 2 and its["tgt"] == 1 and its["in_both"] == 1
    assert [f["name"] for f in out["findings"]
            if f["area"] == "issuetype_schemes"] == ["Beta Scheme"]


def test_dc_workflows_name_presence_only():
    src_data = dict(DC_BASE)
    # DC /workflow shape: plain array, steps is an int COUNT — no transitions
    src_data["/rest/api/2/workflow"] = [
        {"name": "Flow", "description": "", "steps": 3},
        {"name": "Legacy Flow", "description": "", "steps": 2}]
    tgt_data = dict(BASE)
    tgt_data["/rest/api/3/workflow/search"] = {"values": [
        {"id": {"name": "Flow"}, "transitions": [{"name": "Start"}],
         "statuses": [{"id": "1"}]}], "isLast": True}
    src_cl, tgt_cl, _, _ = make_dc_cloud_pair(src_data, tgt_data)
    out = audit_config(src_cl, tgt_cl)
    wf = [f for f in out["findings"] if f["area"] == "workflows"]
    assert [(f["name"], f["kind"]) for f in wf] == [("Legacy Flow",
                                                     "missing_in_tgt")]
    assert not any(f["kind"] == "structure_mismatch" for f in out["findings"])
    assert out["areas"]["workflows"]["structure_checked"] is False


def test_dc_screens_sliced_array_pagination():
    screens = [{"id": i, "name": f"Screen {i}"} for i in range(70)]

    def src_handler(req):
        p = str(req.url.path)
        if p.endswith("/rest/api/2/screens"):
            start = int(req.url.params.get("startAt", 0))
            return httpx.Response(200, json=screens[start:start + 50])
        for suffix, payload in DC_BASE.items():
            if p.endswith(suffix):
                return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"values": [], "isLast": True})

    src_cl = mk(src_handler, "https://s.example.com", "dc")
    tgt_cl = mk(_seen_handler(dict(BASE), set()), "https://t.atlassian.net")
    out = audit_config(src_cl, tgt_cl)
    assert out["areas"]["screens"]["src"] == 70
    assert "error" not in out["areas"]["screens"]

    # endpoint that ignores startAt and replays a full-size slice forever:
    # the no-new-ids guard must terminate after the second request
    replay = [{"id": i, "name": f"S{i}"} for i in range(50)]
    calls = {"n": 0}

    def replay_handler(req):
        calls["n"] += 1
        return httpx.Response(200, json=replay)

    cl = mk(replay_handler, "https://s.example.com", "dc")
    out2, err = _dc_list_sliced(cl, "/rest/api/2/screens")
    assert err is None and len(out2) == 50
    assert calls["n"] == 2


def test_options_check_requires_both_cloud():
    select_schema = {"custom": "com.atlassian.jira.plugin:select"}
    src_data = dict(DC_BASE)
    src_data["/rest/api/2/field"] = [
        {"name": "Squad", "id": "customfield_1", "custom": True,
         "schema": select_schema}]
    tgt_data = dict(BASE)
    tgt_data["/rest/api/3/field"] = [
        {"name": "Squad", "id": "customfield_9", "custom": True,
         "schema": select_schema}]
    src_cl, tgt_cl, src_seen, tgt_seen = make_dc_cloud_pair(src_data, tgt_data)
    out = audit_config(src_cl, tgt_cl)
    assert not any("/context" in p for p in src_seen | tgt_seen)
    assert out["areas"]["custom_fields"]["options_checked"] is False
    # presence matching still ran on the dc+cloud pair
    assert out["areas"]["custom_fields"]["in_both"] == 1

    # cloud+cloud keeps the deep option check (existing behavior)
    c_src = dict(BASE)
    c_src["/rest/api/3/field"] = [
        {"name": "Squad", "id": "customfield_1", "custom": True,
         "schema": select_schema}]
    c_src["/rest/api/3/field/customfield_1/context"] = {
        "values": [{"id": "ctx1"}], "isLast": True}
    c_src["/rest/api/3/field/customfield_1/context/ctx1/option"] = {
        "values": [{"value": "Alpha"}], "isLast": True}
    c_tgt = dict(BASE)
    c_tgt["/rest/api/3/field"] = [
        {"name": "Squad", "id": "customfield_9", "custom": True,
         "schema": select_schema}]
    c_tgt["/rest/api/3/field/customfield_9/context"] = {
        "values": [{"id": "ctxA"}], "isLast": True}
    c_tgt["/rest/api/3/field/customfield_9/context/ctxA/option"] = {
        "values": [{"value": "Alpha"}], "isLast": True}
    s_seen, t_seen = set(), set()
    s_cl = mk(_seen_handler(c_src, s_seen), "https://s.atlassian.net")
    t_cl = mk(_seen_handler(c_tgt, t_seen), "https://t.atlassian.net")
    audit_config(s_cl, t_cl)
    assert any("/context" in p for p in s_seen)
    assert any("/context" in p for p in t_seen)


def test_custom_field_migrated_suffix_collapses_no_false_gap():
    # A custom field renamed by the tool to "<name> (migrated)" in target must
    # NOT be reported as missing; type comparison still runs on the matched pair.
    src = dict(BASE)
    src["/rest/api/3/field"] = [
        {"name": "Squad", "id": "customfield_1", "custom": True,
         "schema": {"custom": "com.atlassian.jira.plugin:select"}},
    ]
    tgt = dict(BASE)
    tgt["/rest/api/3/field"] = [
        {"name": "Squad (migrated)", "id": "customfield_9", "custom": True,
         "schema": {"custom": "com.atlassian.jira.plugin:select"}},
    ]
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl)
    miss = [f for f in out["findings"]
            if f["area"] == "custom_fields" and f["kind"] == "missing_in_tgt"]
    assert miss == []
    assert out["areas"]["custom_fields"]["in_both"] == 1


def test_dc_sliced_non_list_200_is_error_not_clean():
    # A 200 whose body is not a list (e.g. an error wrapper served with the
    # wrong status) must surface as an error, never as a clean empty area —
    # that is the false-CLEAN direction.
    cl = mk(lambda r: httpx.Response(200, json={"errorMessages": ["boom"]}),
            "https://s.example.com", "dc")
    out, err = _dc_list_sliced(cl, "/rest/api/2/screens")
    assert out == []
    assert err is not None and err.startswith("ERRshape")


def test_dc_sliced_id_less_items_use_stable_fallback_key():
    # Rows without an "id" key must not all collapse onto None in the replay
    # guard: a second page of NEW id-less rows would read as a replay and be
    # silently dropped.
    page1 = [{"name": f"Screen {i}"} for i in range(50)]
    page2 = [{"name": f"Screen {i}"} for i in range(50, 70)]

    def handler(req):
        start = int(req.url.params.get("startAt", 0))
        return httpx.Response(200, json=(page1 if start == 0 else page2))

    cl = mk(handler, "https://s.example.com", "dc")
    out, err = _dc_list_sliced(cl, "/rest/api/2/screens")
    assert err is None and len(out) == 70

    # ...while a TRUE replay of id-less rows still terminates.
    calls = {"n": 0}

    def replay_handler(req):
        calls["n"] += 1
        return httpx.Response(200, json=page1)

    cl2 = mk(replay_handler, "https://s.example.com", "dc")
    out2, err2 = _dc_list_sliced(cl2, "/rest/api/2/screens")
    assert err2 is None and len(out2) == 50 and calls["n"] == 2


# ── per-project components + versions parity ───────────────────────────────

def test_components_and_versions_missing_in_target():
    # Source project AC has components/versions the target lacks -> one
    # missing_in_tgt finding per missing object, in areas components/versions,
    # each carrying the project in detail. Matched objects produce no finding.
    src = dict(BASE)
    src["/rest/api/3/project/AC/components"] = [
        {"name": "Backend"}, {"name": "Frontend"}]
    src["/rest/api/3/project/AC/versions"] = [
        {"name": "1.0"}, {"name": "2.0"}]
    tgt = dict(BASE)
    tgt["/rest/api/3/project/AC/components"] = [{"name": "Backend"}]
    tgt["/rest/api/3/project/AC/versions"] = [{"name": "1.0"}]
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))

    comp = [f for f in out["findings"] if f["area"] == "components"]
    assert comp == [{"area": "components", "name": "AC / Frontend",
                     "kind": "missing_in_tgt", "detail": {"project": "AC"}}]
    ver = [f for f in out["findings"] if f["area"] == "versions"]
    assert ver == [{"area": "versions", "name": "AC / 2.0",
                    "kind": "missing_in_tgt", "detail": {"project": "AC"}}]
    # detect-and-guide: no fix_payload is attached by the audit itself
    assert all("fix_payload" not in f for f in comp + ver)


def test_components_versions_all_match_no_finding():
    src = dict(BASE)
    src["/rest/api/3/project/AC/components"] = [{"name": "Backend"}]
    src["/rest/api/3/project/AC/versions"] = [{"name": "1.0"}]
    tgt = dict(BASE)
    # case/whitespace + migration " (migrated)" suffix variants still match
    tgt["/rest/api/3/project/AC/components"] = [{"name": "BACKEND (migrated)"}]
    tgt["/rest/api/3/project/AC/versions"] = [{"name": "  1.0  "}]
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    assert [f for f in out["findings"]
            if f["area"] in ("components", "versions")] == []


def test_components_versions_only_run_for_audited_projects():
    # No jsm_projects -> no per-project component/version requests at all.
    src = dict(BASE)
    src["/rest/api/3/project/AC/components"] = [{"name": "Backend"}]
    tgt = dict(BASE)
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl)
    assert [f for f in out["findings"]
            if f["area"] in ("components", "versions")] == []


def test_components_unreadable_source_is_area_error_not_clean():
    # Source components endpoint 503s while target has FEWER components: a
    # silent clean here would hide a real loss. Must surface area_error.
    def src_handler(req):
        p = str(req.url.path)
        if p.endswith("/project/AC/components"):
            return httpx.Response(503, text="down")
        if p.endswith("/rest/api/3/status"):
            return httpx.Response(200, json=[{"name": "Open"}])
        return httpx.Response(200, json={"values": [], "isLast": True})

    def tgt_handler(req):
        return httpx.Response(200, json={"values": [], "isLast": True})

    src_cl = mk(src_handler, "https://s.atlassian.net")
    tgt_cl = mk(tgt_handler, "https://t.atlassian.net")
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    errs = [f for f in out["findings"] if f["kind"] == "area_error"
            and f["area"] == "components"]
    assert any(f["detail"]["side"] == "source"
               and f["detail"].get("project") == "AC" for f in errs)
    # the area summary must mark itself unreadable, never a clean 0
    assert out["areas"]["components"].get("error")


def test_versions_unreadable_target_is_area_error_not_clean():
    # Target versions endpoint 503s while source has versions: a clean read
    # would hide that the parity could not be checked. Must surface area_error.
    src = dict(BASE)
    src["/rest/api/3/project/AC/versions"] = [{"name": "1.0"}]

    def tgt_handler(req):
        p = str(req.url.path)
        if p.endswith("/project/AC/versions"):
            return httpx.Response(503, text="down")
        return httpx.Response(200, json={"values": [], "isLast": True})

    src_cl, _ = make_pair(src, dict(BASE))
    tgt_cl = mk(tgt_handler, "https://t.atlassian.net")
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    errs = [f for f in out["findings"] if f["kind"] == "area_error"
            and f["area"] == "versions"]
    assert any(f["detail"]["side"] == "target"
               and f["detail"].get("project") == "AC" for f in errs)
    assert out["areas"]["versions"].get("error")
    # an unreadable side must NOT emit a false missing_in_tgt clean signal
    assert not any(f["area"] == "versions" and f["kind"] == "missing_in_tgt"
                   for f in out["findings"])


# ── SIMPLE-areas concurrency equivalence (Task 3) ──────────────────────────

def _rich_pair():
    """A src/tgt pair exercising SIMPLE areas with a clear source-only gap."""
    src = dict(BASE)
    src["/rest/api/3/status"] = [{"name": "Open"}, {"name": "On Hold"},
                                 {"name": "Blocked"}]
    src["/rest/api/3/priority"] = [{"name": "P1"}, {"name": "P2"}]
    tgt = dict(BASE)                      # BASE status has Open and On Hold; src adds Blocked
    tgt["/rest/api/3/priority"] = [{"name": "P1"}]
    return make_pair(src, tgt)


def test_config_simple_areas_seq_vs_parallel_identical(monkeypatch):
    monkeypatch.setenv("MA_GATHER_WORKERS", "1")
    s1, t1 = _rich_pair()
    seq = audit_config(s1, t1)
    monkeypatch.setenv("MA_GATHER_WORKERS", "10")
    s2, t2 = _rich_pair()
    par = audit_config(s2, t2)
    assert seq["areas"] == par["areas"]
    assert seq["findings"] == par["findings"]
    # sanity: the source-only gaps are actually present
    # BASE tgt already has "Open" and "On Hold"; src adds "Blocked" only.
    names = {f["name"] for f in seq["findings"] if f["area"] == "statuses"}
    assert {"Blocked"} <= names


# ── custom-field option concurrency equivalence (Task 4) ───────────────────

def _two_select_pair():
    src = dict(BASE)
    src["/rest/api/3/field"] = [
        {"name": "Squad", "id": "customfield_1", "custom": True,
         "schema": {"custom": "com.atlassian.jira.plugin:select"}},
        {"name": "Tier", "id": "customfield_2", "custom": True,
         "schema": {"custom": "com.atlassian.jira.plugin:radiobuttons"}},
    ]
    src["/rest/api/3/field/customfield_1/context"] = {"values": [{"id": "c1"}], "isLast": True}
    src["/rest/api/3/field/customfield_1/context/c1/option"] = {
        "values": [{"value": "Alpha"}, {"value": "Beta"}], "isLast": True}
    src["/rest/api/3/field/customfield_2/context"] = {"values": [{"id": "c2"}], "isLast": True}
    src["/rest/api/3/field/customfield_2/context/c2/option"] = {
        "values": [{"value": "Gold"}, {"value": "Silver"}], "isLast": True}
    tgt = dict(BASE)
    tgt["/rest/api/3/field"] = [
        {"name": "Squad", "id": "customfield_9", "custom": True,
         "schema": {"custom": "com.atlassian.jira.plugin:select"}},
        {"name": "Tier", "id": "customfield_8", "custom": True,
         "schema": {"custom": "com.atlassian.jira.plugin:radiobuttons"}},
    ]
    tgt["/rest/api/3/field/customfield_9/context"] = {"values": [{"id": "cA"}], "isLast": True}
    tgt["/rest/api/3/field/customfield_9/context/cA/option"] = {
        "values": [{"value": "Alpha"}], "isLast": True}      # missing Beta
    tgt["/rest/api/3/field/customfield_8/context"] = {"values": [{"id": "cB"}], "isLast": True}
    tgt["/rest/api/3/field/customfield_8/context/cB/option"] = {
        "values": [{"value": "Gold"}, {"value": "Silver"}], "isLast": True}  # complete
    return make_pair(src, tgt)


def test_config_custom_field_options_seq_vs_parallel_identical(monkeypatch):
    monkeypatch.setenv("MA_GATHER_WORKERS", "1")
    s1, t1 = _two_select_pair(); seq = audit_config(s1, t1)
    monkeypatch.setenv("MA_GATHER_WORKERS", "10")
    s2, t2 = _two_select_pair(); par = audit_config(s2, t2)
    assert seq["areas"]["custom_fields"] == par["areas"]["custom_fields"]
    assert ([f for f in seq["findings"] if f["area"] == "custom_fields"]
            == [f for f in par["findings"] if f["area"] == "custom_fields"])
    miss = [f for f in seq["findings"]
            if f["area"] == "custom_fields" and f["kind"] == "option_mismatch"]
    assert len(miss) == 1 and miss[0]["name"] == "Squad"
    assert miss[0]["detail"]["missing_options_in_tgt"] == ["Beta"]


# ── screen field concurrency equivalence (Task 5) ──────────────────────────

def _two_screen_pair():
    src = dict(BASE)
    src["/rest/api/3/screens"] = {"values": [
        {"id": 1, "name": "Default Screen"}, {"id": 2, "name": "Bug Screen"}],
        "isLast": True}
    # screen 1: tab 10 with fields A,B ; screen 2: tab 20 with field C
    src["/rest/api/3/screens/1/tabs"] = [{"id": 10}]
    src["/rest/api/3/screens/1/tabs/10/fields"] = [{"name": "A"}, {"name": "B"}]
    src["/rest/api/3/screens/2/tabs"] = [{"id": 20}]
    src["/rest/api/3/screens/2/tabs/20/fields"] = [{"name": "C"}]
    tgt = dict(BASE)
    tgt["/rest/api/3/screens"] = {"values": [
        {"id": 91, "name": "Default Screen"}, {"id": 92, "name": "Bug Screen"}],
        "isLast": True}
    tgt["/rest/api/3/screens/91/tabs"] = [{"id": 30}]
    tgt["/rest/api/3/screens/91/tabs/30/fields"] = [{"name": "A"}]   # missing B
    tgt["/rest/api/3/screens/92/tabs"] = [{"id": 40}]
    tgt["/rest/api/3/screens/92/tabs/40/fields"] = [{"name": "C"}]   # complete
    return make_pair(src, tgt)


def test_config_screen_fields_seq_vs_parallel_identical(monkeypatch):
    monkeypatch.setenv("MA_GATHER_WORKERS", "1")
    s1, t1 = _two_screen_pair(); seq = audit_config(s1, t1)
    monkeypatch.setenv("MA_GATHER_WORKERS", "10")
    s2, t2 = _two_screen_pair(); par = audit_config(s2, t2)
    assert seq["areas"]["screens"] == par["areas"]["screens"]
    sc = [f for f in seq["findings"] if f["area"] == "screens"]
    assert sc == [f for f in par["findings"] if f["area"] == "screens"]
    assert sc == [{"area": "screens", "name": "Default Screen",
                   "kind": "field_mismatch",
                   "detail": {"fields_missing_in_tgt": ["B"]}}]


# ── integrated full-audit concurrency equivalence ─────────────────────────────

def _full_pair():
    """A single src/tgt pair that exercises all three parallel phases together:
    SIMPLE areas (a source-only status), custom-field select options (a field
    with a missing target option), and screens deep-check (a screen with a
    missing target field)."""
    src = dict(BASE)
    # SIMPLE area gap: src has Blocked, tgt (via BASE) has Open + On Hold only
    src["/rest/api/3/status"] = [{"name": "Open"}, {"name": "On Hold"},
                                 {"name": "Blocked"}]
    # custom-field with a missing option in tgt
    src["/rest/api/3/field"] = [
        {"name": "Squad", "id": "customfield_1", "custom": True,
         "schema": {"custom": "com.atlassian.jira.plugin:select"}},
    ]
    src["/rest/api/3/field/customfield_1/context"] = {
        "values": [{"id": "c1"}], "isLast": True}
    src["/rest/api/3/field/customfield_1/context/c1/option"] = {
        "values": [{"value": "Alpha"}, {"value": "Beta"}], "isLast": True}
    # screen deep-check: Default Screen missing field B on tgt
    src["/rest/api/3/screens"] = {"values": [
        {"id": 1, "name": "Default Screen"}], "isLast": True}
    src["/rest/api/3/screens/1/tabs"] = [{"id": 10}]
    src["/rest/api/3/screens/1/tabs/10/fields"] = [{"name": "A"}, {"name": "B"}]

    tgt = dict(BASE)
    # tgt status: only Open + On Hold (Blocked is missing)
    tgt["/rest/api/3/field"] = [
        {"name": "Squad", "id": "customfield_9", "custom": True,
         "schema": {"custom": "com.atlassian.jira.plugin:select"}},
    ]
    tgt["/rest/api/3/field/customfield_9/context"] = {
        "values": [{"id": "cA"}], "isLast": True}
    tgt["/rest/api/3/field/customfield_9/context/cA/option"] = {
        "values": [{"value": "Alpha"}], "isLast": True}   # Beta missing
    tgt["/rest/api/3/screens"] = {"values": [
        {"id": 91, "name": "Default Screen"}], "isLast": True}
    tgt["/rest/api/3/screens/91/tabs"] = [{"id": 30}]
    tgt["/rest/api/3/screens/91/tabs/30/fields"] = [{"name": "A"}]  # B missing
    return make_pair(src, tgt)


def test_config_full_audit_seq_vs_parallel_identical(monkeypatch):
    monkeypatch.setenv("MA_GATHER_WORKERS", "1")
    s1, t1 = _full_pair()
    seq = audit_config(s1, t1)
    monkeypatch.setenv("MA_GATHER_WORKERS", "10")
    s2, t2 = _full_pair()
    par = audit_config(s2, t2)
    assert seq["areas"] == par["areas"]
    assert seq["findings"] == par["findings"]
    # sanity: all three phases produced findings
    assert any(f["area"] == "statuses" and f["kind"] == "missing_in_tgt"
               for f in seq["findings"]), "expected a statuses gap"
    assert any(f["area"] == "custom_fields" and f["kind"] == "option_mismatch"
               for f in seq["findings"]), "expected a custom-field option gap"
    assert any(f["area"] == "screens" and f["kind"] == "field_mismatch"
               for f in seq["findings"]), "expected a screen field gap"


# ── per-project scoping: workflows + screens (Stream B) ────────────────────

def _wfscheme_project(scheme):
    """Wrap a workflow scheme object the way GET /workflowscheme/project does:
    {"values": [{"workflowScheme": {...}}]}."""
    return {"values": [{"workflowScheme": scheme}]}


def test_workflows_scoped_to_selected_project_ignores_out_of_scope_diff():
    # Instance has two workflows that DIFFER between src and tgt, but project AC
    # only uses "AC Flow". Scoping to AC must compare only "AC Flow"; the
    # instance-wide-only difference on "Other Flow" must NOT be flagged.
    src = dict(BASE)
    src["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "10001"}], "isLast": True}
    src["/rest/api/3/workflowscheme/project"] = _wfscheme_project(
        {"defaultWorkflow": "AC Flow", "issueTypeMappings": {}})
    src["/rest/api/3/workflow/search"] = {"values": [
        {"id": {"name": "AC Flow"},
         "transitions": [{"name": "Start"}, {"name": "Finish"}],
         "statuses": [{"id": "1"}, {"id": "2"}]},
        {"id": {"name": "Other Flow"},
         "transitions": [{"name": "A"}, {"name": "B"}],
         "statuses": [{"id": "1"}, {"id": "2"}]},
    ], "isLast": True}

    tgt = dict(BASE)
    tgt["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "20001"}], "isLast": True}
    tgt["/rest/api/3/workflowscheme/project"] = _wfscheme_project(
        {"defaultWorkflow": "AC Flow", "issueTypeMappings": {}})
    tgt["/rest/api/3/workflow/search"] = {"values": [
        # AC Flow matches src exactly -> no finding
        {"id": {"name": "AC Flow"},
         "transitions": [{"name": "Start"}, {"name": "Finish"}],
         "statuses": [{"id": "1"}, {"id": "2"}]},
        # Other Flow differs (only one transition) but is OUT OF SCOPE for AC
        {"id": {"name": "Other Flow"},
         "transitions": [{"name": "A"}],
         "statuses": [{"id": "1"}]},
    ], "isLast": True}

    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    wf = [f for f in out["findings"] if f["area"] == "workflows"]
    # No finding at all: AC Flow matches; Other Flow is out of scope.
    assert wf == [], wf
    assert out["areas"]["workflows"]["scope"] == "projects"


def test_workflows_scoped_still_flags_in_scope_structure_mismatch():
    # The scoped workflow itself differs structurally -> still flagged.
    src = dict(BASE)
    src["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "10001"}], "isLast": True}
    src["/rest/api/3/workflowscheme/project"] = _wfscheme_project(
        {"defaultWorkflow": "default",
         "issueTypeMappings": {"10000": "AC Flow"}})
    src["/rest/api/3/workflow/search"] = {"values": [
        {"id": {"name": "AC Flow"},
         "transitions": [{"name": "Start"}, {"name": "Finish"}],
         "statuses": [{"id": "1"}, {"id": "2"}]},
        {"id": {"name": "default"}, "transitions": [], "statuses": []},
        {"id": {"name": "Other Flow"},
         "transitions": [{"name": "X"}], "statuses": [{"id": "1"}]},
    ], "isLast": True}

    tgt = dict(BASE)
    tgt["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "20001"}], "isLast": True}
    tgt["/rest/api/3/workflowscheme/project"] = _wfscheme_project(
        {"defaultWorkflow": "default",
         "issueTypeMappings": {"10000": "AC Flow"}})
    tgt["/rest/api/3/workflow/search"] = {"values": [
        {"id": {"name": "AC Flow"},
         "transitions": [{"name": "Start"}],   # Finish missing
         "statuses": [{"id": "1"}]},
        {"id": {"name": "default"}, "transitions": [], "statuses": []},
    ], "isLast": True}

    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    wf = [f for f in out["findings"] if f["area"] == "workflows"]
    assert any(f["name"] == "AC Flow" and f["kind"] == "structure_mismatch"
               for f in wf), wf
    assert all(f["name"] != "Other Flow" for f in wf), wf
    assert out["areas"]["workflows"]["scope"] == "projects"


def test_workflows_scoped_in_scope_workflow_missing_in_tgt_still_flagged():
    # NO-FALSE-CLEAN: a workflow the SELECTED project uses on the source side
    # but that is entirely absent from the target must STILL be reported, even
    # though the target project's scheme cannot reference it. The source-side
    # scope resolution carries it into the in-scope union -> gap is caught.
    src = dict(BASE)
    src["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "10001"}], "isLast": True}
    src["/rest/api/3/workflowscheme/project"] = _wfscheme_project(
        {"defaultWorkflow": "default",
         "issueTypeMappings": {"10000": "AC Flow"}})
    src["/rest/api/3/workflow/search"] = {"values": [
        {"id": {"name": "AC Flow"}, "transitions": [], "statuses": []},
        {"id": {"name": "default"}, "transitions": [], "statuses": []},
    ], "isLast": True}

    tgt = dict(BASE)
    tgt["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "20001"}], "isLast": True}
    # target project's scheme only has "default" — "AC Flow" never migrated
    tgt["/rest/api/3/workflowscheme/project"] = _wfscheme_project(
        {"defaultWorkflow": "default", "issueTypeMappings": {}})
    tgt["/rest/api/3/workflow/search"] = {"values": [
        {"id": {"name": "default"}, "transitions": [], "statuses": []},
    ], "isLast": True}

    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    wf = [f for f in out["findings"] if f["area"] == "workflows"]
    assert any(f["name"] == "AC Flow" and f["kind"] == "missing_in_tgt"
               for f in wf), wf
    assert out["areas"]["workflows"]["scope"] == "projects"


def test_workflows_scope_resolution_failure_falls_back_to_instance():
    # The workflowscheme/project endpoint errors on the source side. Scoping
    # MUST fall back to instance-wide so a real gap is still caught, and the
    # area is marked scope="instance".
    note = []

    def src_handler(req):
        p = str(req.url.path)
        if p.endswith("/workflowscheme/project"):
            return httpx.Response(503, text="down")
        if p.endswith("/rest/api/3/project/search"):
            return httpx.Response(200, json={
                "values": [{"key": "AC", "id": "10001"}], "isLast": True})
        if p.endswith("/rest/api/3/workflow/search"):
            return httpx.Response(200, json={"values": [
                {"id": {"name": "AC Flow"}, "transitions": [], "statuses": []},
                {"id": {"name": "Lost Flow"}, "transitions": [],
                 "statuses": []}], "isLast": True})
        for suffix, payload in BASE.items():
            if p.endswith(suffix):
                return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"values": [], "isLast": True})

    def tgt_handler(req):
        p = str(req.url.path)
        if p.endswith("/workflowscheme/project"):
            return httpx.Response(503, text="down")
        if p.endswith("/rest/api/3/project/search"):
            return httpx.Response(200, json={
                "values": [{"key": "AC", "id": "20001"}], "isLast": True})
        if p.endswith("/rest/api/3/workflow/search"):
            # target is MISSING "Lost Flow" entirely
            return httpx.Response(200, json={"values": [
                {"id": {"name": "AC Flow"}, "transitions": [],
                 "statuses": []}], "isLast": True})
        for suffix, payload in BASE.items():
            if p.endswith(suffix):
                return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"values": [], "isLast": True})

    src_cl = mk(src_handler, "https://s.atlassian.net")
    tgt_cl = mk(tgt_handler, "https://t.atlassian.net")
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",),
                       progress=note.append)
    wf = [f for f in out["findings"] if f["area"] == "workflows"]
    # Fail-safe: the real instance-wide gap "Lost Flow" is STILL caught.
    assert any(f["name"] == "Lost Flow" and f["kind"] == "missing_in_tgt"
               for f in wf), wf
    assert out["areas"]["workflows"]["scope"] == "instance"
    # A loud note was emitted about the fallback
    assert any("scope resolution failed" in m and "workflows" in m
               for m in note), note


def test_workflows_empty_jsm_projects_unchanged_instance_wide():
    # No project scoping requested: behavior must be the legacy instance-wide
    # comparison (scope marked "instance"), still catching the gap.
    src = dict(BASE)
    src["/rest/api/3/workflow/search"] = {"values": [
        {"id": {"name": "AC Flow"}, "transitions": [], "statuses": []},
        {"id": {"name": "Lost Flow"}, "transitions": [], "statuses": []},
    ], "isLast": True}
    tgt = dict(BASE)
    tgt["/rest/api/3/workflow/search"] = {"values": [
        {"id": {"name": "AC Flow"}, "transitions": [], "statuses": []},
    ], "isLast": True}
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl)
    wf = [f for f in out["findings"] if f["area"] == "workflows"]
    assert [(f["name"], f["kind"]) for f in wf] == [
        ("Lost Flow", "missing_in_tgt")]
    assert out["areas"]["workflows"]["scope"] == "instance"


def test_global_areas_marked_scope_instance():
    # Genuinely-global areas are never narrowed; they carry scope="instance"
    # so the UI can show they weren't scoped — even when projects are selected.
    src_cl, tgt_cl = make_pair(dict(BASE), dict(BASE))
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    for area in ("priorities", "resolutions", "link_types", "statuses",
                 "issue_types", "roles"):
        assert out["areas"][area].get("scope") == "instance", area


# ── screens scoping ────────────────────────────────────────────────────────

def _scoped_screens_src():
    src = dict(BASE)
    src["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "10001"}], "isLast": True}
    # ITSS for the project
    src["/rest/api/3/issuetypescreenscheme/project"] = {
        "values": [{"issueTypeScreenScheme": {"id": "500"}}], "isLast": True}
    # ITSS -> screen scheme ids
    src["/rest/api/3/issuetypescreenscheme/mapping"] = {
        "values": [{"screenSchemeId": "700"}], "isLast": True}
    # screen scheme -> screen ids
    src["/rest/api/3/screenscheme"] = {"values": [
        {"id": "700", "screens": {"default": 1}}], "isLast": True}
    # global screens list (id -> name)
    src["/rest/api/3/screens"] = {"values": [
        {"id": 1, "name": "AC Screen"}, {"id": 2, "name": "Other Screen"}],
        "isLast": True}
    src["/rest/api/3/screens/1/tabs"] = [{"id": 10}]
    src["/rest/api/3/screens/1/tabs/10/fields"] = [{"name": "A"}, {"name": "B"}]
    src["/rest/api/3/screens/2/tabs"] = [{"id": 20}]
    src["/rest/api/3/screens/2/tabs/20/fields"] = [{"name": "X"}, {"name": "Y"}]
    return src


def test_screens_scoped_ignores_out_of_scope_field_diff():
    src = _scoped_screens_src()
    tgt = dict(BASE)
    tgt["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "20001"}], "isLast": True}
    tgt["/rest/api/3/issuetypescreenscheme/project"] = {
        "values": [{"issueTypeScreenScheme": {"id": "900"}}], "isLast": True}
    tgt["/rest/api/3/issuetypescreenscheme/mapping"] = {
        "values": [{"screenSchemeId": "950"}], "isLast": True}
    tgt["/rest/api/3/screenscheme"] = {"values": [
        {"id": "950", "screens": {"default": 91}}], "isLast": True}
    tgt["/rest/api/3/screens"] = {"values": [
        {"id": 91, "name": "AC Screen"}, {"id": 92, "name": "Other Screen"}],
        "isLast": True}
    tgt["/rest/api/3/screens/91/tabs"] = [{"id": 30}]
    # AC Screen matches src exactly (A, B)
    tgt["/rest/api/3/screens/91/tabs/30/fields"] = [{"name": "A"}, {"name": "B"}]
    tgt["/rest/api/3/screens/92/tabs"] = [{"id": 40}]
    # Other Screen DIFFERS (missing Y) but is OUT OF SCOPE for AC
    tgt["/rest/api/3/screens/92/tabs/40/fields"] = [{"name": "X"}]

    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    sc = [f for f in out["findings"] if f["area"] == "screens"]
    assert sc == [], sc
    assert out["areas"]["screens"]["scope"] == "projects"


def test_screens_scoped_still_flags_in_scope_field_mismatch():
    src = _scoped_screens_src()
    tgt = dict(BASE)
    tgt["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "20001"}], "isLast": True}
    tgt["/rest/api/3/issuetypescreenscheme/project"] = {
        "values": [{"issueTypeScreenScheme": {"id": "900"}}], "isLast": True}
    tgt["/rest/api/3/issuetypescreenscheme/mapping"] = {
        "values": [{"screenSchemeId": "950"}], "isLast": True}
    tgt["/rest/api/3/screenscheme"] = {"values": [
        {"id": "950", "screens": {"default": 91}}], "isLast": True}
    tgt["/rest/api/3/screens"] = {"values": [
        {"id": 91, "name": "AC Screen"}, {"id": 92, "name": "Other Screen"}],
        "isLast": True}
    tgt["/rest/api/3/screens/91/tabs"] = [{"id": 30}]
    # AC Screen is MISSING field B on target
    tgt["/rest/api/3/screens/91/tabs/30/fields"] = [{"name": "A"}]

    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    sc = [f for f in out["findings"] if f["area"] == "screens"]
    assert sc == [{"area": "screens", "name": "AC Screen",
                   "kind": "field_mismatch",
                   "detail": {"fields_missing_in_tgt": ["B"]}}], sc
    assert out["areas"]["screens"]["scope"] == "projects"


def test_screens_scope_resolution_failure_falls_back_to_instance():
    note = []
    src = _scoped_screens_src()
    # break ITSS resolution on the source side
    del src["/rest/api/3/issuetypescreenscheme/project"]

    def src_handler(req):
        p = str(req.url.path)
        if p.endswith("/issuetypescreenscheme/project"):
            return httpx.Response(503, text="down")
        for suffix, payload in src.items():
            if p.endswith(suffix):
                return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"values": [], "isLast": True})

    # target lacks "Other Screen" entirely -> a real instance-wide gap
    tgt = dict(BASE)
    tgt["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "20001"}], "isLast": True}
    tgt["/rest/api/3/screens"] = {"values": [
        {"id": 91, "name": "AC Screen"}], "isLast": True}
    tgt["/rest/api/3/screens/91/tabs"] = [{"id": 30}]
    tgt["/rest/api/3/screens/91/tabs/30/fields"] = [{"name": "A"}, {"name": "B"}]

    src_cl = mk(src_handler, "https://s.atlassian.net")
    _, tgt_cl = make_pair(dict(BASE), tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",),
                       progress=note.append)
    # Fail-safe: scope falls back to instance-wide.
    assert out["areas"]["screens"]["scope"] == "instance"
    assert any("scope resolution failed" in m and "screens" in m
               for m in note), note
    # the in-both screen (AC Screen, in both) is still deep-checked; the
    # instance-wide presence comparison is preserved (no crash, no false clean).
    assert "error" not in out["areas"]["screens"]


def test_workflows_scope_resolution_raised_exception_falls_back_to_instance():
    # When an exception is raised INSIDE _scoped_workflow_names (e.g. a
    # RuntimeError from a transport handler that bypasses httpx.HTTPError
    # catching, simulating a ClientError on a network blip), the audit must
    # NOT propagate — it must fall back to instance-wide and still catch the
    # real source-only workflow gap.
    note = []

    def src_handler(req):
        p = str(req.url.path)
        # The workflowscheme/project call raises a non-httpx exception
        # (simulates an unexpected error inside req() or all_projects()).
        if p.endswith("/workflowscheme/project"):
            raise RuntimeError("simulated non-httpx blip during scope resolution")
        if p.endswith("/rest/api/3/project/search"):
            return httpx.Response(200, json={
                "values": [{"key": "AC", "id": "10001"}], "isLast": True})
        if p.endswith("/rest/api/3/workflow/search"):
            return httpx.Response(200, json={"values": [
                {"id": {"name": "AC Flow"}, "transitions": [], "statuses": []},
                {"id": {"name": "Lost Flow"}, "transitions": [],
                 "statuses": []}], "isLast": True})
        for suffix, payload in BASE.items():
            if p.endswith(suffix):
                return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"values": [], "isLast": True})

    def tgt_handler(req):
        p = str(req.url.path)
        if p.endswith("/workflowscheme/project"):
            raise RuntimeError("simulated non-httpx blip during scope resolution")
        if p.endswith("/rest/api/3/project/search"):
            return httpx.Response(200, json={
                "values": [{"key": "AC", "id": "20001"}], "isLast": True})
        if p.endswith("/rest/api/3/workflow/search"):
            # target is MISSING "Lost Flow" entirely
            return httpx.Response(200, json={"values": [
                {"id": {"name": "AC Flow"}, "transitions": [],
                 "statuses": []}], "isLast": True})
        for suffix, payload in BASE.items():
            if p.endswith(suffix):
                return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"values": [], "isLast": True})

    src_cl = mk(src_handler, "https://s.atlassian.net")
    tgt_cl = mk(tgt_handler, "https://t.atlassian.net")

    # (1) audit_config must NOT raise even though the resolver raises internally.
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",),
                       progress=note.append)

    wf = [f for f in out["findings"] if f["area"] == "workflows"]

    # (2) The real source-only workflow gap "Lost Flow" is still caught
    #     (instance-wide fallback, not silently dropped).
    assert any(f["name"] == "Lost Flow" and f["kind"] == "missing_in_tgt"
               for f in wf), wf

    # (3) The area is marked instance-wide because scoping failed.
    assert out["areas"]["workflows"]["scope"] == "instance"

    # A loud note was emitted about the fallback.
    assert any("scope resolution failed" in m and "workflows" in m
               for m in note), note


# ── per-project scoping: schemes, statuses, issue-types (feat/config-scope) ──
# A side that resolves project AC -> id, then exposes the per-area scheme/status
# walk so the audit can narrow each global list to what AC actually uses.

def _scope_side(pid, *, wf_scheme=None, itss=None, screen_scheme=None,
                its_scheme=None, its_types=None, fcs=None, fc_configs=None,
                perm_scheme=None, notif_scheme=None, statuses=None):
    """Build a handler-data dict wiring up the project-scope resolution
    endpoints for a single project key 'AC' with numeric id ``pid``. Each kwarg,
    when given, populates that area's resolution chain. Unset chains return the
    empty default (resolution fails -> that area falls back instance-wide)."""
    d = {
        "/rest/api/3/project/search": {
            "values": [{"key": "AC", "id": pid}], "isLast": True},
    }
    if wf_scheme is not None:
        d["/rest/api/3/workflowscheme/project"] = {"values": [
            {"workflowScheme": wf_scheme}]}
    if itss is not None:
        # itss = (itss_id, itss_name, [(ss_id, ss_name, {screens})...])
        itss_id, itss_name, ss_list = itss
        d["/rest/api/3/issuetypescreenscheme/project"] = {"values": [
            {"issueTypeScreenScheme": {"id": itss_id, "name": itss_name}}],
            "isLast": True}
        d["/rest/api/3/issuetypescreenscheme/mapping"] = {
            "values": [{"screenSchemeId": ss_id} for ss_id, _n, _s in ss_list],
            "isLast": True}
        d["/rest/api/3/screenscheme"] = {"values": [
            {"id": ss_id, "name": ss_name, "screens": screens}
            for ss_id, ss_name, screens in ss_list], "isLast": True}
    if its_scheme is not None:
        # its_scheme = (id, name); its_types = [(typeId, name)...]
        sid, sname = its_scheme
        d["/rest/api/3/issuetypescheme/project"] = {"values": [
            {"issueTypeScheme": {"id": sid, "name": sname}}], "isLast": True}
        d["/rest/api/3/issuetypescheme/mapping"] = {"values": [
            {"issueTypeId": tid} for tid, _n in (its_types or [])],
            "isLast": True}
        d["/rest/api/3/issuetype"] = [{"id": tid, "name": n}
                                      for tid, n in (its_types or [])]
    if fcs is not None:
        sid, sname = fcs
        d["/rest/api/3/fieldconfigurationscheme/project"] = {"values": [
            {"fieldConfigurationScheme": {"id": sid, "name": sname}}],
            "isLast": True}
        d["/rest/api/3/fieldconfigurationscheme/mapping"] = {"values": [
            {"fieldConfigurationId": cid} for cid, _n in (fc_configs or [])],
            "isLast": True}
        d["/rest/api/3/fieldconfiguration"] = [
            {"id": cid, "name": n} for cid, n in (fc_configs or [])]
    if perm_scheme is not None:
        d["/rest/api/3/project/AC/permissionscheme"] = {"name": perm_scheme}
    if notif_scheme is not None:
        d["/rest/api/3/project/AC/notificationscheme"] = {"name": notif_scheme}
    if statuses is not None:
        d["/rest/api/3/project/AC/statuses"] = [
            {"id": "1", "statuses": [{"name": n} for n in statuses]}]
    return d


def _merge(*dicts):
    out = {}
    for d in dicts:
        out.update(d)
    return out


# ---- workflow_schemes ------------------------------------------------------

def test_workflow_schemes_scoped_ignores_out_of_scope_diff():
    src = _merge(dict(BASE), _scope_side("10001", wf_scheme={
        "name": "AC WF Scheme", "defaultWorkflow": "AC Flow",
        "issueTypeMappings": {}}))
    src["/rest/api/3/workflowscheme"] = {"values": [
        {"name": "AC WF Scheme"}, {"name": "Other WF Scheme"}], "isLast": True}
    tgt = _merge(dict(BASE), _scope_side("20001", wf_scheme={
        "name": "AC WF Scheme", "defaultWorkflow": "AC Flow",
        "issueTypeMappings": {}}))
    # target is MISSING "Other WF Scheme" — but it is OUT OF SCOPE for AC.
    tgt["/rest/api/3/workflowscheme"] = {"values": [
        {"name": "AC WF Scheme"}], "isLast": True}
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    assert out["areas"]["workflow_schemes"]["scope"] == "projects"
    assert [f for f in out["findings"]
            if f["area"] == "workflow_schemes"] == []


def test_workflow_schemes_scoped_in_scope_source_only_still_flagged():
    src = _merge(dict(BASE), _scope_side("10001", wf_scheme={
        "name": "AC WF Scheme", "defaultWorkflow": "AC Flow",
        "issueTypeMappings": {}}))
    src["/rest/api/3/workflowscheme"] = {"values": [
        {"name": "AC WF Scheme"}], "isLast": True}
    # target project scheme resolves but target's GLOBAL list lacks AC WF Scheme
    tgt = _merge(dict(BASE), _scope_side("20001", wf_scheme={
        "name": "AC WF Scheme", "defaultWorkflow": "AC Flow",
        "issueTypeMappings": {}}))
    tgt["/rest/api/3/workflowscheme"] = {"values": [], "isLast": True}
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    assert out["areas"]["workflow_schemes"]["scope"] == "projects"
    assert [(f["name"], f["kind"]) for f in out["findings"]
            if f["area"] == "workflow_schemes"] == [
        ("AC WF Scheme", "missing_in_tgt")]


def test_workflow_schemes_resolution_failure_falls_back_to_instance():
    note = []
    src = dict(BASE)
    # no workflowscheme/project wiring -> resolution fails
    src["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "10001"}], "isLast": True}
    src["/rest/api/3/workflowscheme"] = {"values": [
        {"name": "AC WF Scheme"}, {"name": "Lost Scheme"}], "isLast": True}
    tgt = dict(BASE)
    tgt["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "20001"}], "isLast": True}
    tgt["/rest/api/3/workflowscheme"] = {"values": [
        {"name": "AC WF Scheme"}], "isLast": True}
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",),
                       progress=note.append)
    assert out["areas"]["workflow_schemes"]["scope"] == "instance"
    # instance-wide fallback STILL catches the real gap
    assert any(f["name"] == "Lost Scheme" and f["kind"] == "missing_in_tgt"
               for f in out["findings"] if f["area"] == "workflow_schemes")
    assert any("scope resolution failed" in m and "workflow_schemes" in m
               for m in note), note


# ---- issuetype_screen_schemes & screen_schemes -----------------------------

def _scope_itss_side(pid):
    return _scope_side(pid, itss=("500", "AC ITSS", [
        ("700", "AC Screen Scheme", {"default": 1})]))


def test_issuetype_screen_schemes_scoped_ignores_out_of_scope():
    src = _merge(dict(BASE), _scope_itss_side("10001"))
    src["/rest/api/3/issuetypescreenscheme"] = {"values": [
        {"name": "AC ITSS"}, {"name": "Other ITSS"}], "isLast": True}
    tgt = _merge(dict(BASE), _scope_itss_side("20001"))
    tgt["/rest/api/3/issuetypescreenscheme"] = {"values": [
        {"name": "AC ITSS"}], "isLast": True}   # missing Other ITSS (out of scope)
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    assert out["areas"]["issuetype_screen_schemes"]["scope"] == "projects"
    assert [f for f in out["findings"]
            if f["area"] == "issuetype_screen_schemes"] == []


def test_issuetype_screen_schemes_in_scope_source_only_flagged():
    src = _merge(dict(BASE), _scope_itss_side("10001"))
    src["/rest/api/3/issuetypescreenscheme"] = {"values": [
        {"name": "AC ITSS"}], "isLast": True}
    tgt = _merge(dict(BASE), _scope_itss_side("20001"))
    tgt["/rest/api/3/issuetypescreenscheme"] = {"values": [], "isLast": True}
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    assert [(f["name"], f["kind"]) for f in out["findings"]
            if f["area"] == "issuetype_screen_schemes"] == [
        ("AC ITSS", "missing_in_tgt")]
    assert out["areas"]["issuetype_screen_schemes"]["scope"] == "projects"


def test_screen_schemes_scoped_ignores_out_of_scope():
    src = _merge(dict(BASE), _scope_itss_side("10001"))
    src["/rest/api/3/screenscheme"] = {"values": [
        {"id": "700", "name": "AC Screen Scheme", "screens": {"default": 1}}],
        "isLast": True}
    # NOTE: /screenscheme is consulted twice (scoping walk uses ?id=700; the
    # SIMPLE global list uses the bare path). The suffix-matching handler serves
    # the same payload for both, which contains exactly AC Screen Scheme.
    tgt = _merge(dict(BASE), _scope_itss_side("20001"))
    tgt["/rest/api/3/screenscheme"] = {"values": [
        {"id": "700", "name": "AC Screen Scheme", "screens": {"default": 1}}],
        "isLast": True}
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    assert out["areas"]["screen_schemes"]["scope"] == "projects"
    assert [f for f in out["findings"]
            if f["area"] == "screen_schemes"] == []


def test_screen_schemes_resolution_failure_falls_back_to_instance():
    note = []
    src = dict(BASE)
    src["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "10001"}], "isLast": True}
    # no ITSS wiring -> screen-scheme scoping fails
    src["/rest/api/3/screenscheme"] = {"values": [
        {"name": "AC Screen Scheme"}, {"name": "Lost Screen Scheme"}],
        "isLast": True}
    tgt = dict(BASE)
    tgt["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "20001"}], "isLast": True}
    tgt["/rest/api/3/screenscheme"] = {"values": [
        {"name": "AC Screen Scheme"}], "isLast": True}
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",),
                       progress=note.append)
    assert out["areas"]["screen_schemes"]["scope"] == "instance"
    assert any(f["name"] == "Lost Screen Scheme" and f["kind"] == "missing_in_tgt"
               for f in out["findings"] if f["area"] == "screen_schemes")
    assert any("scope resolution failed" in m and "screen_schemes" in m
               for m in note), note


# ---- issuetype_schemes & issue_types ---------------------------------------

def _scope_its_side(pid):
    return _scope_side(pid, its_scheme=("400", "AC IT Scheme"),
                       its_types=[("10000", "Task"), ("10001", "Bug")])


def test_issuetype_schemes_scoped_ignores_out_of_scope():
    src = _merge(dict(BASE), _scope_its_side("10001"))
    src["/rest/api/3/issuetypescheme"] = {"values": [
        {"name": "AC IT Scheme"}, {"name": "Other IT Scheme"}], "isLast": True}
    tgt = _merge(dict(BASE), _scope_its_side("20001"))
    tgt["/rest/api/3/issuetypescheme"] = {"values": [
        {"name": "AC IT Scheme"}], "isLast": True}   # Other out of scope
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    assert out["areas"]["issuetype_schemes"]["scope"] == "projects"
    assert [f for f in out["findings"]
            if f["area"] == "issuetype_schemes"] == []


def test_issuetype_schemes_in_scope_source_only_flagged():
    src = _merge(dict(BASE), _scope_its_side("10001"))
    src["/rest/api/3/issuetypescheme"] = {"values": [
        {"name": "AC IT Scheme"}], "isLast": True}
    tgt = _merge(dict(BASE), _scope_its_side("20001"))
    tgt["/rest/api/3/issuetypescheme"] = {"values": [], "isLast": True}
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    assert [(f["name"], f["kind"]) for f in out["findings"]
            if f["area"] == "issuetype_schemes"] == [
        ("AC IT Scheme", "missing_in_tgt")]


def test_issue_types_scoped_to_project_types():
    # AC's scheme uses Task + Bug. The instance has Task, Bug, Epic; src/tgt
    # differ on Epic (out of scope) -> not flagged. A source-only IN-scope type
    # (Bug missing in tgt's global list) IS flagged.
    # Source: scheme maps to Task(10000)+Bug(10001); global list adds Epic.
    src = _merge(dict(BASE), _scope_its_side("10001"))
    src["/rest/api/3/issuetype"] = [
        {"id": "10000", "name": "Task"}, {"id": "10001", "name": "Bug"},
        {"id": "10002", "name": "Epic"}]
    # Target: scheme maps to the TARGET's own ids for Task(20000)+Bug(20001),
    # but the target's GLOBAL list is MISSING Bug (only Task + Epic) -> the
    # in-scope union (Task, Bug) is compared and Bug is a real source-only gap.
    tgt = _merge(dict(BASE), _scope_side(
        "20001", its_scheme=("400", "AC IT Scheme"),
        its_types=[("20000", "Task"), ("20001", "Bug")]))
    tgt["/rest/api/3/issuetype"] = [
        {"id": "20000", "name": "Task"}, {"id": "20002", "name": "Epic"}]
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    assert out["areas"]["issue_types"]["scope"] == "projects"
    its = [(f["name"], f["kind"]) for f in out["findings"]
           if f["area"] == "issue_types"]
    assert ("Bug", "missing_in_tgt") in its
    assert all(n != "Epic" for n, _k in its), its


def test_issue_types_resolution_failure_falls_back_to_instance():
    note = []
    src = dict(BASE)
    src["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "10001"}], "isLast": True}
    # no issuetypescheme/project wiring -> resolution fails
    src["/rest/api/3/issuetype"] = [{"name": "Task"}, {"name": "Lost Type"}]
    tgt = dict(BASE)
    tgt["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "20001"}], "isLast": True}
    tgt["/rest/api/3/issuetype"] = [{"name": "Task"}]
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",),
                       progress=note.append)
    assert out["areas"]["issue_types"]["scope"] == "instance"
    assert any(f["name"] == "Lost Type" and f["kind"] == "missing_in_tgt"
               for f in out["findings"] if f["area"] == "issue_types")
    assert any("scope resolution failed" in m and "issue_types" in m
               for m in note), note


# ---- field_config_schemes & field_configurations --------------------------

def _scope_fc_side(pid):
    return _scope_side(pid, fcs=("300", "AC FC Scheme"),
                       fc_configs=[("8000", "AC Field Config")])


def test_field_config_schemes_scoped_ignores_out_of_scope():
    src = _merge(dict(BASE), _scope_fc_side("10001"))
    src["/rest/api/3/fieldconfigurationscheme"] = {"values": [
        {"name": "AC FC Scheme"}, {"name": "Other FC Scheme"}], "isLast": True}
    tgt = _merge(dict(BASE), _scope_fc_side("20001"))
    tgt["/rest/api/3/fieldconfigurationscheme"] = {"values": [
        {"name": "AC FC Scheme"}], "isLast": True}
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    assert out["areas"]["field_config_schemes"]["scope"] == "projects"
    assert [f for f in out["findings"]
            if f["area"] == "field_config_schemes"] == []


def test_field_configurations_scoped_source_only_flagged():
    # Source AC scheme uses BOTH "Shared Config"(8000) and "AC Field
    # Config"(8001); global list also has out-of-scope "Other Config"(8002).
    # Target AC scheme uses only "Shared Config"(9000) — "AC Field Config" never
    # migrated. The in-scope union (Shared + AC Field) is compared; AC Field
    # Config is a real source-only gap; Other Config (out of scope) is not.
    src = _merge(dict(BASE), _scope_side(
        "10001", fcs=("300", "AC FC Scheme"),
        fc_configs=[("8000", "Shared Config"), ("8001", "AC Field Config")]))
    src["/rest/api/3/fieldconfiguration"] = [
        {"id": "8000", "name": "Shared Config"},
        {"id": "8001", "name": "AC Field Config"},
        {"id": "8002", "name": "Other Config"}]
    tgt = _merge(dict(BASE), _scope_side(
        "20001", fcs=("300", "AC FC Scheme"),
        fc_configs=[("9000", "Shared Config")]))
    tgt["/rest/api/3/fieldconfiguration"] = [{"id": "9000",
                                              "name": "Shared Config"}]
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    assert out["areas"]["field_configurations"]["scope"] == "projects"
    fc = [(f["name"], f["kind"]) for f in out["findings"]
          if f["area"] == "field_configurations"]
    assert fc == [("AC Field Config", "missing_in_tgt")]


def test_field_config_schemes_resolution_failure_falls_back_to_instance():
    note = []
    src = dict(BASE)
    src["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "10001"}], "isLast": True}
    src["/rest/api/3/fieldconfigurationscheme"] = {"values": [
        {"name": "AC FC Scheme"}, {"name": "Lost FC Scheme"}], "isLast": True}
    tgt = dict(BASE)
    tgt["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "20001"}], "isLast": True}
    tgt["/rest/api/3/fieldconfigurationscheme"] = {"values": [
        {"name": "AC FC Scheme"}], "isLast": True}
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",),
                       progress=note.append)
    assert out["areas"]["field_config_schemes"]["scope"] == "instance"
    assert any(f["name"] == "Lost FC Scheme" and f["kind"] == "missing_in_tgt"
               for f in out["findings"] if f["area"] == "field_config_schemes")
    assert any("scope resolution failed" in m and "field_config_schemes" in m
               for m in note), note


# ---- permission_schemes ----------------------------------------------------

def test_permission_schemes_scoped_ignores_out_of_scope():
    src = _merge(dict(BASE), _scope_side("10001", perm_scheme="AC Perms"))
    src["/rest/api/3/permissionscheme"] = {"permissionSchemes": [
        {"name": "AC Perms"}, {"name": "Other Perms"}]}
    tgt = _merge(dict(BASE), _scope_side("20001", perm_scheme="AC Perms"))
    tgt["/rest/api/3/permissionscheme"] = {"permissionSchemes": [
        {"name": "AC Perms"}]}   # Other Perms missing but out of scope
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    assert out["areas"]["permission_schemes"]["scope"] == "projects"
    assert [f for f in out["findings"]
            if f["area"] == "permission_schemes"] == []


def test_permission_schemes_in_scope_source_only_flagged():
    src = _merge(dict(BASE), _scope_side("10001", perm_scheme="AC Perms"))
    src["/rest/api/3/permissionscheme"] = {"permissionSchemes": [
        {"name": "AC Perms"}]}
    tgt = _merge(dict(BASE), _scope_side("20001", perm_scheme="AC Perms"))
    tgt["/rest/api/3/permissionscheme"] = {"permissionSchemes": []}
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    assert [(f["name"], f["kind"]) for f in out["findings"]
            if f["area"] == "permission_schemes"] == [
        ("AC Perms", "missing_in_tgt")]


def test_permission_schemes_resolution_failure_falls_back_to_instance():
    note = []

    def src_handler(req):
        p = str(req.url.path)
        if p.endswith("/project/AC/permissionscheme"):
            return httpx.Response(503, text="down")
        if p.endswith("/rest/api/3/project/search"):
            return httpx.Response(200, json={
                "values": [{"key": "AC", "id": "10001"}], "isLast": True})
        if p.endswith("/rest/api/3/permissionscheme"):
            return httpx.Response(200, json={"permissionSchemes": [
                {"name": "AC Perms"}, {"name": "Lost Perms"}]})
        for suffix, payload in BASE.items():
            if p.endswith(suffix):
                return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"values": [], "isLast": True})

    def tgt_handler(req):
        p = str(req.url.path)
        if p.endswith("/rest/api/3/project/search"):
            return httpx.Response(200, json={
                "values": [{"key": "AC", "id": "20001"}], "isLast": True})
        if p.endswith("/project/AC/permissionscheme"):
            return httpx.Response(200, json={"name": "AC Perms"})
        if p.endswith("/rest/api/3/permissionscheme"):
            return httpx.Response(200, json={"permissionSchemes": [
                {"name": "AC Perms"}]})
        for suffix, payload in BASE.items():
            if p.endswith(suffix):
                return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"values": [], "isLast": True})

    src_cl = mk(src_handler, "https://s.atlassian.net")
    tgt_cl = mk(tgt_handler, "https://t.atlassian.net")
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",),
                       progress=note.append)
    assert out["areas"]["permission_schemes"]["scope"] == "instance"
    assert any(f["name"] == "Lost Perms" and f["kind"] == "missing_in_tgt"
               for f in out["findings"] if f["area"] == "permission_schemes")
    assert any("scope resolution failed" in m and "permission_schemes" in m
               for m in note), note


# ---- notification_schemes --------------------------------------------------

def test_notification_schemes_scoped_ignores_out_of_scope():
    src = _merge(dict(BASE), _scope_side("10001", notif_scheme="AC Notifs"))
    src["/rest/api/3/notificationscheme"] = {"values": [
        {"name": "AC Notifs"}, {"name": "Other Notifs"}], "isLast": True}
    tgt = _merge(dict(BASE), _scope_side("20001", notif_scheme="AC Notifs"))
    tgt["/rest/api/3/notificationscheme"] = {"values": [
        {"name": "AC Notifs"}], "isLast": True}
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    assert out["areas"]["notification_schemes"]["scope"] == "projects"
    assert [f for f in out["findings"]
            if f["area"] == "notification_schemes"] == []


def test_notification_schemes_in_scope_source_only_flagged():
    src = _merge(dict(BASE), _scope_side("10001", notif_scheme="AC Notifs"))
    src["/rest/api/3/notificationscheme"] = {"values": [
        {"name": "AC Notifs"}], "isLast": True}
    tgt = _merge(dict(BASE), _scope_side("20001", notif_scheme="AC Notifs"))
    tgt["/rest/api/3/notificationscheme"] = {"values": [], "isLast": True}
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    assert [(f["name"], f["kind"]) for f in out["findings"]
            if f["area"] == "notification_schemes"] == [
        ("AC Notifs", "missing_in_tgt")]


# ---- statuses --------------------------------------------------------------

def test_statuses_scoped_to_project_statuses():
    # AC's workflows use Open + In Progress. Instance has Open, In Progress,
    # Done; src/tgt differ on Done (out of scope) -> not flagged. In Progress
    # is a real in-scope source-only gap -> flagged.
    src = _merge(dict(BASE), _scope_side(
        "10001", statuses=["Open", "In Progress"]))
    src["/rest/api/3/status"] = [
        {"name": "Open"}, {"name": "In Progress"}, {"name": "Done"}]
    tgt = _merge(dict(BASE), _scope_side(
        "20001", statuses=["Open", "In Progress"]))
    # tgt global statuses: Open, Done (In Progress missing -> in-scope gap;
    # Done present here but out of scope either way).
    tgt["/rest/api/3/status"] = [{"name": "Open"}, {"name": "Done"}]
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    assert out["areas"]["statuses"]["scope"] == "projects"
    st = [(f["name"], f["kind"]) for f in out["findings"]
          if f["area"] == "statuses"]
    assert ("In Progress", "missing_in_tgt") in st
    assert all(n != "Done" for n, _k in st), st


def test_statuses_resolution_failure_falls_back_to_instance():
    note = []

    def src_handler(req):
        p = str(req.url.path)
        if p.endswith("/project/AC/statuses"):
            return httpx.Response(503, text="down")
        if p.endswith("/rest/api/3/project/search"):
            return httpx.Response(200, json={
                "values": [{"key": "AC", "id": "10001"}], "isLast": True})
        if p.endswith("/rest/api/3/status"):
            return httpx.Response(200, json=[
                {"name": "Open"}, {"name": "Lost Status"}])
        for suffix, payload in BASE.items():
            if p.endswith(suffix):
                return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"values": [], "isLast": True})

    def tgt_handler(req):
        p = str(req.url.path)
        if p.endswith("/rest/api/3/project/search"):
            return httpx.Response(200, json={
                "values": [{"key": "AC", "id": "20001"}], "isLast": True})
        if p.endswith("/project/AC/statuses"):
            return httpx.Response(200, json=[
                {"id": "1", "statuses": [{"name": "Open"}]}])
        if p.endswith("/rest/api/3/status"):
            return httpx.Response(200, json=[{"name": "Open"}])
        for suffix, payload in BASE.items():
            if p.endswith(suffix):
                return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"values": [], "isLast": True})

    src_cl = mk(src_handler, "https://s.atlassian.net")
    tgt_cl = mk(tgt_handler, "https://t.atlassian.net")
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",),
                       progress=note.append)
    assert out["areas"]["statuses"]["scope"] == "instance"
    assert any(f["name"] == "Lost Status" and f["kind"] == "missing_in_tgt"
               for f in out["findings"] if f["area"] == "statuses")
    assert any("scope resolution failed" in m and "statuses" in m
               for m in note), note


# ---- custom_fields context scoping -----------------------------------------

def _cf_field(fid, name, ctx_by_project):
    """ctx_by_project: dict projectId -> list of contexts returned by
    /field/{fid}/context?projectId=..."""
    return fid, name, ctx_by_project


def _cf_handler(data, fields, ctx_map):
    """Handler that also answers /field/{fid}/context?projectId=PID from
    ctx_map[(fid, pid)] -> {"values": [...]}; unknown -> empty."""
    def handler(req):
        p = str(req.url.path)
        if "/context" in p and "/field/" in p:
            fid = p.split("/field/")[1].split("/context")[0]
            pid = req.url.params.get("projectId")
            vals = ctx_map.get((fid, pid), [])
            return httpx.Response(200, json={"values": vals, "isLast": True})
        for suffix, payload in data.items():
            if p.endswith(suffix):
                return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"values": [], "isLast": True})
    return handler


def test_custom_field_global_context_in_scope():
    # Field with a GLOBAL context (returned for the selected project) is in
    # scope; an OUT-OF-SCOPE field (no context for AC) is excluded.
    src = dict(BASE)
    src["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "10001"}], "isLast": True}
    src["/rest/api/3/field"] = [
        {"name": "Global Field", "id": "customfield_1", "custom": True,
         "schema": {"custom": "x:textfield"}},
        {"name": "Other Field", "id": "customfield_2", "custom": True,
         "schema": {"custom": "x:textfield"}},
    ]
    # AC sees a context for Global Field, none for Other Field.
    src_ctx = {("customfield_1", "10001"): [{"id": "g1", "isGlobalContext": True}]}
    src_h = _cf_handler(src, src["/rest/api/3/field"], src_ctx)

    tgt = dict(BASE)
    tgt["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "20001"}], "isLast": True}
    # target lacks Global Field entirely -> in-scope source-only gap
    tgt["/rest/api/3/field"] = []
    tgt_h = _cf_handler(tgt, [], {})

    src_cl = mk(src_h, "https://s.atlassian.net")
    tgt_cl = mk(tgt_h, "https://t.atlassian.net")
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    assert out["areas"]["custom_fields"]["scope"] == "projects"
    miss = [f["name"] for f in out["findings"]
            if f["area"] == "custom_fields" and f["kind"] == "missing_in_tgt"]
    # Global Field is in scope and missing in target -> flagged.
    assert "Global Field" in miss
    # Other Field is OUT of scope for AC -> never flagged.
    assert "Other Field" not in miss


def test_custom_field_project_context_in_scope():
    # A field whose context is specific to the selected project is in scope.
    src = dict(BASE)
    src["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "10001"}], "isLast": True}
    src["/rest/api/3/field"] = [
        {"name": "AC Field", "id": "customfield_1", "custom": True,
         "schema": {"custom": "x:textfield"}},
    ]
    src_ctx = {("customfield_1", "10001"): [{"id": "p1"}]}
    src_h = _cf_handler(src, src["/rest/api/3/field"], src_ctx)
    tgt = dict(BASE)
    tgt["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "20001"}], "isLast": True}
    tgt["/rest/api/3/field"] = []
    tgt_h = _cf_handler(tgt, [], {})
    src_cl = mk(src_h, "https://s.atlassian.net")
    tgt_cl = mk(tgt_h, "https://t.atlassian.net")
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    assert out["areas"]["custom_fields"]["scope"] == "projects"
    assert any(f["name"] == "AC Field" and f["kind"] == "missing_in_tgt"
               for f in out["findings"] if f["area"] == "custom_fields")


def test_custom_field_other_project_context_out_of_scope():
    # A field whose ONLY context is for OTHER projects (no context returned for
    # the selected project AC) is OUT of scope -> a target diff is not flagged.
    src = dict(BASE)
    src["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "10001"}], "isLast": True}
    src["/rest/api/3/field"] = [
        {"name": "Foreign Field", "id": "customfield_1", "custom": True,
         "schema": {"custom": "x:select"}},
    ]
    # No ctx entry for ("customfield_1", "10001") -> AC sees no context.
    src_h = _cf_handler(src, src["/rest/api/3/field"], {})
    tgt = dict(BASE)
    tgt["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "20001"}], "isLast": True}
    # tgt lacks the field entirely -> would be a gap IF in scope; must NOT be.
    tgt["/rest/api/3/field"] = []
    tgt_h = _cf_handler(tgt, [], {})
    src_cl = mk(src_h, "https://s.atlassian.net")
    tgt_cl = mk(tgt_h, "https://t.atlassian.net")
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    assert out["areas"]["custom_fields"]["scope"] == "projects"
    assert all(f["name"] != "Foreign Field" for f in out["findings"]
               if f["area"] == "custom_fields"), out["findings"]


def test_custom_field_per_field_context_error_keeps_field_in_scope():
    # If the context check ERRORS for a field, that field must stay IN scope
    # (never silently dropped) so a real gap is still caught.
    src = dict(BASE)
    src["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "10001"}], "isLast": True}
    src["/rest/api/3/field"] = [
        {"name": "Erroring Field", "id": "customfield_1", "custom": True,
         "schema": {"custom": "x:textfield"}},
    ]

    def src_handler(req):
        p = str(req.url.path)
        if "/field/customfield_1/context" in p:
            return httpx.Response(503, text="down")   # context check errors
        if p.endswith("/rest/api/3/project/search"):
            return httpx.Response(200, json={
                "values": [{"key": "AC", "id": "10001"}], "isLast": True})
        for suffix, payload in src.items():
            if p.endswith(suffix):
                return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"values": [], "isLast": True})

    tgt = dict(BASE)
    tgt["/rest/api/3/project/search"] = {
        "values": [{"key": "AC", "id": "20001"}], "isLast": True}
    tgt["/rest/api/3/field"] = []   # target lacks the field -> real gap
    tgt_h = _cf_handler(tgt, [], {})
    src_cl = mk(src_handler, "https://s.atlassian.net")
    tgt_cl = mk(tgt_h, "https://t.atlassian.net")
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    # scope still resolved (project-id ok), field kept in scope by fail-safe.
    assert out["areas"]["custom_fields"]["scope"] == "projects"
    assert any(f["name"] == "Erroring Field" and f["kind"] == "missing_in_tgt"
               for f in out["findings"] if f["area"] == "custom_fields")


def test_custom_field_scope_resolution_failure_falls_back_to_instance():
    # Project-id resolution fails (no project/search, no /project/AC) -> the
    # whole custom_fields area reverts to instance-wide and still catches gaps.
    note = []
    src = dict(BASE)
    src["/rest/api/3/field"] = [
        {"name": "Lost Field", "id": "customfield_1", "custom": True,
         "schema": {"custom": "x:textfield"}}]

    def src_handler(req):
        p = str(req.url.path)
        if p.endswith("/rest/api/3/project/AC") or \
                p.endswith("/rest/api/3/project/search") or \
                p.endswith("/rest/api/3/project"):
            return httpx.Response(503, text="down")   # cannot resolve project id
        for suffix, payload in src.items():
            if p.endswith(suffix):
                return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"values": [], "isLast": True})

    tgt = dict(BASE)
    tgt["/rest/api/3/field"] = []
    src_cl = mk(src_handler, "https://s.atlassian.net")
    _, tgt_cl = make_pair(dict(BASE), tgt)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",),
                       progress=note.append)
    assert out["areas"]["custom_fields"]["scope"] == "instance"
    # instance-wide fallback still flags the real gap
    assert any(f["name"] == "Lost Field" and f["kind"] == "missing_in_tgt"
               for f in out["findings"] if f["area"] == "custom_fields")
    assert any("scope resolution failed" in m and "custom_fields" in m
               for m in note), note


# ---- empty jsm_projects + DC unchanged for new areas ----------------------

def test_new_areas_empty_jsm_projects_unchanged_instance_wide():
    # With NO selected projects, every newly-scoped area stays instance-wide and
    # still catches gaps (legacy behavior).
    src = dict(BASE)
    src["/rest/api/3/workflowscheme"] = {"values": [
        {"name": "A"}, {"name": "Lost Scheme"}], "isLast": True}
    src["/rest/api/3/permissionscheme"] = {"permissionSchemes": [
        {"name": "P"}, {"name": "Lost Perms"}]}
    src["/rest/api/3/status"] = [{"name": "Open"}, {"name": "Lost Status"}]
    tgt = dict(BASE)
    tgt["/rest/api/3/workflowscheme"] = {"values": [{"name": "A"}],
                                         "isLast": True}
    tgt["/rest/api/3/permissionscheme"] = {"permissionSchemes": [{"name": "P"}]}
    tgt["/rest/api/3/status"] = [{"name": "Open"}]
    src_cl, tgt_cl = make_pair(src, tgt)
    out = audit_config(src_cl, tgt_cl)   # no jsm_projects
    for area, lost in (("workflow_schemes", "Lost Scheme"),
                       ("permission_schemes", "Lost Perms"),
                       ("statuses", "Lost Status")):
        assert out["areas"][area]["scope"] == "instance"
        assert any(f["name"] == lost and f["kind"] == "missing_in_tgt"
                   for f in out["findings"] if f["area"] == area), (area, lost)


def test_new_areas_dc_side_unchanged_instance_wide():
    # A DC side must leave ALL areas instance-wide even with projects selected
    # (no per-project scheme REST surface on DC). The scopable DC-capable areas
    # (statuses, issue_types, permission_schemes, notification_schemes) stay
    # instance-wide; Cloud-only scheme areas remain skipped.
    src_data = dict(DC_BASE)
    src_data["/rest/api/2/status"] = [{"name": "Open"}, {"name": "Lost Status"}]
    src_data["/rest/api/2/permissionscheme"] = {"permissionSchemes": [
        {"name": "P"}, {"name": "Lost Perms"}]}
    tgt_data = dict(BASE)
    tgt_data["/rest/api/3/status"] = [{"name": "Open"}]
    tgt_data["/rest/api/3/permissionscheme"] = {"permissionSchemes": [
        {"name": "P"}]}
    src_cl, tgt_cl, _, _ = make_dc_cloud_pair(src_data, tgt_data)
    out = audit_config(src_cl, tgt_cl, jsm_projects=("AC",))
    assert out["areas"]["statuses"]["scope"] == "instance"
    assert out["areas"]["permission_schemes"]["scope"] == "instance"
    gaps = {(f["area"], f["name"]) for f in out["findings"]
            if f["kind"] == "missing_in_tgt"}
    assert ("statuses", "Lost Status") in gaps
    assert ("permission_schemes", "Lost Perms") in gaps
