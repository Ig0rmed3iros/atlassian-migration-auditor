### Task 9: `auditor/findings.py` — run summary + verdict

**Files:**
- Create: `auditor/findings.py`
- Test: `tests/test_findings.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_findings.py`:
```python
from auditor.findings import build_run_summary


def proj(missing=0, tails=0, collisions=0, mismatched=0, src=100, common=None):
    common = common if common is not None else src - missing - tails
    return {"stats": {"project": "AC", "src": src, "tgt": common,
                      "common": common, "missing_in_tgt": missing,
                      "missing_in_src": 0, "tails": tails,
                      "collisions": collisions,
                      "issues_with_mismatches": mismatched,
                      "fidelity_pct": round(100 * (common - mismatched) /
                                            common, 2) if common else 100.0}}


def cfg(n_missing=0):
    return {"areas": {}, "findings": [
        {"area": "statuses", "name": f"S{i}", "kind": "missing_in_tgt",
         "detail": {}} for i in range(n_missing)]}


def test_clean():
    s = build_run_summary({"AC": proj()}, cfg(), [])
    assert s["verdict"] == "CLEAN"
    assert s["stats"]["issues_src_total"] == 100


def test_tails_only_is_clean_with_tails():
    s = build_run_summary({"AC": proj(tails=5)}, cfg(), [])
    assert s["verdict"] == "CLEAN_WITH_TAILS"
    assert any("tail" in h.lower() for h in s["headlines"])


def test_mismatches_or_config_gaps_are_gaps_found():
    assert build_run_summary({"AC": proj(mismatched=3)}, cfg(), [])["verdict"] \
        == "GAPS_FOUND"
    assert build_run_summary({"AC": proj()}, cfg(5), [])["verdict"] == "GAPS_FOUND"


def test_holes_collisions_or_blindspots_are_critical():
    assert build_run_summary({"AC": proj(missing=2)}, cfg(), [])["verdict"] \
        == "CRITICAL"
    assert build_run_summary({"AC": proj(collisions=1)}, cfg(), [])["verdict"] \
        == "CRITICAL"
    bs = [{"key": "MS", "search_count": 0, "insight_count": 16016,
           "blind_spot": True}]
    s = build_run_summary({"AC": proj()}, cfg(), bs)
    assert s["verdict"] == "CRITICAL"
    assert any("blind" in h.lower() for h in s["headlines"])


def test_headlines_name_the_worst_project():
    s = build_run_summary({"AC": proj(missing=7)}, cfg(), [])
    assert any("AC" in h and "7" in h for h in s["headlines"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_findings.py -q`
Expected: `ModuleNotFoundError: No module named 'auditor.findings'`.

- [ ] **Step 3: Write the implementation**

