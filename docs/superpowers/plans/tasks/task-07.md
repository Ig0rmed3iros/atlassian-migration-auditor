### Task 7: `auditor/compare.py` — fidelity comparison → findings

**Files:**
- Create: `auditor/compare.py`
- Test: `tests/test_compare.py`

Port of `compare.py` reshaped to emit finding dicts (spec kinds) instead of files. Key semantics preserved: presence split by the cutover line (`tail` = missing key-number above target max = expected drift; `hole` = below the line = **genuine loss** → `missing_in_tgt`), field SPECS with severity, remap tables, user-mapping audit, comment count/content fidelity, attachment fidelity. New: `missing_in_src` for target-extra issues (above src max → `tail_post_cutover` with `direction: "target"`), `key_collision` when a same-key pair's identity metadata (created + reporter + summary) disagrees.

- [ ] **Step 1: Write the failing tests**

`tests/test_compare.py`:
```python
import gzip, json
import pytest
from auditor.client import h16
from auditor.compare import compare_project


def write_side(path, rows):
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def mk_issue(key, summary="s", desc="body", status="Open", created="2026-01-01T00:00:00.000+0000",
             reporter="Ana", comments=(), attachments=(), labels=(), links=()):
    return {"key": key, "id": key, "fields": {
        "summary": summary,
        "description": {"len": len(desc), "sha": h16(desc), "head": desc[:200]},
        "issuetype": {"name": "Task"}, "status": {"name": status},
        "priority": {"name": "P3"}, "resolution": None, "resolutiondate": None,
        "created": created, "updated": "x", "duedate": None,
        "labels": list(labels), "components": [], "fixVersions": [], "versions": [],
        "parent": None, "environment": None, "security": None,
        "assignee": {"displayName": "Bob"}, "reporter": {"displayName": reporter},
        "creator": {"displayName": reporter},
        "comment": {"total": len(comments), "inline": len(comments),
                    "items": [{"author": "A", "created": "c", "updated": "u",
                               "len": len(t), "sha": h16(t)} for t in comments]},
        "worklog": {"total": 0}, "votes": {"votes": 0}, "watches": {"watchCount": 0},
        "attachment": [{"filename": fn, "size": sz, "created": "c", "author": "A"}
                       for fn, sz in attachments],
        "issuelinks": [{"type": t, "inward": i, "outward": o} for t, i, o in links],
    }}


@pytest.fixture()
def paths(tmp_path):
    return str(tmp_path / "src.gz"), str(tmp_path / "tgt.gz")


def kinds(findings):
    return sorted(f["kind"] for f in findings)


def test_identical_sides_produce_no_findings(paths):
    src, tgt = paths
    rows = [mk_issue("AC-1"), mk_issue("AC-2")]
    write_side(src, rows); write_side(tgt, rows)
    out = compare_project("AC", src, tgt)
    assert out["findings"] == []
    assert out["stats"]["src"] == 2 and out["stats"]["common"] == 2
    assert out["stats"]["fidelity_pct"] == 100.0


def test_genuine_hole_vs_post_cutover_tail(paths):
    src, tgt = paths
    # target max key-num = 3; AC-2 missing below the line = HOLE,
    # AC-9 missing above the line = expected tail.
    write_side(src, [mk_issue("AC-1"), mk_issue("AC-2"), mk_issue("AC-3"),
                     mk_issue("AC-9")])
    write_side(tgt, [mk_issue("AC-1"), mk_issue("AC-3")])
    out = compare_project("AC", src, tgt)
    by_kind = {f["kind"]: f for f in out["findings"]}
    assert by_kind["missing_in_tgt"]["src_key"] == "AC-2"
    tail = by_kind["tail_post_cutover"]
    assert tail["src_key"] == "AC-9" and tail["detail"]["direction"] == "source"
    assert out["stats"]["missing_in_tgt"] == 1 and out["stats"]["tails"] == 1


def test_target_extra_above_src_max_is_target_tail(paths):
    src, tgt = paths
    write_side(src, [mk_issue("AC-1")])
    write_side(tgt, [mk_issue("AC-1"), mk_issue("AC-5")])
    out = compare_project("AC", src, tgt)
    f = out["findings"][0]
    assert f["kind"] == "tail_post_cutover" and f["detail"]["direction"] == "target"
    assert f["tgt_key"] == "AC-5"


def test_target_extra_below_src_max_is_missing_in_src(paths):
    src, tgt = paths
    write_side(src, [mk_issue("AC-1"), mk_issue("AC-9")])
    write_side(tgt, [mk_issue("AC-1"), mk_issue("AC-5"), mk_issue("AC-9")])
    out = compare_project("AC", src, tgt)
    assert kinds(out["findings"]) == ["missing_in_src"]


def test_field_and_content_mismatches(paths):
    src, tgt = paths
    write_side(src, [mk_issue("AC-1", status="Open", desc="original")])
    write_side(tgt, [mk_issue("AC-1", status="Done", desc="rewritten")])
    out = compare_project("AC", src, tgt)
    ks = kinds(out["findings"])
    assert "field_mismatch" in ks and "content_mismatch" in ks
    fm = next(f for f in out["findings"] if f["kind"] == "field_mismatch")
    assert fm["field"] == "status" and fm["detail"]["src"] == "Open" \
        and fm["detail"]["tgt"] == "Done" and fm["detail"]["sev"] == "high"
    assert out["stats"]["remap"]["status"][0]["count"] == 1


def test_comment_and_attachment_fidelity(paths):
    src, tgt = paths
    write_side(src, [mk_issue("AC-1", comments=("hello", "world"),
                              attachments=(("a.png", 10),))])
    write_side(tgt, [mk_issue("AC-1", comments=("hello", "DIFFERENT"),
                              attachments=(("a.png", 10), ("b.png", 5)))])
    out = compare_project("AC", src, tgt)
    ks = kinds(out["findings"])
    assert "comment_mismatch" in ks and "attachment_mismatch" in ks
    am = next(f for f in out["findings"] if f["kind"] == "attachment_mismatch")
    assert am["detail"]["extra_in_tgt"] == ["b.png|5"]


def test_key_collision_when_identity_metadata_disagrees(paths):
    src, tgt = paths
    write_side(src, [mk_issue("AC-1", summary="real issue", reporter="Ana",
                              created="2020-01-01T00:00:00.000+0000")])
    write_side(tgt, [mk_issue("AC-1", summary="totally different", reporter="Zed",
                              created="2026-06-01T00:00:00.000+0000")])
    out = compare_project("AC", src, tgt)
    assert "key_collision" in kinds(out["findings"])


def test_unmapped_users_in_stats(paths):
    src, tgt = paths
    s = mk_issue("AC-1", reporter="Ana")
    t = mk_issue("AC-1", reporter="Ana")
    t["fields"]["assignee"] = {"displayName": "Former user"}
    write_side(src, [s]); write_side(tgt, [t])
    out = compare_project("AC", src, tgt)
    assert {"src": "Bob", "occurrences": 1} in out["stats"]["unmapped_users"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_compare.py -q`
