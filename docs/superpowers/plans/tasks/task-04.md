### Task 4: `auditor/scope.py` — project enumeration + matching

**Files:**
- Create: `auditor/scope.py`
- Test: `tests/test_scope.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_scope.py`:
```python
from auditor.scope import match_projects

SRC = [
    {"key": "AC", "name": "AC Support", "id": "100"},
    {"key": "MS", "name": "Managed Services", "id": "101"},
    {"key": "OLD", "name": "Legacy", "id": "102"},
]
TGT = [
    {"key": "AC", "name": "AC Support", "id": "900"},
    {"key": "MS", "name": "Managed Services", "id": "901"},
    {"key": "NEW", "name": "Greenfield", "id": "902"},
]


def test_match_by_key():
    m = match_projects(SRC, TGT)
    keys = [p["key"] for p in m["matched"]]
    assert keys == ["AC", "MS"]
    ac = m["matched"][0]
    assert ac["src_id"] == "100" and ac["tgt_id"] == "900"
    assert ac["src_count"] is None and ac["tgt_count"] is None


def test_source_and_target_only():
    m = match_projects(SRC, TGT)
    assert [p["key"] for p in m["source_only"]] == ["OLD"]
    assert [p["key"] for p in m["target_only"]] == ["NEW"]


def test_empty_sides():
    m = match_projects([], TGT)
    assert m["matched"] == [] and len(m["target_only"]) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_scope.py -q`
Expected: `ModuleNotFoundError: No module named 'auditor.scope'`.

- [ ] **Step 3: Write the implementation**

`auditor/scope.py`:
```python
"""Project scope: enumerate both sides and match by key.

Counts (src_count/tgt_count) are left None here; the run engine fills them
via approx_count so this stays a pure function over project lists.
"""
from __future__ import annotations


def match_projects(src_projects: list, tgt_projects: list) -> dict:
    s = {p["key"]: p for p in src_projects}
    t = {p["key"]: p for p in tgt_projects}
    matched = []
    for key in sorted(set(s) & set(t)):
        matched.append({
            "key": key,
            "name": s[key].get("name"),
            "src_id": s[key].get("id"),
            "tgt_id": t[key].get("id"),
            "src_count": None,
            "tgt_count": None,
        })
    source_only = [{"key": k, "name": s[k].get("name")}
                   for k in sorted(set(s) - set(t))]
    target_only = [{"key": k, "name": t[k].get("name")}
                   for k in sorted(set(t) - set(s))]
    return {"matched": matched, "source_only": source_only,
            "target_only": target_only}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_scope.py -q`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add auditor/scope.py tests/test_scope.py
git commit -m "feat: project scope matching (matched/source-only/target-only)"
```

---

