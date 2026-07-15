"""Confluence config audit = macro inventory (R7).

Macros with no Cloud equivalent are the #1 real-world Confluence migration
failure: a DC vendor macro the target cannot render silently degrades every
page that used it, yet page-level body fingerprints can't isolate WHICH
capability vanished. So the config stage inventories occurrences of every
`<ac:structured-macro ac:name="X">` per side and diffs the totals.

Unlike jira's config audit, this reads NO live admin API — there isn't one
for macro usage. The per-page counters recorded at extraction time
(fields["macros"], counted while the raw body was still in hand) are
aggregated from the run's own workspace extracts, which is why the
cross-product audit_config contract carries `workspace`. Aggregation is
run-wide across the selected spaces: a macro is one capability of the SITE,
so a per-space split would report the same missing plugin N times.

Findings are source-driven, mirroring the jira areas: source-present /
target-absent is a gap, a lower target count is a partial drop, and a
target-only macro is surfaced in the area summary but never becomes a
finding (target-side additions are not migration loss).
"""
from __future__ import annotations

import os
from collections import Counter
from typing import Callable

from ..compare import _load


def _side_totals(workspace: str, side: str, spaces) -> Counter:
    """Sum per-page macro counters over every selected space's extract.
    _load keeps the format-stamp skip single-sourced with compare."""
    total: Counter = Counter()
    for space in spaces:
        path = os.path.join(workspace, side, f"{space}.core.jsonl.gz")
        issues, _meta = _load(path)
        for fields in issues.values():
            total.update(fields.get("macros") or {})
    return total


def audit_macros(workspace: str, spaces: list[str],
                 progress: Callable[[str], None] | None = None) -> dict:
    """Same {"areas","findings"} contract as jira's audit_config."""
    say = progress or (lambda m: None)
    src = _side_totals(workspace, "src", spaces)
    tgt = _side_totals(workspace, "tgt", spaces)

    source_only = sorted(set(src) - set(tgt))
    target_only = sorted(set(tgt) - set(src))
    findings: list[dict] = []
    for name in sorted(src):
        s, t = src[name], tgt.get(name, 0)
        if t == 0:
            findings.append({"area": "macros", "name": name,
                             "kind": "missing_in_tgt",
                             "detail": {"src_occurrences": s}})
        elif t < s:
            findings.append({"area": "macros", "name": name,
                             "kind": "count_mismatch",
                             "detail": {"src": s, "tgt": t}})

    areas = {"macros": {
        "label": "macros",
        "src": len(src), "tgt": len(tgt),           # distinct names per side
        "in_both": len(set(src) & set(tgt)),
        "source_only": source_only,
        "target_only_count": len(target_only),
        "target_only": target_only,
        "by_macro": {name: {"src": src.get(name, 0), "tgt": tgt.get(name, 0)}
                     for name in sorted(set(src) | set(tgt))},
    }}
    say(f"[macros] src={len(src)} tgt={len(tgt)} "
        f"source-only={len(source_only)}")
    return {"areas": areas, "findings": findings}
