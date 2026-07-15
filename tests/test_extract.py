import gzip, json
import httpx
from auditor.client import Connection, JiraClient
from auditor.extract import CORE_FIELDS, extract_project, slim
from auditor.textnorm import content_fp, wiki_text


def test_extract_page_default_and_env_override(monkeypatch):
    from auditor.extract import _extract_page
    monkeypatch.delenv("MA_EXTRACT_PAGE", raising=False)
    assert _extract_page() == 100
    monkeypatch.setenv("MA_EXTRACT_PAGE", "250"); assert _extract_page() == 250
    monkeypatch.setenv("MA_EXTRACT_PAGE", "0");   assert _extract_page() == 1
    monkeypatch.setenv("MA_EXTRACT_PAGE", "junk"); assert _extract_page() == 100


def test_extract_requests_page_100_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MA_EXTRACT_PAGE", raising=False)
    seen = {}
    def handler(req):
        p = str(req.url.path)
        if p.endswith("approximate-count"):
            return httpx.Response(200, json={"count": 1})
        if p.endswith("search/jql"):
            seen["maxResults"] = json.loads(req.content)["maxResults"]
            return httpx.Response(200, json={
                "issues": [{"key": "AC-1", "id": "1", "fields": {"summary": "s"}}],
                "isLast": True})
        return httpx.Response(404)
    conn = Connection(auth_type="pat", site_url="https://s.atlassian.net",
                      email="e", api_token="t")
    cl = JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                    sleeper=lambda s: None)
    extract_project(cl, "AC", str(tmp_path / "AC.core.jsonl.gz"))
    assert seen["maxResults"] == 100


def test_core_fields_have_no_instance_customfields():
    assert not any(f.startswith("customfield_") for f in CORE_FIELDS)
    for must in ("summary", "description", "status", "comment", "attachment"):
        assert must in CORE_FIELDS


def test_slim_fingerprints_description_and_comments():
    issue = {"key": "AC-1", "id": "1", "fields": {
        "summary": "s",
        "description": {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "hello world"}]}]},
        "comment": {"total": 2, "comments": [
            {"author": {"displayName": "A"}, "created": "c1", "updated": "u1",
             "body": {"type": "doc", "content": [
                 {"type": "paragraph", "content": [{"type": "text", "text": "first"}]}]}},
            {"author": {"displayName": "B"}, "created": "c2", "updated": "u2",
             "body": {"type": "doc", "content": [
                 {"type": "paragraph", "content": [{"type": "text", "text": "second"}]}]}},
        ]},
        "attachment": [{"filename": "a.png", "size": 10, "created": "c",
                        "author": {"displayName": "A"}}],
        "worklog": {"total": 3},
        "issuelinks": [{"type": {"name": "Blocks"},
                        "inwardIssue": {"key": "AC-9"}, "outwardIssue": None}],
        "environment": None,
    }}
    out = slim(issue)
    f = out["fields"]
    assert f["description"] == {"len": 11, "sha": content_fp("hello world"),
                                "head": "hello world"}
    assert f["comment"]["total"] == 2 and f["comment"]["inline"] == 2
    assert f["comment"]["items"][0]["sha"] == content_fp("first")
    assert f["attachment"] == [{"filename": "a.png", "size": 10, "created": "c",
                                "author": "A"}]
    assert f["worklog"] == {"total": 3}
    assert f["issuelinks"] == [{"type": "Blocks", "inward": "AC-9", "outward": None}]
    assert f["environment"] is None


def test_slim_wiki_dialect_string_bodies():
    desc = "h1. Title\nBody *bold* [~imedeiros]"
    cbody = "a *wiki* comment"
    issue = {"key": "AC-1", "id": "1", "fields": {
        "summary": "s",
        "description": desc,
        "comment": {"total": 1, "comments": [
            {"author": {"displayName": "Igor Medeiros"}, "created": "c1",
             "updated": "u1", "body": cbody}]},
    }}
    out = slim(issue, dialect="wiki")
    d = out["fields"]["description"]
    assert d["sha"] == content_fp(wiki_text(desc))
    assert d["head"] == desc            # display stays the raw readable string
    assert d["len"] == len(desc) > 0
    cm = out["fields"]["comment"]["items"][0]
    assert cm["sha"] == content_fp(wiki_text(cbody))
    assert cm["len"] == len(cbody)


