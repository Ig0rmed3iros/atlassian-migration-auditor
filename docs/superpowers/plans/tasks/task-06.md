### Task 6: `auditor/extract.py` — content-fingerprint extraction

**Files:**
- Create: `auditor/extract.py`
- Test: `tests/test_extract.py`

Port of `extract_core.py`: every issue (no sampling), audit-critical fields, description/comment bodies reduced to sha fingerprints, gzip JSONL output, count verification. Instance-specific custom fields are NOT hardcoded — callers pass `extra_fields`.

- [ ] **Step 1: Write the failing tests**

`tests/test_extract.py`:
```python
import gzip, json
import httpx
from auditor.client import Connection, JiraClient, h16
from auditor.extract import CORE_FIELDS, extract_project, slim


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
    assert f["description"] == {"len": 11, "sha": h16("hello world"),
                                "head": "hello world"}
    assert f["comment"]["total"] == 2 and f["comment"]["inline"] == 2
    assert f["comment"]["items"][0]["sha"] == h16("first")
    assert f["attachment"] == [{"filename": "a.png", "size": 10, "created": "c",
                                "author": "A"}]
    assert f["worklog"] == {"total": 3}
    assert f["issuelinks"] == [{"type": "Blocks", "inward": "AC-9", "outward": None}]
    assert f["environment"] is None


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
    assert [r["key"] for r in rows] == ["AC-0", "AC-1", "AC-2"]


def test_extract_project_flags_count_mismatch(tmp_path):
    issues = [{"key": "AC-1", "id": "1", "fields": {"summary": "s"}}]
    res = extract_project(_client_with_issues(issues, 5), "AC",
                          str(tmp_path / "x.gz"))
    assert res["verified"] is False and res["approx"] == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_extract.py -q`
Expected: `ModuleNotFoundError: No module named 'auditor.extract'`.

- [ ] **Step 3: Write the implementation**

`auditor/extract.py`:
```python
"""Deterministic content-complete extraction (port of extract_core.py).

Every issue of a project, both audit-critical fields and content fingerprints
(description/comments reduced to sha16 + length so megabytes of prose become
comparable rows). Output: gzip JSONL, one slim issue per line, key-ordered.
Count-verified against approximate-count so a silent pagination gap can never
masquerade as a clean extraction.
"""
from __future__ import annotations

import gzip
import json
from typing import Callable

from .client import JiraClient, adf_text, h16

CORE_FIELDS = [
    "summary", "description", "issuetype", "status", "statuscategorychangedate",
    "priority", "resolution", "resolutiondate", "assignee", "reporter", "creator",
    "created", "updated", "duedate", "labels", "components", "fixVersions",
    "versions", "parent", "issuelinks", "subtasks", "comment", "attachment",
    "votes", "watches", "timetracking", "environment", "security", "worklog",
]


def slim(issue: dict) -> dict:
    f = dict(issue.get("fields", {}))
    dtext = adf_text(f.get("description")) if f.get("description") else ""
    f["description"] = {"len": len(dtext), "sha": h16(dtext), "head": dtext[:200]}
    c = f.get("comment") or {}
    items = []
    for cm in (c.get("comments") or []):
        ctext = adf_text(cm.get("body"))
        items.append({"author": (cm.get("author") or {}).get("displayName"),
                      "created": cm.get("created"), "updated": cm.get("updated"),
                      "len": len(ctext), "sha": h16(ctext)})
    f["comment"] = {"total": c.get("total"), "inline": len(items), "items": items}
    f["attachment"] = [{"filename": a.get("filename"), "size": a.get("size"),
                        "created": a.get("created"),
                        "author": (a.get("author") or {}).get("displayName")}
                       for a in (f.get("attachment") or [])]
    wl = f.get("worklog") or {}
    f["worklog"] = {"total": wl.get("total")}
    f["issuelinks"] = [{"type": (l.get("type") or {}).get("name"),
                        "inward": (l.get("inwardIssue") or {}).get("key"),
                        "outward": (l.get("outwardIssue") or {}).get("key")}
                       for l in (f.get("issuelinks") or [])]
    etext = adf_text(f.get("environment")) if f.get("environment") else ""
    f["environment"] = {"len": len(etext), "sha": h16(etext)} if etext else None
    return {"key": issue["key"], "id": issue.get("id"), "fields": f}


def extract_project(client: JiraClient, project_key: str, out_path: str,
                    extra_fields: tuple = (),
                    progress: Callable[[int], None] | None = None) -> dict:
    fields = CORE_FIELDS + list(extra_fields)
    n = 0
    with gzip.open(out_path, "wt", encoding="utf-8") as fh:
        for iss in client.search_jql(
                f'project = "{project_key}" ORDER BY key ASC', fields, page=50):
            fh.write(json.dumps(slim(iss), default=str) + "\n")
            n += 1
            if progress and n % 500 == 0:
                progress(n)
    ac = client.approx_count(f'project = "{project_key}"')
    verified = isinstance(ac, int) and n == ac
    if progress:
        progress(n)
    return {"extracted": n, "approx": ac, "verified": verified}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_extract.py -q`
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add auditor/extract.py tests/test_extract.py
git commit -m "feat: content-fingerprint extraction with count verification"
```

---

## Post-review amendments (applied)

Atomic temp+rename write (a crashed extraction must never leave a reusable partial file at out_path; closes the reuse_extracts_from silent-under-report seam); 4 new tests.

