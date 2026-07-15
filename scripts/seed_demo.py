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
