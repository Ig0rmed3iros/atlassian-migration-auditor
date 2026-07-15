"""Run-over-run diff: what changed between two audits of the same target.

Powers the audit -> fix -> re-audit loop — "did my fixes land, and what's new?".
Findings are matched by a stable signature (env: kind+area+name; migration:
project+kind+src_key+field), so a finding that disappears is RESOLVED, a new one
is NEW, and a recurring one is UNCHANGED. Reuses the export row assembly.
"""
from __future__ import annotations

from webapp.export import _env_rows, _migration_rows


def _rows_for(store, run_id):
    """(audit_type, rows) for a run, or None when the run is missing or has no
    diffable findings (a fix run)."""
    run = store.get_run(run_id)
    if run is None:
        return None
    if run.get("kind") == "env_audit":
        return "env", _env_rows(store, run_id)[1]
    if run.get("kind") == "audit":
        return "migration", _migration_rows(store, run_id)[1]
    return None


def _sig(row, audit_type):
    if audit_type == "env":
        return (row.get("kind"), row.get("area"), row.get("name"))
    return (row.get("project"), row.get("kind"), row.get("src_key"),
            row.get("field"))


def compare_runs(store, base_id, new_id):
    """Diff two runs. Returns None if either run is missing/undiffable or the two
    are different audit types (not comparable)."""
    base = _rows_for(store, base_id)
    new = _rows_for(store, new_id)
    if base is None or new is None or base[0] != new[0]:
        return None
    audit_type = new[0]
    base_sigs = {_sig(r, audit_type): r for r in base[1]}
    new_sigs = {_sig(r, audit_type): r for r in new[1]}
    return {
        "audit_type": audit_type,
        "base_run_id": base_id, "new_run_id": new_id,
        "new": [r for s, r in new_sigs.items() if s not in base_sigs],
        "resolved": [r for s, r in base_sigs.items() if s not in new_sigs],
        "unchanged_count": sum(1 for s in new_sigs if s in base_sigs),
        "base_count": len(base[1]), "new_count": len(new[1]),
    }


def candidate_base_runs(store, run):
    """Prior DONE runs of the SAME migration + same kind, older than `run` — the
    valid baselines to diff against (newest first)."""
    return [r for r in store.list_runs(run["migration_id"])
            if r["id"] < run["id"] and r.get("kind") == run.get("kind")
            and r.get("status") == "done"]