Expected: `ModuleNotFoundError: No module named 'auditor.compare'`.

- [ ] **Step 3: Write the implementation**

`auditor/compare.py`:
```python
"""Field-by-field migration comparison for one project (port of compare.py).

No sampling: every common issue is compared on the SPECS ledger; presence is
split by the cutover line (missing keys numbered ABOVE the other side's max
key are post-cutover drift, not loss). Emits spec-shaped finding dicts plus a
stats block; persistence is the caller's job.
"""
from __future__ import annotations

import gzip
import json
from collections import Counter, defaultdict


def _load(path: str) -> dict:
    d = {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            d[r["key"]] = r["fields"]
    return d


def _nm(o):
    if isinstance(o, dict):
        return o.get("name") or o.get("value")
    return o


def _person(o):
    return o.get("displayName") if isinstance(o, dict) else None


def _nameset(lst):
    return sorted([(x.get("name") if isinstance(x, dict) else x)
                   for x in (lst or [])])


def _num(k: str) -> int:
    try:
        return int(k.split("-")[-1])
    except (ValueError, AttributeError):
        return -1


SPECS = [
    ("summary",        lambda f: f.get("summary"), "high"),
    ("issuetype",      lambda f: _nm(f.get("issuetype")), "high"),
    ("status",         lambda f: _nm(f.get("status")), "high"),
    ("priority",       lambda f: _nm(f.get("priority")), "med"),
    ("resolution",     lambda f: _nm(f.get("resolution")), "high"),
    ("resolutiondate", lambda f: f.get("resolutiondate"), "med"),
    ("created",        lambda f: f.get("created"), "high"),
    ("duedate",        lambda f: f.get("duedate"), "med"),
    ("labels",         lambda f: sorted(f.get("labels") or []), "med"),
    ("components",     lambda f: _nameset(f.get("components")), "med"),
    ("fixVersions",    lambda f: _nameset(f.get("fixVersions")), "med"),
    ("versions",       lambda f: _nameset(f.get("versions")), "med"),
    ("parent",         lambda f: (f.get("parent") or {}).get("key"), "high"),
    ("environment",    lambda f: (f.get("environment") or {}).get("sha"), "med"),
    ("security",       lambda f: _nm(f.get("security")), "high"),
    ("assignee",       lambda f: _person(f.get("assignee")), "high"),
    ("reporter",       lambda f: _person(f.get("reporter")), "high"),
    ("creator",        lambda f: _person(f.get("creator")), "med"),
    ("worklog_total",  lambda f: (f.get("worklog") or {}).get("total"), "med"),
    ("votes",          lambda f: (f.get("votes") or {}).get("votes"), "low"),
    ("watches",        lambda f: (f.get("watches") or {}).get("watchCount"), "low"),
    ("issuelinks",     lambda f: sorted(
        f"{l.get('type')}|{l.get('inward')}|{l.get('outward')}"
        for l in (f.get("issuelinks") or [])), "med"),
]
_REMAP_FIELDS = ("status", "issuetype", "priority")
_FORMER = {"Former user", "Former User"}


def _desc_sha(f):
    return (f.get("description") or {}).get("sha")


def _att_set(f):
    return sorted(f"{a.get('filename')}|{a.get('size')}"
                  for a in (f.get("attachment") or []))


def compare_project(project: str, src_path: str, tgt_path: str) -> dict:
    src, tgt = _load(src_path), _load(tgt_path)
    sk, tk = set(src), set(tgt)
    common = sorted(sk & tk, key=_num)
    missing = sorted(sk - tk, key=_num)
    extra = sorted(tk - sk, key=_num)
    tgt_max = max((_num(k) for k in tk), default=-1)
    src_max = max((_num(k) for k in sk), default=-1)

    findings: list[dict] = []
    # presence: source side
    for k in missing:
        if _num(k) > tgt_max:
            findings.append({"project": project, "kind": "tail_post_cutover",
                             "src_key": k, "tgt_key": None, "field": None,
                             "summary": (src[k].get("summary") or "")[:200],
                             "detail": {"direction": "source",
                                        "created": src[k].get("created"),
                                        "cutover_max_key": tgt_max}})
        else:
            findings.append({"project": project, "kind": "missing_in_tgt",
                             "src_key": k, "tgt_key": None, "field": None,
                             "summary": (src[k].get("summary") or "")[:200],
                             "detail": {"created": src[k].get("created"),
                                        "below_cutover_line": True}})
    # presence: target side
    for k in extra:
        if _num(k) > src_max:
            findings.append({"project": project, "kind": "tail_post_cutover",
                             "src_key": None, "tgt_key": k, "field": None,
                             "summary": (tgt[k].get("summary") or "")[:200],
                             "detail": {"direction": "target",
                                        "created": tgt[k].get("created"),
                                        "cutover_max_key": src_max}})
        else:
            findings.append({"project": project, "kind": "missing_in_src",
                             "src_key": None, "tgt_key": k, "field": None,
                             "summary": (tgt[k].get("summary") or "")[:200],
                             "detail": {"created": tgt[k].get("created")}})

    remap = {f: Counter() for f in _REMAP_FIELDS}
    user_pairs: Counter = Counter()
    unmapped: Counter = Counter()
    mismatch_issue_keys: set = set()
    sev_count: Counter = Counter()
    field_counts: Counter = Counter()

    for k in common:
        fs, ft = src[k], tgt[k]
        # collision: same key, different identity metadata (>=2 of 3 disagree)
        ident_diff = sum([
            fs.get("created") != ft.get("created"),
            _person(fs.get("reporter")) != _person(ft.get("reporter")),
            fs.get("summary") != ft.get("summary"),
        ])
        if ident_diff >= 2:
            findings.append({"project": project, "kind": "key_collision",
                             "src_key": k, "tgt_key": k, "field": None,
                             "summary": (fs.get("summary") or "")[:200],
                             "detail": {"src_created": fs.get("created"),
                                        "tgt_created": ft.get("created"),
                                        "src_reporter": _person(fs.get("reporter")),
                                        "tgt_reporter": _person(ft.get("reporter"))}})
            continue   # a collided pair's field diffs are meaningless noise
        for name, fn, sev in SPECS:
            a, b = fn(fs), fn(ft)
            if a != b:
                mismatch_issue_keys.add(k)
                sev_count[sev] += 1
                field_counts[name] += 1
                kind = "link_mismatch" if name == "issuelinks" else "field_mismatch"
                findings.append({"project": project, "kind": kind,
                                 "src_key": k, "tgt_key": k, "field": name,
                                 "summary": f"{name} differs",
                                 "detail": {"src": a, "tgt": b, "sev": sev}})
                if name in remap:
                    remap[name][(a, b)] += 1
        if _desc_sha(fs) != _desc_sha(ft):
            mismatch_issue_keys.add(k)
            findings.append({"project": project, "kind": "content_mismatch",
                             "src_key": k, "tgt_key": k, "field": "description",
                             "summary": "description content differs",
                             "detail": {"src_len": (fs.get("description") or {}).get("len"),
                                        "tgt_len": (ft.get("description") or {}).get("len")}})
        for role in ("assignee", "reporter", "creator"):
            a, b = _person(fs.get(role)), _person(ft.get(role))
            if a:
                user_pairs[(a, b)] += 1
                if b is None or b in _FORMER:
                    unmapped[a] += 1
        sc, tc = fs.get("comment") or {}, ft.get("comment") or {}
        cm_detail = None
        if sc.get("total") != tc.get("total"):
            cm_detail = {"src_total": sc.get("total"), "tgt_total": tc.get("total")}
        else:
            full = (sc.get("total") == sc.get("inline")
                    and tc.get("total") == tc.get("inline"))
            if full and (sc.get("total") or 0) > 0:
                s_sha = sorted(i["sha"] for i in sc.get("items", []))
                t_sha = sorted(i["sha"] for i in tc.get("items", []))
                if s_sha != t_sha:
                    cm_detail = {"content_differs": True,
                                 "total": sc.get("total")}
        if cm_detail:
            mismatch_issue_keys.add(k)
            findings.append({"project": project, "kind": "comment_mismatch",
                             "src_key": k, "tgt_key": k, "field": "comment",
                             "summary": "comment fidelity differs",
                             "detail": cm_detail})
        sa, ta = _att_set(fs), _att_set(ft)
        if sa != ta:
            mismatch_issue_keys.add(k)
            findings.append({"project": project, "kind": "attachment_mismatch",
                             "src_key": k, "tgt_key": k, "field": "attachment",
                             "summary": "attachments differ",
                             "detail": {"missing_in_tgt": sorted(set(sa) - set(ta)),
                                        "extra_in_tgt": sorted(set(ta) - set(sa))}})

    holes = sum(1 for f in findings if f["kind"] == "missing_in_tgt")
    tails = sum(1 for f in findings if f["kind"] == "tail_post_cutover")
    clean_common = len(common) - len(mismatch_issue_keys)
    fidelity = round(100.0 * clean_common / len(common), 2) if common else 100.0
    stats = {
        "project": project, "src": len(sk), "tgt": len(tk),
        "common": len(common), "missing_in_tgt": holes,
        "missing_in_src": sum(1 for f in findings if f["kind"] == "missing_in_src"),
        "tails": tails, "collisions": sum(1 for f in findings
                                          if f["kind"] == "key_collision"),
        "issues_with_mismatches": len(mismatch_issue_keys),
        "fidelity_pct": fidelity,
        "severity_totals": dict(sev_count),
        "field_mismatch_counts": dict(field_counts),
        "remap": {f: [{"src": s, "tgt": t, "count": c}
                      for (s, t), c in cnt.most_common(40)]
                  for f, cnt in remap.items() if cnt},
        "unmapped_users": [{"src": u, "occurrences": c}
                           for u, c in unmapped.most_common(60)],
        "distinct_src_people": len({a for (a, _) in user_pairs}),
    }
    return {"stats": stats, "findings": findings}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_compare.py -q`