def test_slim_adf_and_wiki_same_prose_same_sha():
    adf = {"type": "doc", "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "Hello World,"},
            {"type": "hardBreak"},
            {"type": "text", "text": "line two"}]}]}
    a = slim({"key": "AC-1", "id": "1", "fields": {"description": adf}})
    w = slim({"key": "AC-1", "id": "1",
              "fields": {"description": "Hello *World*,\nline two"}},
             dialect="wiki")
    assert a["fields"]["description"]["sha"] == w["fields"]["description"]["sha"]


def test_slim_normalizes_timestamps():
    a = slim({"key": "AC-1", "id": "1", "fields": {
        "created": "2024-01-02T03:04:05.000+0000",
        "updated": "2024-01-02T03:04:05Z",
        "resolutiondate": "2024-01-02T03:04:05+0000",
        "duedate": "2024-03-04"}})
    b = slim({"key": "AC-1", "id": "1", "fields": {
        "created": "2024-01-02T03:04:05+00:00",
        "updated": "2024-01-02T03:04:05.000+00:00",
        "resolutiondate": "2024-01-02T03:04:05Z",
        "duedate": "2024-03-04"}})
    for ts in ("created", "updated", "resolutiondate"):
        assert a["fields"][ts] == b["fields"][ts]
    assert a["fields"]["duedate"] == "2024-03-04"   # date-only passes through


def test_extract_project_dc_drops_cloud_only_field(tmp_path):
    seen = {}
    def handler(req):
        params = req.url.params
        if params.get("maxResults") == "0":          # approx_count probe
            return httpx.Response(200, json={"total": 0})
        seen["fields"] = params.get("fields", "")
        return httpx.Response(200, json={"issues": []})
    conn = Connection(auth_type="pat", site_url="https://jira.acme.example",
                      deployment="dc", api_token="t")
    cl = JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                    sleeper=lambda s: None)
    res = extract_project(cl, "AC", str(tmp_path / "x.gz"))
    assert res["verified"] is True
    # DC requests *all (a GET; enumerating ids would overflow the URL), which
    # also avoids naming the Cloud-only statuscategorychangedate field.
    assert seen["fields"] == "*all"


def _client_with_issues(issues, count):
    def handler(req):
        p = str(req.url.path)
        if p.endswith("approximate-count"):
            return httpx.Response(200, json={"count": count})
        if p.endswith("search/jql"):
            return httpx.Response(200, json={"issues": issues, "isLast": True})
        return httpx.Response(404)
    conn = Connection(auth_type="pat", site_url="https://s.atlassian.net",
                      email="e", api_token="t")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def test_extract_project_writes_gz_and_verifies(tmp_path):
    issues = [{"key": f"AC-{i}", "id": str(i), "fields": {"summary": f"s{i}"}}
              for i in range(3)]
    out = tmp_path / "AC.core.jsonl.gz"
    progress = []
    res = extract_project(_client_with_issues(issues, 3), "AC", str(out),
                          progress=lambda n: progress.append(n))
    assert res == {"extracted": 3, "approx": 3, "verified": True}
    with gzip.open(out, "rt") as fh:
        rows = [json.loads(l) for l in fh]
    assert "_extract_format" in rows[0]          # format stamp header first
    assert [r["key"] for r in rows[1:]] == ["AC-0", "AC-1", "AC-2"]


def test_extract_project_flags_count_mismatch(tmp_path):
    issues = [{"key": "AC-1", "id": "1", "fields": {"summary": "s"}}]
    res = extract_project(_client_with_issues(issues, 5), "AC",
                          str(tmp_path / "x.gz"))
    assert res["verified"] is False and res["approx"] == 5


def test_truncated_extract_not_committed_to_out_path(tmp_path):
    # A clean-but-truncated run (paginated short of the authoritative count, no
    # crash) must NOT commit a valid-looking file: it carries a current format
    # stamp, so a later reuse run would trust it and score fidelity against a
    # short extract (review Bug 5). out_path stays absent.
    issues = [{"key": "AC-1", "id": "1", "fields": {"summary": "s"}}]
    out = tmp_path / "AC.core.jsonl.gz"
    res = extract_project(_client_with_issues(issues, 5), "AC", str(out))
    assert res["verified"] is False and res["approx"] == 5
    assert not out.exists()


