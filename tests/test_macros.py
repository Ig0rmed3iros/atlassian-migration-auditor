"""Unit tests for auditor.confluence.macros — the R7 macro inventory audit.

Synthetic data only (Acme / Globex / Igor Medeiros / placeholders).
"""
import gzip
import json
import os

import pytest

from auditor.confluence.macros import audit_macros
from auditor.extract import EXTRACT_FORMAT


def mk_row(title, macros):
    """A slim extract row with just what the macro audit reads."""
    return {"key": title, "id": title, "fields": {"title": title,
                                                  "macros": macros}}


def write_extract(workspace, side, space, rows, stamp=True):
    os.makedirs(os.path.join(workspace, side), exist_ok=True)
    path = os.path.join(workspace, side, f"{space}.core.jsonl.gz")
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        if stamp:
            fh.write(json.dumps({"_extract_format": EXTRACT_FORMAT}) + "\n")
        for r in rows:
            fh.write(json.dumps(r) + "\n")


@pytest.fixture()
def workspace(tmp_path):
    return str(tmp_path)


def test_audit_macros_missing_and_drop(workspace):
    """src {jira:5, toc:2, vendor-macro:3} vs tgt {jira:5, toc:1} →
    vendor-macro missing_in_tgt, toc count_mismatch, jira clean. Counts are
    summed across PAGES (the per-page Counter is the unit of extraction)."""
    write_extract(workspace, "src", "ENG", [
        mk_row("Acme Home", {"jira": 3, "toc": 2}),
        mk_row("Acme Ops", {"jira": 2, "vendor-macro": 3}),
    ])
    write_extract(workspace, "tgt", "ENG", [
        mk_row("Acme Home", {"jira": 5, "toc": 1}),
        mk_row("Acme Ops", {}),
    ])
    said = []
    out = audit_macros(workspace, ["ENG"], progress=said.append)

    by = {(f["kind"], f["name"]): f for f in out["findings"]}
    assert by[("missing_in_tgt", "vendor-macro")]["detail"] == \
        {"src_occurrences": 3}
    assert by[("missing_in_tgt", "vendor-macro")]["area"] == "macros"
    assert by[("count_mismatch", "toc")]["detail"] == {"src": 2, "tgt": 1}
    assert len(out["findings"]) == 2            # jira is clean: no finding

    a = out["areas"]["macros"]
    assert a["label"] == "macros"
    assert a["src"] == 3 and a["tgt"] == 2      # distinct macro names per side
    assert a["in_both"] == 2
    assert a["source_only"] == ["vendor-macro"]
    assert a["target_only"] == [] and a["target_only_count"] == 0
    assert a["by_macro"] == {"jira": {"src": 5, "tgt": 5},
                             "toc": {"src": 2, "tgt": 1},
                             "vendor-macro": {"src": 3, "tgt": 0}}
    assert any(m.startswith("[macros]") for m in said)


def test_audit_macros_sums_spaces_and_target_only_is_advisory(workspace):
    """Counters aggregate ACROSS spaces (one inventory per run, like the jira
    config areas); the format stamp line is skipped; a target-only macro is
    surfaced in the area summary but is never a finding — only source-side
    loss is a migration gap."""
    write_extract(workspace, "src", "ENG",
                  [mk_row("Acme Home", {"toc": 1})])
    write_extract(workspace, "src", "OPS",
                  [mk_row("Globex Runbook", {"toc": 2})], stamp=False)
    write_extract(workspace, "tgt", "ENG",
                  [mk_row("Acme Home", {"toc": 3, "globex-banner": 1})])
    write_extract(workspace, "tgt", "OPS", [])
    out = audit_macros(workspace, ["ENG", "OPS"])

    a = out["areas"]["macros"]
    assert a["by_macro"]["toc"] == {"src": 3, "tgt": 3}   # 1 + 2 across spaces
    assert a["target_only"] == ["globex-banner"]
    assert a["target_only_count"] == 1
    # toc matches once summed; globex-banner is target-side noise: no findings.
    assert out["findings"] == []