`auditor/findings.py`:
```python
"""Run-level summary: aggregate stats, verdict, and prose headlines.

Verdict ladder (worst wins):
  CRITICAL          genuine holes (missing below the cutover line), key
                    collisions, or unresolved permission blind spots —
                    the audit cannot be called clean.
  GAPS_FOUND        field/content mismatches or config objects missing in
                    the target. Data is present but not faithful/complete.
  CLEAN_WITH_TAILS  only post-cutover drift (expected when a source stays
                    live after the snapshot).
  CLEAN             nothing found.
"""
from __future__ import annotations


def build_run_summary(project_results: dict, config_result: dict,
                      blind_spots: list) -> dict:
    stats_list = [r["stats"] for r in project_results.values()]
    holes = sum(s.get("missing_in_tgt", 0) for s in stats_list)
    tails = sum(s.get("tails", 0) for s in stats_list)
    collisions = sum(s.get("collisions", 0) for s in stats_list)
    mismatched = sum(s.get("issues_with_mismatches", 0) for s in stats_list)
    cfg_missing = sum(1 for f in config_result.get("findings", [])
                      if f["kind"] == "missing_in_tgt")
    cfg_other = sum(1 for f in config_result.get("findings", [])
                    if f["kind"] != "missing_in_tgt")
    live_blind = [b for b in blind_spots if b.get("blind_spot")]

    if holes or collisions or live_blind:
        verdict = "CRITICAL"
    elif mismatched or cfg_missing or cfg_other:
        verdict = "GAPS_FOUND"
    elif tails:
        verdict = "CLEAN_WITH_TAILS"
    else:
        verdict = "CLEAN"

    headlines: list[str] = []
    for b in live_blind:
        headlines.append(
            f"Permission blind spot on {b['key']}: search sees "
            f"{b.get('search_count')} of {b.get('insight_count')} issues. "
            f"Counts below it are NOT trustworthy until access is fixed.")
    worst = sorted(stats_list, key=lambda s: -s.get("missing_in_tgt", 0))
    if worst and worst[0].get("missing_in_tgt"):
        w = worst[0]
        headlines.append(
            f"{w['project']} has {w['missing_in_tgt']} issues missing in the "
            f"target below the cutover line. This is genuine data loss until "
            f"proven otherwise.")
    if collisions:
        headlines.append(
            f"{collisions} key collision(s): same key, different issue "
            f"identity on each side. Treat matched-field stats for those "
            f"keys as noise.")
    if tails and not holes:
        headlines.append(
            f"{tails} issue(s) exist only as post-cutover tail (created "
            f"after the snapshot). Expected drift, not loss.")
    if mismatched:
        headlines.append(
            f"{mismatched} migrated issue(s) have at least one field or "
            f"content difference.")
    if cfg_missing:
        headlines.append(
            f"{cfg_missing} config object(s) from the source are missing in "
            f"the target (statuses, fields, screens, schemes or JSM objects).")
    if not headlines:
        headlines.append("Every audited issue and config object matched. "
                         "Clean migration.")

    return {
        "stats": {
            "projects": len(stats_list),
            "issues_src_total": sum(s.get("src", 0) for s in stats_list),
            "issues_tgt_total": sum(s.get("tgt", 0) for s in stats_list),
            "holes": holes, "tails": tails, "collisions": collisions,
            "issues_with_mismatches": mismatched,
            "config_missing": cfg_missing, "config_other": cfg_other,
            "blind_spots": len(live_blind),
        },
        "verdict": verdict,
        "headlines": headlines,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_findings.py -q`
Expected: `5 passed`.

- [ ] **Step 5: Commit**

```bash
git add auditor/findings.py tests/test_findings.py
git commit -m "feat: run summary with verdict ladder and prose headlines"
```

---


> NOTE from Task 7 review: stats now include comments_uncheckable, and fidelity_pct can be None — findings.py must not assume it is a number.

> NOTE from Task 8 review: a config finding kind area_error means a side was unreachable — findings.py verdict MUST treat any area_error as at least GAPS_FOUND (cannot certify a side it couldn't read).

## Post-review amendments (applied)

**orphans + comments_uncheckable advisory headlines (verdict unchanged)**

`build_run_summary` now aggregates two additional totals from per-project stats:
- `orphans = sum(s.get("missing_in_src", 0) ...)` — issues present on the target but absent from the source (over-migration or target-side edits).
- `comments_uncheckable = sum(s.get("comments_uncheckable", 0) ...)` — issues whose comment content could not be fully verified because the API returned fewer comments than exist.

Both are exposed in the returned `stats` dict and generate advisory prose headlines appended before the `"Clean migration."` fallback, so a run with only orphans/uncheckable no longer prints that false-clean message.

Crucially, neither value enters the verdict ladder. A run with only orphans is still `CLEAN` (no source data was lost) but now carries the advisory headline instead of the misleading clean summary.

Test additions (`tests/test_findings.py`):
- `test_orphans_surface_as_headline_without_changing_verdict` — `missing_in_src=5` → verdict stays `CLEAN`, `stats["orphans"]==5`, advisory headline present, no false-clean.
- `test_uncheckable_comments_surface_as_headline` — `comments_uncheckable=12` → `stats["comments_uncheckable"]==12`, advisory headline present.

Full suite: 85 passed.