def test_truncated_extract_preserves_existing_cached_file(tmp_path):
    # If a complete extract already sits at out_path, a later truncated re-run
    # must not clobber it with the short result.
    out = tmp_path / "AC.core.jsonl.gz"
    out.write_bytes(b"PRIOR-COMPLETE-EXTRACT")
    issues = [{"key": "AC-1", "id": "1", "fields": {"summary": "s"}}]
    res = extract_project(_client_with_issues(issues, 5), "AC", str(out))
    assert res["verified"] is False
    assert out.read_bytes() == b"PRIOR-COMPLETE-EXTRACT"


def test_crash_mid_extraction_leaves_no_file_at_out_path(tmp_path):
    calls = {"n": 0}
    def handler(req):
        p = str(req.url.path)
        if p.endswith("search/jql"):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(200, json={
                    "issues": [{"key": "AC-1", "id": "1",
                                "fields": {"summary": "s"}}],
                    "nextPageToken": "t2"})
            return httpx.Response(400, text="boom")   # 4xx -> ClientError
        return httpx.Response(404)
    conn = Connection(auth_type="pat", site_url="https://s.atlassian.net",
                      email="e", api_token="t")
    cl = JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                    sleeper=lambda s: None)
    out = tmp_path / "AC.core.jsonl.gz"
    import pytest
    from auditor.client import ClientError
    with pytest.raises(ClientError):
        extract_project(cl, "AC", str(out))
    assert not out.exists()          # only the orphaned .tmp may exist


def test_extraction_streams_across_pages(tmp_path):
    pages = [
        {"issues": [{"key": "AC-1", "id": "1", "fields": {"summary": "a"}},
                    {"key": "AC-2", "id": "2", "fields": {"summary": "b"}}],
         "nextPageToken": "t2"},
        {"issues": [{"key": "AC-3", "id": "3", "fields": {"summary": "c"}}],
         "isLast": True},
    ]
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/field"):
            return httpx.Response(200, json=[])
        if p.endswith("approximate-count"):
            return httpx.Response(200, json={"count": 3})
        body = json.loads(req.content)
        return httpx.Response(200, json=pages[1] if body.get("nextPageToken")
                              else pages[0])
    conn = Connection(auth_type="pat", site_url="https://s.atlassian.net",
                      email="e", api_token="t")
    cl = JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                    sleeper=lambda s: None)
    out = tmp_path / "AC.core.jsonl.gz"
    res = extract_project(cl, "AC", str(out))
    assert res == {"extracted": 3, "approx": 3, "verified": True}
    with gzip.open(out, "rt") as fh:
        keys = [json.loads(l).get("key") for l in fh]
        assert keys == [None, "AC-1", "AC-2", "AC-3"]   # header line first


def test_extra_fields_reach_the_search_request(tmp_path):
    seen = {}
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/field"):
            return httpx.Response(200, json=[])
        if p.endswith("approximate-count"):
            return httpx.Response(200, json={"count": 0})
        seen["fields"] = json.loads(req.content)["fields"]
        return httpx.Response(200, json={"issues": [], "isLast": True})
    conn = Connection(auth_type="pat", site_url="https://s.atlassian.net",
                      email="e", api_token="t")
    cl = JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                    sleeper=lambda s: None)
    extract_project(cl, "AC", str(tmp_path / "x.gz"),
                    extra_fields=("customfield_10002",))
    assert "customfield_10002" in seen["fields"]
    assert "summary" in seen["fields"]


def test_err_string_approx_count_is_unverified_not_crash(tmp_path):
    def handler(req):
        p = str(req.url.path)
        if p.endswith("approximate-count"):
            return httpx.Response(500, text="down")    # retries then ERR
        return httpx.Response(200, json={"issues": [
            {"key": "AC-1", "id": "1", "fields": {"summary": "s"}}],
            "isLast": True})
    conn = Connection(auth_type="pat", site_url="https://s.atlassian.net",
                      email="e", api_token="t")
    cl = JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                    sleeper=lambda s: None)
    res = extract_project(cl, "AC", str(tmp_path / "x.gz"))
    assert res["extracted"] == 1
    assert isinstance(res["approx"], str) and res["approx"].startswith("ERR")
    assert res["verified"] is False


# ── Extract file format stamp ────────────────────────────────────────────────
# The slim() output format changed (shas are content_fp of canonical text,
# timestamps are norm_ts epochs). reuse_extracts_from can point a run at a
# pre-upgrade workspace; mixing a cached old-format side with a fresh one
# would flag EVERY common issue as drifted. The stamp lets the engine refuse.

