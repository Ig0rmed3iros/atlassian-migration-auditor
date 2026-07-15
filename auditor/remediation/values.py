"""Targeted per-issue value capture for missing custom fields (spec R2).

Bounded read: ONE combined scan across the audited issue population capturing
raw values for MANY custom fields at once. Output mirrors the extract convention
(gzip JSONL); only issues that actually carry a value are written."""
from __future__ import annotations

import gzip
import json
import os

from ..client import JiraClient, escape_query_key
from ..extract import _extract_page


def capture_fields_values(client: JiraClient, project_keys: list[str],
                          field_ids: list[str], out_dir: str) -> dict:
    """ONE combined scan of the audited issue population capturing raw values
    for MANY custom fields at once. Writes {field_id}.jsonl.gz per field under
    out_dir (one row per issue that carries a non-empty value). Returns
    {field_id: count}. Collapses N per-field scans into a single pass."""
    os.makedirs(out_dir, exist_ok=True)
    field_ids = list(dict.fromkeys(field_ids))   # dedup, preserve order
    counts = {fid: 0 for fid in field_ids}
    if not field_ids:
        return counts
    keys = " , ".join(f'"{escape_query_key(k)}"' for k in project_keys)
    jql = f"project in ({keys}) ORDER BY key ASC"
    tmp = {fid: os.path.join(out_dir, f"{fid}.jsonl.gz.tmp") for fid in field_ids}
    writers = {fid: gzip.open(tmp[fid], "wt", encoding="utf-8") for fid in field_ids}
    try:
        for iss in client.search_jql(jql, field_ids, page=_extract_page()):
            fields = iss.get("fields") or {}
            for fid in field_ids:
                val = fields.get(fid)
                if val in (None, "", [], {}):
                    continue
                writers[fid].write(json.dumps(
                    {"issue_key": iss["key"], "value": val}, default=str) + "\n")
                counts[fid] += 1
    finally:
        for w in writers.values():
            w.close()
    for fid in field_ids:
        os.replace(tmp[fid], os.path.join(out_dir, f"{fid}.jsonl.gz"))
    return counts


def capture_field_values(client: JiraClient, project_keys: list[str],
                         field_id: str, out_path: str) -> int:
    """Backward-compatible single-field capture (delegates to the combined
    scan). Writes to out_path (whatever its name)."""
    out_dir = os.path.dirname(out_path) or "."
    counts = capture_fields_values(client, project_keys, [field_id], out_dir)
    produced = os.path.join(out_dir, f"{field_id}.jsonl.gz")
    if os.path.abspath(produced) != os.path.abspath(out_path):
        os.replace(produced, out_path)
    return counts.get(field_id, 0)
