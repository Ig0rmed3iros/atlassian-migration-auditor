### Task 16: Demo seed + end-to-end verification

**Files:**
- Create: `scripts/seed_demo.py`

A synthetic-data seeder so the UI can be demoed/verified without real Jira credentials (placeholder companies only, per the synthetic-data rule).

- [ ] **Step 1: Write the seeder**

`scripts/seed_demo.py`:
```python
"""Seed a synthetic finished run so the analysis UI can be inspected without
real credentials. Usage: python3 scripts/seed_demo.py  (uses MA_DATA_DIR)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from webapp.config import load_config
from webapp.store import Store

cfg = load_config()
os.makedirs(cfg.data_dir, exist_ok=True)
store = Store(db_path=cfg.db_path, key_path=cfg.key_path,
              secret_key=cfg.secret_key)
mid = store.create_migration("Acme DC -> Globex Cloud (demo)")
store.save_connection(mid, "source", "pat", "https://acme.atlassian.net",
                      secret={"email": "demo@acme.test", "token": "demo"},
                      account_email="demo@acme.test")
store.save_connection(mid, "target", "pat", "https://globex.atlassian.net",
                      secret={"email": "demo@globex.test", "token": "demo"},
                      account_email="demo@globex.test")
rid = store.create_run(mid, {"projects": ["SUP", "ENG"]})
store.set_run_projects(rid, [
    {"key": "SUP", "name": "Support", "src_count": 16022, "tgt_count": 15105,
     "missing": 0, "tail_count": 917, "fidelity_pct": 99.4, "blind_spot": 0,
     "status": "compared"},
    {"key": "ENG", "name": "Engineering", "src_count": 40092, "tgt_count": 39997,
     "missing": 3, "tail_count": 92, "fidelity_pct": 97.1, "blind_spot": 0,
     "status": "compared"}])
issues = []
for i in range(3):
    issues.append({"project": "ENG", "kind": "missing_in_tgt",
                   "src_key": f"ENG-{100 + i}", "tgt_key": None, "field": None,
                   "summary": f"Lost issue example {i}",
                   "detail": {"below_cutover_line": True}})
for i in range(40):
    issues.append({"project": "SUP", "kind": "tail_post_cutover",
                   "src_key": f"SUP-{16000 + i}", "tgt_key": None, "field": None,
                   "summary": f"Post-cutover ticket {i}",
                   "detail": {"direction": "source"}})
for i in range(25):
    issues.append({"project": "ENG", "kind": "field_mismatch",
                   "src_key": f"ENG-{200 + i}", "tgt_key": f"ENG-{200 + i}",
                   "field": "status", "summary": "status differs",
                   "detail": {"src": "On Hold", "tgt": "To Do", "sev": "high"}})
store.insert_findings_issue(rid, issues)
store.insert_findings_config(rid, [
    {"area": "statuses", "name": n, "kind": "missing_in_tgt", "detail": {}}
    for n in ("On Hold", "Waiting", "RCA")] + [
    {"area": "custom_fields", "name": "Squad", "kind": "type_mismatch",
     "detail": {"src_type": "select", "tgt_type": "textfield"}}])
for msg in ("source: authenticated", "2 projects in scope",
            "ENG: common=39994 holes=3 tails=92", "run complete"):
    store.add_event(rid, "demo", "info", msg)
store.update_run(rid, status="done", verdict="CRITICAL", stats={
    "projects": 2, "issues_src_total": 56114, "issues_tgt_total": 55102,
    "holes": 3, "tails": 1009, "collisions": 0, "issues_with_mismatches": 25,
    "config_missing": 3, "config_other": 1, "blind_spots": 0,
    "headlines": [
        "ENG has 3 issues missing in the target below the cutover line. "
        "This is genuine data loss until proven otherwise.",
        "1,009 issue(s) exist only as post-cutover tail. Expected drift."],
    "project_stats": {"SUP": {"src": 16022, "tgt": 15105, "fidelity_pct": 99.4},
                       "ENG": {"src": 40092, "tgt": 39997, "fidelity_pct": 97.1}},
    "areas": {"statuses": {"src": 74, "tgt": 42}}})
print(f"Seeded migration {mid}, run {rid}. Start the app and open "
      f"http://localhost:8484/runs/{rid}/analysis")
```

- [ ] **Step 2: Run the seeder + full suite**

```bash
python3 scripts/seed_demo.py
python3 -m pytest -q
```
Expected: seeder prints the analysis URL; full suite passes.

- [ ] **Step 3: Manual smoke (visual verification — REQUIRED)**

```bash
migration-auditor serve &
sleep 2
```
Then verify with Playwright (python) or a browser: open `http://localhost:8484`,
the seeded migration page, the run page, and every analysis view
(`/analysis`, `/analysis/projects`, `/analysis/projects/ENG`, `/analysis/config`,
`/analysis/issues`, `/analysis/log`). Confirm: verdict banner renders CRITICAL,
KPI cards populated, findings tables paginate/filter, issue keys link to
`https://acme.atlassian.net/browse/...`. Kill the server afterwards.

- [ ] **Step 4: Commit**

```bash
git add scripts/seed_demo.py
git commit -m "chore: synthetic demo seeder for UI verification"
```

---

## Final acceptance checklist

- [ ] `python3 -m pytest -q` — entire suite green
- [ ] `migration-auditor serve` boots; dashboard loads
- [ ] Seeded analysis pages all render with data
- [ ] No real customer names anywhere in code/fixtures (synthetic only)
- [ ] `data/` is gitignored; no tokens in any committed file

---

## Final-review amendments (applied)

After the final review gate, five fixes were applied:

- **Fix 1 (safety guarantee): auto-undo elevation at run end.** Added
  `undo_migration_elevations(store, migration_id, src, tgt, log=None)` in
  `webapp/stages.py`. It undoes every still-active elevation recorded for ANY
  run of the migration (migration-scoped, not run-scoped) — provably safe
  because the active-run guard means only one run per migration is ever
  in-flight, so it can never strip a grant a live run needs. `RunEngine.__init__`
  now takes an injected `elevation_undo` callable (default no-op); `finalize`
  calls it after the run is marked done, and the failure (`except`) path calls
  it best-effort inside a try/except. `create_app` wires the real callable,
  emitting a `finalize` event per de-granted side. This bounds the privilege
  window to ≤1 run and is self-healing. Tests:
  `test_finalize_invokes_elevation_undo` (runs) and
  `test_undo_migration_elevations_clears_grants` (stages-pipeline).
- **Fix 2 (fail-loud gap): surface indeterminate blind spots.**
  `stage_permissions` now emits a `COULD NOT VERIFY …` warn event for projects
  flagged `indeterminate` (count lookup errored while insight reports issues
  exist), not just hard blind spots. Test:
  `test_stage_permissions_warns_on_indeterminate`.
- **Fix 3 (guard): `role_id is None` in `elevate_apply`.** If no Administrators
  role is found, emit a warn event and redirect back to the elevate page
  WITHOUT calling `apply_elevation`.
- **Fix 4 (guard): unknown `run_id` in elevate routes.** `elevate_confirm`,
  `elevate_apply`, and `elevate_undo` redirect to `/` when `store.get_run`
  returns `None`, before dereferencing `run`.
- **Fix 5 (comment): safe-erring config sub-probes.** Documented in
  `_field_options` and `_screen_fields` that a discarded pagination error errs
  safe (outage → empty set → at worst a false option/field mismatch that
  over-reports to GAPS_FOUND, never a false CLEAN); v1-acceptable.