def test_extract_writes_format_header_and_reader_reports_it(tmp_path):
    from auditor.extract import EXTRACT_FORMAT, extract_format
    issues = [{"key": "AC-1", "id": "1", "fields": {"summary": "s"}}]
    out = tmp_path / "AC.core.jsonl.gz"
    res = extract_project(_client_with_issues(issues, 1), "AC", str(out))
    assert res == {"extracted": 1, "approx": 1, "verified": True}
    with gzip.open(out, "rt") as fh:
        first = json.loads(fh.readline())
    assert first["_extract_format"] == EXTRACT_FORMAT
    # Header also carries the custom-field name inventory (empty here: this
    # mock serves no /field) so the comparator can tell "field absent on the
    # instance" from "field empty on the issue".
    assert first["cf_names"] == [] and first["cf_ambiguous"] == []
    assert extract_format(str(out)) == EXTRACT_FORMAT


def test_extract_format_of_legacy_and_unreadable_files(tmp_path):
    from auditor.extract import extract_format
    legacy = tmp_path / "legacy.core.jsonl.gz"
    with gzip.open(legacy, "wt") as fh:           # pre-stamp file: no header
        fh.write(json.dumps({"key": "AC-1", "fields": {}}) + "\n")
    assert extract_format(str(legacy)) == 1
    garbage = tmp_path / "garbage.core.jsonl.gz"
    garbage.write_bytes(b"not gzip at all")
    assert extract_format(str(garbage)) == 0      # never reusable
    empty = tmp_path / "empty.core.jsonl.gz"
    with gzip.open(empty, "wt") as fh:
        pass
    assert extract_format(str(empty)) == 0


def test_extract_project_escapes_project_key(tmp_path):
    # Defense in depth: a project key carrying a double quote must reach the
    # search AND count JQL as an escaped literal, not break out of it.
    jqls = []
    def handler(req):
        if str(req.url.path).endswith("/field"):
            return httpx.Response(200, json=[])
        body = json.loads(req.content)
        jqls.append(body["jql"])
        if str(req.url.path).endswith("approximate-count"):
            return httpx.Response(200, json={"count": 0})
        return httpx.Response(200, json={"issues": [], "isLast": True})
    conn = Connection(auth_type="pat", site_url="https://s.atlassian.net",
                      email="e", api_token="t")
    cl = JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                    sleeper=lambda s: None)
    extract_project(cl, 'AC"X', str(tmp_path / "x.gz"))
    assert jqls[0] == 'project = "AC\\"X" ORDER BY key ASC'
    assert jqls[1] == 'project = "AC\\"X"'


# ---------------------------------------------------------------- finding 3
def test_environment_presence_gate_is_dialect_symmetric():
    """An image-only environment keeps raw markup as DC wiki DISPLAY text
    (truthy) but renders empty from an ADF media node — gating presence on
    display text stored a fingerprint-of-nothing on one side and None on the
    other, a false field_mismatch on every such issue (audit finding 3).
    Presence must key on the CANONICAL content instead."""
    wiki_issue = {"key": "AC-1", "fields": {
        "environment": "!screenshot.png|thumbnail!"}}
    adf_issue = {"key": "AC-1", "fields": {"environment": {
        "type": "doc", "content": [{"type": "mediaSingle", "content": [
            {"type": "media", "attrs": {"id": "f1"}}]}]}}}
    assert slim(wiki_issue, "wiki")["fields"]["environment"] is None
    assert slim(adf_issue, "adf")["fields"]["environment"] is None


def test_environment_with_prose_still_fingerprints_equal_across_dialects():
    wiki_issue = {"key": "AC-1", "fields": {
        "environment": "Chrome 125 on *Windows 11*"}}
    adf_issue = {"key": "AC-1", "fields": {"environment": {
        "type": "doc", "content": [{"type": "paragraph", "content": [
            {"type": "text", "text": "Chrome 125 on Windows 11"}]}]}}}
    we = slim(wiki_issue, "wiki")["fields"]["environment"]
    ae = slim(adf_issue, "adf")["fields"]["environment"]
    assert we is not None and ae is not None
    assert we["sha"] == ae["sha"]


