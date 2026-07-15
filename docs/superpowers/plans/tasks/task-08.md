### Task 8: `auditor/config_audit.py` — instance config parity

**Files:**
- Create: `auditor/config_audit.py`
- Test: `tests/test_config_audit.py`

Port of `config_audit.py` with the `config_fix.py` corrections folded in: correct select-type detection (`select|radio|checkbox|cascading`), servicedeskapi `start/limit` pagination via `client.sd_list`. Emits config findings + per-area summaries.

- [ ] **Step 1: Write the failing tests**

`tests/test_config_audit.py`:
```python
import httpx
from auditor.client import Connection, JiraClient
from auditor.config_audit import audit_config


def mk(handler, site):
    conn = Connection(auth_type="pat", site_url=site, email="e", api_token="t")
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_config_audit.py -q`
Expected: `ModuleNotFoundError: No module named 'auditor.config_audit'`.

- [ ] **Step 3: Write the implementation**

`auditor/config_audit.py`:
```python
"""Instance configuration parity audit (port of config_audit.py + config_fix.py).

Compares EVERY config object by NAME (IDs are re-minted by migration):
simple dimensions, custom fields (type + select options), workflows
(structure), screens (deep field check, capped), and JSM request types +
queues per selected project. Emits spec-shaped config findings.
"""
from __future__ import annotations

from typing import Callable

from .client import JiraClient

SIMPLE = [
    ("statuses", "/rest/api/3/status", None),
    ("issue_types", "/rest/api/3/issuetype", None),
    ("priorities", "/rest/api/3/priority", None),
    ("resolutions", "/rest/api/3/resolution", None),
    ("link_types", "/rest/api/3/issueLinkType", "issueLinkTypes"),
    ("roles", "/rest/api/3/role", None),
    ("screens", "/rest/api/3/screens", None),
    ("screen_schemes", "/rest/api/3/screenscheme", None),
    ("issuetype_screen_schemes", "/rest/api/3/issuetypescreenscheme", None),
    ("workflow_schemes", "/rest/api/3/workflowscheme", None),
    ("issuetype_schemes", "/rest/api/3/issuetypescheme", None),
    ("field_configurations", "/rest/api/3/fieldconfiguration", None),
    ("field_config_schemes", "/rest/api/3/fieldconfigurationscheme", None),
    ("permission_schemes", "/rest/api/3/permissionscheme", "permissionSchemes"),
    ("notification_schemes", "/rest/api/3/notificationscheme", None),
]
_SELECT_MARKERS = ("select", "radio", "checkbox", "cascading")
_SCREEN_DEEP_CAP = 60
_OPTION_CONTEXT_CAP = 3


def _names(items, fn=lambda x: x.get("name")):
    return [fn(i) for i in (items or []) if fn(i)]


def _summary(label, s_names, t_names):
    s, t = set(s_names), set(t_names)
    return {"label": label, "src": len(s), "tgt": len(t),
            "in_both": len(s & t), "source_only": sorted(s - t),
            "target_only_count": len(t - s)}


def _field_options(client: JiraClient, fid: str) -> set:
    opts = []
    ctx, _ = client.paginate_start_at(f"/rest/api/3/field/{fid}/context")
    for c in (ctx or [])[:_OPTION_CONTEXT_CAP]:
        o, _ = client.paginate_start_at(
            f"/rest/api/3/field/{fid}/context/{c['id']}/option")
        opts += _names(o or [], lambda x: x.get("value"))
    return set(opts)


def _screen_fields(client: JiraClient, sid) -> set:
    out = []
    tabs, _ = client.paginate_start_at(f"/rest/api/3/screens/{sid}/tabs")
    for tb in (tabs or []):
        st, flds = client.req(f"/rest/api/3/screens/{sid}/tabs/{tb['id']}/fields")
        if st == 200 and isinstance(flds, list):
            out += _names(flds)
    return set(out)


def _wf_name(w):
    return (w.get("id") or {}).get("name") if isinstance(w.get("id"), dict) \
        else w.get("name")


def audit_config(src: JiraClient, tgt: JiraClient, jsm_projects=(),
                 progress: Callable[[str], None] | None = None) -> dict:
    areas: dict = {}
    findings: list[dict] = []
    say = progress or (lambda m: None)

    # ---- simple dimensions
    for area, path, key in SIMPLE:
        s, se = src.paginate_start_at(path, key=key)
        t, te = tgt.paginate_start_at(path, key=key)
        summ = _summary(area, _names(s), _names(t))
        if se or te:
            summ["error"] = f"src={se} tgt={te}"
        areas[area] = summ
        for name in summ["source_only"]:
            findings.append({"area": area, "name": name,
                             "kind": "missing_in_tgt", "detail": {}})
        say(f"[{area}] src={summ['src']} tgt={summ['tgt']} "
            f"source-only={len(summ['source_only'])}")

    # ---- custom fields: presence + type + select options
    sf, _ = src.paginate_start_at("/rest/api/3/field")
    tf, _ = tgt.paginate_start_at("/rest/api/3/field")
    scustom = {f["name"]: f for f in (sf or []) if f.get("custom")}
    tcustom = {f["name"]: f for f in (tf or []) if f.get("custom")}
    summ = _summary("custom_fields", scustom.keys(), tcustom.keys())
    for name in summ["source_only"]:
        findings.append({"area": "custom_fields", "name": name,
                         "kind": "missing_in_tgt",
                         "detail": {"type": str((scustom[name].get("schema") or {})
                                                .get("custom", "")).split(":")[-1]}})
    checked = 0
    for name in sorted(set(scustom) & set(tcustom)):
        s_type = str((scustom[name].get("schema") or {}).get("custom", "")).split(":")[-1]
        t_type = str((tcustom[name].get("schema") or {}).get("custom", "")).split(":")[-1]
        if s_type != t_type:
            findings.append({"area": "custom_fields", "name": name,
                             "kind": "type_mismatch",
                             "detail": {"src_type": s_type, "tgt_type": t_type}})
        ct = str((scustom[name].get("schema") or {}).get("custom", ""))
        if any(m in ct for m in _SELECT_MARKERS):
            checked += 1
            so = _field_options(src, scustom[name]["id"])
            to = _field_options(tgt, tcustom[name]["id"])
            miss = sorted(so - to)
            if miss:
                findings.append({"area": "custom_fields", "name": name,
                                 "kind": "option_mismatch",
                                 "detail": {"missing_options_in_tgt": miss[:40],
                                            "src_opts": len(so),
                                            "tgt_opts": len(to)}})
    summ["select_fields_checked"] = checked
    areas["custom_fields"] = summ
    say(f"[custom_fields] src={summ['src']} tgt={summ['tgt']} checked={checked}")

    # ---- workflows: structural comparison for in-both
    sw, _ = src.paginate_start_at("/rest/api/3/workflow/search",
                                  params={"expand": "transitions,statuses"})
    tw, _ = tgt.paginate_start_at("/rest/api/3/workflow/search",
                                  params={"expand": "transitions,statuses"})
    swn = {_wf_name(w): w for w in (sw or [])}
    twn = {_wf_name(w): w for w in (tw or [])}
    areas["workflows"] = _summary("workflows", swn.keys(), twn.keys())
    for name in areas["workflows"]["source_only"]:
        findings.append({"area": "workflows", "name": name,
                         "kind": "missing_in_tgt", "detail": {}})
    for name in sorted(set(swn) & set(twn)):
        s_tr = set(tr.get("name") for tr in (swn[name].get("transitions") or []))
        t_tr = set(tr.get("name") for tr in (twn[name].get("transitions") or []))
        s_st = len(swn[name].get("statuses") or [])
        t_st = len(twn[name].get("statuses") or [])
        if s_tr != t_tr or s_st != t_st:
            findings.append({"area": "workflows", "name": name,
                             "kind": "structure_mismatch",
                             "detail": {"src_statuses": s_st, "tgt_statuses": t_st,
                                        "transitions_missing_in_tgt":
                                            sorted(s_tr - t_tr)[:20]}})
    say(f"[workflows] in_both={areas['workflows']['in_both']}")

    # ---- screens: deep field check for in-both (bounded)
    ss, _ = src.paginate_start_at("/rest/api/3/screens")
    ts, _ = tgt.paginate_start_at("/rest/api/3/screens")
    ssn = {s["name"]: s for s in (ss or [])}
    tsn = {s["name"]: s for s in (ts or [])}
    in_both = sorted(set(ssn) & set(tsn))
    deep = in_both[:_SCREEN_DEEP_CAP]
    for name in deep:
        s_f = _screen_fields(src, ssn[name]["id"])
        t_f = _screen_fields(tgt, tsn[name]["id"])
        miss = sorted(s_f - t_f)
        if miss:
            findings.append({"area": "screens", "name": name,
                             "kind": "field_mismatch",
                             "detail": {"fields_missing_in_tgt": miss[:25]}})
    areas["screens"]["deep_checked"] = len(deep)
    areas["screens"]["capped"] = len(in_both) > _SCREEN_DEEP_CAP
    say(f"[screens] deep_checked={len(deep)} capped={areas['screens']['capped']}")

    # ---- JSM request types + queues per selected project (paginated correctly)
    def _sd_id(client, key):
        for s in client.sd_list("/rest/servicedeskapi/servicedesk"):
            if s.get("projectKey") == key:
                return s.get("id")
        return None

    def _jsm(client, key):
        sid = _sd_id(client, key)
        if not sid:
            return {"request_types": [], "queues": []}
        rt = client.sd_list(f"/rest/servicedeskapi/servicedesk/{sid}/requesttype")
        q = client.sd_list(f"/rest/servicedeskapi/servicedesk/{sid}/queue")
        return {"request_types": _names(rt), "queues": _names(q)}

    jsm_area = {}
    for key in jsm_projects:
        s_j, t_j = _jsm(src, key), _jsm(tgt, key)
        entry = {}
        for obj in ("request_types", "queues"):
            s_set, t_set = set(s_j[obj]), set(t_j[obj])
            entry[obj] = {"src": len(s_set), "tgt": len(t_set),
                          "source_only": sorted(s_set - t_set)}
            label = "request type" if obj == "request_types" else "queue"
            for name in sorted(s_set - t_set):
                findings.append({"area": "jsm",
                                 "name": f"{key}: {label} '{name}'",
                                 "kind": "missing_in_tgt",
                                 "detail": {"project": key,
                                            "object": label.replace(" ", "_")}})
        jsm_area[key] = entry
        say(f"[jsm {key}] rt src={entry['request_types']['src']}/"
            f"tgt={entry['request_types']['tgt']}")
    if jsm_projects:
        areas["jsm"] = jsm_area

    return {"areas": areas, "findings": findings}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_config_audit.py -q`
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add auditor/config_audit.py tests/test_config_audit.py
git commit -m "feat: instance config parity audit with select-option, workflow-structure, screen-field and JSM checks"
```

---

## Post-review amendments (applied)

Spec invariant (design doc ~line 159): an unreachable/unauthorized side fails LOUDLY — never rendered as "0 issues". The original config audit violated it in three places; all fixed:

- **Source/target fetch errors → loud `area_error` findings (never silent src=0).** For EVERY area fetch (the SIMPLE loop, custom fields `/field`, workflows, screens), a non-None paginate error now emits `{"area": <area>, "name": <area>, "kind": "area_error", "detail": {"side": "source"|"target", "error": <err>}}` (in addition to recording `summ["error"]`). A source outage no longer reads as a false-clean empty area; a target outage no longer silently inflates/hides findings. The errors that were previously discarded with `_` (custom fields `sf`/`tf`, workflows `sw`/`tw`, screens `ss`/`ts`) are now captured. New finding kind: `area_error`.
- **JSM scoped per-project (no whole-audit abort).** The per-project JSM block is wrapped in `try/except ClientError`: on failure it records `jsm_area[key] = {"error": str(exc)}`, emits `{"area":"jsm","name":f"{key}: lookup failed","kind":"area_error","detail":{"project":key,"error":str(exc)}}`, and continues to the next project. Already-computed findings (statuses/fields/workflows/screens) survive a JSM outage. `ClientError` imported from `auditor.client`.
- **Paginate mid-loop truncation now reports an error (`client.py`).** `paginate_start_at` previously did `break` and returned `(partial, None)` on a non-200 for page 2+ — silent truncation. It now returns `(out, f"ERR{st}:truncated")`. Net rule: ANY page failure (first-page or mid-loop) yields a non-None error string, even if some rows were already collected.

Tests added (TDD, written first and observed RED): `test_paginate_reports_midloop_truncation` (tests/test_client.py), `test_source_fetch_error_emits_area_error_not_clean` + `test_jsm_outage_is_scoped_not_total_abort` (tests/test_config_audit.py).

---

