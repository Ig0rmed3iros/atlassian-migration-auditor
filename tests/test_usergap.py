# tests/test_usergap.py
import gzip, json, os
import httpx
from auditor.client import Connection, JiraClient
from auditor.remediation.usergap import referenced_users, detect_user_gaps


def _write(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt") as fh:
        fh.write(json.dumps({"_extract_format": 3}) + "\n")
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_referenced_users_collects_reporter_and_assignee(tmp_path):
    p = os.path.join(tmp_path, "src", "P.core.jsonl.gz")
    _write(p, [{"key": "P-1", "fields": {
        "reporter": {"accountId": "a1", "displayName": "Ada"},
        "assignee": {"accountId": "a2", "displayName": "Ben"}}}])
    users = referenced_users(str(tmp_path), "P")
    assert {"a1", "a2"} <= set(users)


def test_detect_user_gaps_flags_unresolved_on_target(tmp_path):
    p = os.path.join(tmp_path, "src", "P.core.jsonl.gz")
    _write(p, [{"key": "P-1", "fields": {
        "reporter": {"accountId": "a1", "displayName": "Ada"}}}])

    def handler(req):
        # target user lookup: a1 not found
        return httpx.Response(404, json={})
    conn = Connection(auth_type="pat", site_url="https://t.atlassian.net",
                      deployment="cloud", email="a@b.c", api_token="x")
    tgt = JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                     sleeper=lambda s: None)
    gaps = detect_user_gaps(str(tmp_path), ["P"], tgt)
    assert gaps[0]["kind"] == "user_gap"
    assert gaps[0]["detail"]["account_id"] == "a1"