# ---------------------------------------------------------------- finding 7
def test_dc_extraction_maps_epic_link_to_parent(tmp_path):
    """Jira Cloud's parent-field unification returns fields.parent for
    epic-linked issues; DC keeps the epic link in the gh-epic-link
    customfield and serves parent only for subtasks. Without the mapping a
    faithful JCMA migration mass-flags parent None vs PROJ-nn on every issue
    under an epic (audit finding 7)."""
    issues = [
        {"key": "AC-1", "id": "1", "fields": {
            "summary": "epic child", "customfield_10100": "AC-9"}},
        {"key": "AC-2", "id": "2", "fields": {
            "summary": "subtask", "parent": {"key": "AC-1"},
            "customfield_10100": None}},
        {"key": "AC-3", "id": "3", "fields": {"summary": "loose"}},
    ]

    def handler(req):
        p = str(req.url.path)
        params = req.url.params
        if p.endswith("/rest/api/2/field"):
            return httpx.Response(200, json=[
                {"id": "customfield_10100", "name": "Epic Link",
                 "custom": True,
                 "schema": {"custom": "com.pyxis.greenhopper.jira:gh-epic-link"}},
                {"id": "summary", "name": "Summary", "custom": False},
            ])
        if params.get("maxResults") == "0":
            return httpx.Response(200, json={"total": 3})
        if "id >" in (params.get("jql") or ""):
            return httpx.Response(200, json={"issues": []})
        # DC requests *all (not a per-id list), so the epic-link customfield is
        # returned implicitly and folded into parent by slim().
        assert params.get("fields", "") == "*all"
        return httpx.Response(200, json={"issues": issues})

    conn = Connection(auth_type="pat", site_url="https://jira.acme.example",
                      deployment="dc", api_token="t")
    cl = JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                    sleeper=lambda s: None)
    out = str(tmp_path / "ac.gz")
    res = extract_project(cl, "AC", out)
    assert res["verified"] is True
    rows = {}
    with gzip.open(out, "rt") as fh:
        for line in fh:
            r = json.loads(line)
            if "_extract_format" in r:
                continue
            rows[r["key"]] = r["fields"]
    assert rows["AC-1"]["parent"] == {"key": "AC-9"}     # epic link mapped
    assert "customfield_10100" not in rows["AC-1"]       # raw field dropped
    assert rows["AC-2"]["parent"] == {"key": "AC-1"}     # subtask untouched
    assert rows["AC-3"].get("parent") is None


def test_dc_extraction_survives_missing_epic_link_field(tmp_path):
    """A DC instance without the gh-epic-link field (no Jira Software) has no
    epics — extraction proceeds without the mapping, never crashes."""
    def handler(req):
        p = str(req.url.path)
        params = req.url.params
        if p.endswith("/rest/api/2/field"):
            return httpx.Response(403, text="nope")
        if params.get("maxResults") == "0":
            return httpx.Response(200, json={"total": 0})
        return httpx.Response(200, json={"issues": []})
    conn = Connection(auth_type="pat", site_url="https://jira.acme.example",
                      deployment="dc", api_token="t")
    cl = JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                    sleeper=lambda s: None)
    res = extract_project(cl, "AC", str(tmp_path / "x.gz"))
    assert res["verified"] is True


# ---------------------------------------------------------------------------
# Custom-field VALUE capture (EXTRACT_FORMAT 4): slim() fingerprints CF values
# by NAME and never stores the raw value; extract_project records the CF name
# inventory in the header.
# ---------------------------------------------------------------------------

def test_slim_captures_cf_values_by_name():
    cf_meta = {"customfield_10001": {"name": "Severity",
                                     "schema": {"type": "option"}}}
    issue = {"key": "AC-1", "id": "1", "fields": {
        "summary": "s", "customfield_10001": {"value": "High", "id": "10"}}}
    out = slim(issue, cf_meta=cf_meta)
    assert out["fields"]["_cf"]["Severity"]["kind"] == "option"
    assert "customfield_10001" not in out["fields"]   # raw value never stored


def test_slim_omits_empty_cf_values():
    cf_meta = {"customfield_10001": {"name": "Severity",
                                     "schema": {"type": "option"}}}
    issue = {"key": "AC-1", "id": "1",
             "fields": {"summary": "s", "customfield_10001": None}}
    out = slim(issue, cf_meta=cf_meta)
    assert out["fields"]["_cf"] == {}


def test_slim_strips_raw_customfields_even_without_meta():
    issue = {"key": "AC-1", "id": "1",
             "fields": {"summary": "s", "customfield_10001": "x"}}
    out = slim(issue)
    assert "customfield_10001" not in out["fields"]