Expected: `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add auditor/compare.py tests/test_compare.py
git commit -m "feat: fidelity comparison with cutover-tail split, collisions, content/comment/attachment checks"
```

## Post-review amendments (applied)

1. **Tails require overlap evidence.** A missing/extra key is only `tail_post_cutover` when `len(common) > 0`; with no overlap there is no cutover to infer, so an empty or fully re-keyed target reads as genuine loss/extra (`missing_in_tgt` / `missing_in_src`), not drift.
2. **Fidelity is collision-aware and honest about "nothing compared."** Each collided key now counts in `mismatch_issue_keys` (the collision is the strongest mismatch), and `fidelity_pct` is `None` (json null) when `common` is empty instead of a misleading 100.0.
3. **`created` is the collision invariant.** Replaced the loose >=2-of-3 rule: a collision fires only when `created` differs AND at least one of reporter/summary differs. `created` is immutable and migration-preserved; reporter/summary are legitimately mutable, so they corroborate but cannot out-vote it (a rename + edit alone is not a collision).
4. **`comment_uncheckable` accounting restored from the reference.** When comment totals match but either side has `total > inline` (API cap — content not fully captured), emit a `comment_uncheckable` finding and count it in the new `comments_uncheckable` stat. It is a coverage gap, not a mismatch: it does NOT enter `mismatch_issue_keys` and does NOT affect `fidelity_pct`.
5. **Content findings carry high severity.** `content_mismatch`, `comment_mismatch`, and `attachment_mismatch` add `"sev": "high"` to their detail and increment `sev_count["high"]`.
6. **Dropped the unused `defaultdict` import.**

---

