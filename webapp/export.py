"""CSV / JSON export of a run's findings.

A local download for spreadsheets, ticketing, or CI — works for BOTH audit types
(environment audits read findings_config; migration audits read findings_issue),
each with the column set that makes sense for it. Local-only: the operator's own
audit data, never transmitted.
"""
from __future__ import annotations

import csv
import io
import json

from auditor.envaudit.fixes import category_for

_ENV_FIELDS = ["area", "name", "kind", "severity", "category", "fix_tier"]
_MIGRATION_FIELDS = ["project", "kind", "field", "src_key", "tgt_key",
                     "severity", "summary"]


def _env_rows(store, run_id):
    rows = []
    for area in store.config_areas(run_id):
        for r in store.query_config(run_id, area):
            d = r.get("detail") or {}
            fix = d.get("fix") if isinstance(d.get("fix"), dict) else {}
            rows.append({
                "area": r.get("area") or "",
                "name": r.get("name") or "",
                "kind": r.get("kind") or "",
                "severity": d.get("severity") or "",
                "category": d.get("category") or category_for(r.get("kind") or ""),
                "fix_tier": fix.get("tier") or "",
            })
    return _ENV_FIELDS, rows


def _migration_rows(store, run_id):
    rows = []
    for r in store.all_issue_findings(run_id):
        # Severity lives in detail_json["sev"] (findings_issue has no severity
        # column); parse it so the export/diff aren't blank for every row.
        try:
            sev = (json.loads(r.get("detail_json") or "{}") or {}).get("sev")
        except (ValueError, TypeError):
            sev = None
        rows.append({
            "project": r.get("project") or "",
            "kind": r.get("kind") or "",
            "field": r.get("field") or "",
            "src_key": r.get("src_key") or "",
            "tgt_key": r.get("tgt_key") or "",
            "severity": sev or "",
            "summary": r.get("summary") or "",
        })
    return _MIGRATION_FIELDS, rows


def export_findings(store, run_id):
    """Return (fields, rows) for the run, shaped by audit type, or None when the
    run is missing."""
    run = store.get_run(run_id)
    if run is None:
        return None
    if run.get("kind") == "env_audit":
        return _env_rows(store, run_id)
    return _migration_rows(store, run_id)


def rows_to_csv(fields, rows) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore",
                       lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()