def _client_with_fields(field_meta, issues, count, captured=None):
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/field"):
            return httpx.Response(200, json=field_meta)
        if p.endswith("approximate-count"):
            return httpx.Response(200, json={"count": count})
        if p.endswith("search/jql"):
            if captured is not None:
                captured["fields"] = json.loads(req.content).get("fields", [])
            return httpx.Response(200, json={"issues": issues, "isLast": True})
        return httpx.Response(404)
    conn = Connection(auth_type="pat", site_url="https://s.atlassian.net",
                      email="e", api_token="t")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def test_extract_captures_cf_names_in_header_and_values_in_rows(tmp_path):
    field_meta = [
        {"id": "customfield_10001", "name": "Severity",
         "schema": {"type": "option", "custom": "x:select"}},
        {"id": "summary", "name": "Summary", "schema": {"type": "string"}},
    ]
    issues = [{"key": "AC-1", "id": "1", "fields": {
        "summary": "s", "customfield_10001": {"value": "High", "id": "5"}}}]
    cap = {}
    cl = _client_with_fields(field_meta, issues, 1, captured=cap)
    out = tmp_path / "AC.gz"
    extract_project(cl, "AC", str(out))
    assert "customfield_10001" in cap["fields"]      # CF id was requested
    with gzip.open(out, "rt") as fh:
        rows = [json.loads(l) for l in fh]
    assert rows[0]["cf_names"] == ["Severity"]
    assert rows[1]["fields"]["_cf"]["Severity"]["kind"] == "option"


def test_extract_flags_duplicate_cf_names_as_ambiguous(tmp_path):
    # Two custom fields share the name "Region": comparison by name would be
    # ambiguous, so both are excluded from per-issue _cf and disclosed.
    field_meta = [
        {"id": "customfield_1", "name": "Region",
         "schema": {"type": "option", "custom": "x:select"}},
        {"id": "customfield_2", "name": "Region",
         "schema": {"type": "option", "custom": "x:select"}},
    ]
    issues = [{"key": "AC-1", "id": "1", "fields": {
        "summary": "s", "customfield_1": {"value": "EU"},
        "customfield_2": {"value": "US"}}}]
    cl = _client_with_fields(field_meta, issues, 1)
    out = tmp_path / "AC.gz"
    extract_project(cl, "AC", str(out))
    with gzip.open(out, "rt") as fh:
        rows = [json.loads(l) for l in fh]
    assert rows[0]["cf_ambiguous"] == ["Region"]
    assert rows[0]["cf_names"] == []                 # ambiguous names excluded
    assert rows[1]["fields"]["_cf"] == {}            # not compared per issue


def test_dc_extract_requests_all_not_enumerated_cf_ids(tmp_path):
    # Regression guard for the DC GET URL-overflow: even with many custom
    # fields, the DC search must request "*all", never a per-id list.
    field_meta = [{"id": f"customfield_{i}", "name": f"CF{i}",
                   "schema": {"type": "option", "custom": "x:select"}}
                  for i in range(50)]
    seen = {}
    def handler(req):
        p, params = str(req.url.path), req.url.params
        if p.endswith("/rest/api/2/field"):
            return httpx.Response(200, json=field_meta)
        if params.get("maxResults") == "0":
            return httpx.Response(200, json={"total": 0})
        seen["fields"] = params.get("fields", "")
        return httpx.Response(200, json={"issues": []})
    conn = Connection(auth_type="pat", site_url="https://jira.acme.example",
                      deployment="dc", api_token="t")
    cl = JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                    sleeper=lambda s: None)
    extract_project(cl, "AC", str(tmp_path / "x.gz"))
    assert seen["fields"] == "*all"
    assert "customfield_" not in seen["fields"]


def test_slim_cf_stores_fingerprint_not_raw_value():
    # The privacy guarantee: a raw custom-field value must never reach the
    # extract — only its one-way fingerprint.
    cf_meta = {"customfield_1": {"name": "Salary", "schema": {"type": "string"}}}
    issue = {"key": "AC-1", "id": "1",
             "fields": {"summary": "s", "customfield_1": "123456"}}
    out = slim(issue, cf_meta=cf_meta)
    assert "123456" not in json.dumps(out["fields"])
    assert out["fields"]["_cf"]["Salary"]["fp"] != "123456"
