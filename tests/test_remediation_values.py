import gzip, json, os
import httpx
from auditor.client import Connection, JiraClient
from auditor.remediation.values import capture_field_values


def mk(handler):
    conn = Connection(auth_type="pat", site_url="https://s.atlassian.net",
                      deployment="cloud", email="a@b.c", api_token="x")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def test_capture_writes_issue_key_to_value(tmp_path):
    def handler(req):
        # cloud search/jql POST
        return httpx.Response(200, json={"issues": [
            {"key": "P-1", "fields": {"customfield_1": {"value": "High"}}},
            {"key": "P-2", "fields": {"customfield_1": None}}], "isLast": True})
    out = os.path.join(tmp_path, "v.jsonl.gz")
    n = capture_field_values(mk(handler), ["P"], "customfield_1", out)
    assert n == 1   # P-2 had no value, skipped
    rows = [json.loads(l) for l in gzip.open(out, "rt")]
    assert rows == [{"issue_key": "P-1", "value": {"value": "High"}}]


def test_capture_bare_filename_does_not_crash(tmp_path, monkeypatch):
    """out_path with no directory component (dirname == '') must not raise."""
    def handler(req):
        return httpx.Response(200, json={"issues": [], "isLast": True})
    # change cwd to tmp_path so the bare file lands there cleanly
    monkeypatch.chdir(tmp_path)
    n = capture_field_values(mk(handler), ["P"], "customfield_1", "bare.jsonl.gz")
    assert n == 0
    assert os.path.exists(tmp_path / "bare.jsonl.gz")


def test_capture_fields_values_single_pass(tmp_path):
    from auditor.remediation.values import capture_fields_values
    calls = {"n": 0}
    def handler(req):
        if str(req.url.path).endswith("/search/jql"):
            calls["n"] += 1
            return httpx.Response(200, json={"issues": [
                {"key": "P-1", "fields": {"cf_1": {"value": "High"}, "cf_2": "x"}},
                {"key": "P-2", "fields": {"cf_1": None, "cf_2": "y"}}],
                "isLast": True})
        return httpx.Response(200, json={"issues": [], "isLast": True})
    counts = capture_fields_values(mk(handler), ["P"], ["cf_1", "cf_2"], str(tmp_path))
    assert calls["n"] == 1                      # ONE scan for BOTH fields
    assert counts == {"cf_1": 1, "cf_2": 2}
    r1 = [json.loads(l) for l in gzip.open(os.path.join(tmp_path, "cf_1.jsonl.gz"), "rt")]
    assert r1 == [{"issue_key": "P-1", "value": {"value": "High"}}]
    assert os.path.exists(os.path.join(tmp_path, "cf_2.jsonl.gz"))


def test_capture_fields_values_empty_field_gets_empty_file(tmp_path):
    from auditor.remediation.values import capture_fields_values
    def handler(req):
        return httpx.Response(200, json={"issues": [
            {"key": "P-1", "fields": {"cf_1": None}}], "isLast": True})
    counts = capture_fields_values(mk(handler), ["P"], ["cf_1"], str(tmp_path))
    assert counts == {"cf_1": 0}
    assert os.path.exists(os.path.join(tmp_path, "cf_1.jsonl.gz"))   # empty but present
