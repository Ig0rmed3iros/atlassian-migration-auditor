"""Display-time fidelity derivation (pure, no web/DB imports).

The audit compute hot path (auditor/compare.py) stores every per-issue mismatch
verbatim and a single `issues_with_mismatches` count per project. That raw count
treats a SYSTEMATIC GAP — one field that a migration tool silently dropped on
nearly every issue (e.g. `environment` left empty on the target) — the same as a
genuine per-issue data fault. The result is a deflated "fidelity" number that is
dominated by one known, explainable hole.

`derive_fidelity` re-reads the ALREADY-STORED findings and splits them:

  * SYSTEMATIC GAPS — a field that is empty on the target across a large,
    near-total slice of a project's issues. These are surfaced separately (so an
    operator sees "environment was dropped on 4,012 issues" as one line) and are
    EXCLUDED from core fidelity.
  * CORE fidelity — fidelity computed only over issues that have at least one
    mismatch which is NOT on a systematic-gap field. An issue that also carries a
    real mismatch still counts; only issues whose *every* mismatch is a
    systematic-gap field are forgiven.

This is a derivation over stored data: it never re-runs the audit and never
changes verdict strings. The raw value is always preserved alongside the core
value so nothing is hidden.
"""
from __future__ import annotations

from collections import Counter, defaultdict

# ── Tunable constants (documented) ───────────────────────────────────────────
# A field is a systematic gap for a project only if it is mismatched on a large
# enough share of that project's issues AND the target side is overwhelmingly
# empty (the signature of a dropped field, not a remap).
SYS_FRAC = 0.30          # >= 30% of the project's common (compared) issues, and
SYS_ABS_FLOOR = 200      # at least this many distinct issues in absolute terms,
SYS_TARGET_EMPTY = 0.85  # and >= 85% of those have an EMPTY target value.

# Kinds whose `field` participates in systematic-gap detection. content (description),
# comment and attachment mismatches are their OWN kinds and are never treated as
# "fields" — they always count toward core fidelity.
_FIELD_LIKE_KINDS = ("field_mismatch", "link_mismatch")


def _is_empty(v) -> bool:
    """A target value counts as empty if it is None, an empty/whitespace string,
    or an empty collection. Zero and False are NOT empty (legitimate values)."""
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip() == ""
    if isinstance(v, (list, tuple, dict, set)):
        return len(v) == 0
    return False


def _issue_key(f: dict):
    """Stable per-issue identity for a mismatch finding. field/link mismatches
    carry the same key on src and tgt; prefer src_key, fall back to tgt_key."""
    return f.get("src_key") or f.get("tgt_key")


def derive_fidelity(project_rows: list[dict],
                    issue_findings: list[dict],
                    audited: int | None = None) -> dict:
    """Derive systematic gaps and core fidelity from stored findings.

    Args:
      project_rows: per-project dicts with at least `key`, `common`
        (issues compared), `fidelity_pct` (the raw stored fidelity, may be None)
        and `issues_with_mismatches` (the raw mismatched count).
      issue_findings: stored findings_issue rows with `project`, `kind`,
        `field`, `src_key`, `tgt_key` and a parsed `detail` dict. For field/link
        mismatches the real values live in detail['src'] / detail['tgt'].
      audited: the run-level source-issue total used as the OVERALL fidelity
        denominator (the natural base, matching holes/tails which are also
        run-level). Falls back to the sum of per-project compared ("common")
        issues when not supplied, for back-compat.

    Returns:
      {systematic_gaps, overall, per_project} — see module docstring.
    """
    rows_by_key = {r["key"]: r for r in project_rows}

    # group findings by project ------------------------------------------------
    by_project: dict[str, list[dict]] = defaultdict(list)
    holes = 0
    tails = 0
    for f in issue_findings:
        kind = f.get("kind")
        if kind == "missing_in_tgt":
            holes += 1
        elif kind == "tail_post_cutover":
            # Only SOURCE-direction tails belong to the audited population:
            # the overall denominator is the run-level SOURCE item total, and
            # an item born on the live target after cutover was never part of
            # it. Subtracting target tails drove a perfect migration's
            # headline fidelity arbitrarily below zero on any live target.
            detail = f.get("detail") or {}
            direction = detail.get("direction")
            if direction == "source" or (direction is None and f.get("src_key")):
                tails += 1
        by_project[f.get("project")].append(f)

    systematic_gaps: list[dict] = []
    per_project: list[dict] = []
    core_mismatched_total = 0

    # iterate projects in stored order, then any project that only appears in
    # findings (defensive — should not happen, but keeps the function total).
    keys = list(rows_by_key.keys())
    for p in by_project:
        if p not in rows_by_key:
            keys.append(p)

    for key in keys:
        row = rows_by_key.get(key, {})
        common = row.get("common") or 0
        raw_fidelity = row.get("fidelity_pct")
        findings = by_project.get(key, [])

        # ── per-field tallies for field-like mismatches ──────────────────────
        # field -> {distinct issue keys}, field -> empty-target count,
        # field -> Counter(src_value) for issues with empty target.
        field_issues: dict[str, set] = defaultdict(set)
        field_empty: dict[str, set] = defaultdict(set)
        field_src_when_empty: dict[str, Counter] = defaultdict(Counter)
        for f in findings:
            if f.get("kind") not in _FIELD_LIKE_KINDS:
                continue
            field = f.get("field")
            if not field:
                continue
            ik = _issue_key(f)
            field_issues[field].add(ik)
            detail = f.get("detail") or {}
            if _is_empty(detail.get("tgt")):
                field_empty[field].add(ik)
                field_src_when_empty[field][_src_label(detail.get("src"))] += 1

        # ── decide which fields are systematic gaps for this project ─────────
        threshold = max(SYS_ABS_FLOOR, SYS_FRAC * common)
        systematic_fields: set = set()
        for field, issues in field_issues.items():
            affected = len(issues)
            empty = len(field_empty[field])
            empty_frac = (empty / affected) if affected else 0.0
            if affected >= threshold and empty_frac >= SYS_TARGET_EMPTY:
                systematic_fields.add(field)
                top = field_src_when_empty[field].most_common(1)
                if top:
                    src_val, cnt = top[0]
                    top_pattern = f"{src_val} -> (empty) x{cnt}"
                else:
                    top_pattern = "(empty) -> (empty) x0"
                systematic_gaps.append({
                    "project": key,
                    "field": field,
                    "affected_issues": affected,
                    "target_empty_pct": round(100.0 * empty_frac, 2),
                    "top_pattern": top_pattern,
                })

        # ── core mismatch set: issues with >=1 NON-systematic mismatch ───────
        # An issue is forgiven only if EVERY one of its mismatch findings is on a
        # systematic-gap field. content/comment/attachment kinds are never
        # systematic, so they always make an issue core.
        issues_all_mismatch: set = set()
        issues_core: set = set()
        for f in findings:
            kind = f.get("kind")
            if kind in ("missing_in_tgt", "tail_post_cutover", "missing_in_src",
                        "comment_uncheckable", "attachment_uncheckable"):
                continue  # presence/coverage, not a fidelity mismatch
            ik = _issue_key(f)
            if ik is None:
                continue
            issues_all_mismatch.add(ik)
            is_systematic = (kind in _FIELD_LIKE_KINDS
                             and f.get("field") in systematic_fields)
            if not is_systematic:
                issues_core.add(ik)

        core_mismatched = len(issues_core)
        core_mismatched_total += core_mismatched

        if common:
            fidelity_core = round(100.0 * (common - core_mismatched) / common, 2)
        else:
            fidelity_core = None

        per_project.append({
            "key": key,
            "fidelity_core": fidelity_core,
            "fidelity_raw": raw_fidelity,
            "common": common,
            "core_mismatched": core_mismatched,
            "mismatched": row.get("issues_with_mismatches",
                                  len(issues_all_mismatch)),
        })

    # ── overall fidelity ─────────────────────────────────────────────────────
    # Denominator = the run-level source total when provided (matches the raw
    # frontend formula and the holes/tails buckets, which are run-level); else
    # fall back to the sum of per-project compared issues.
    if audited is None:
        audited = sum((r.get("common") or 0) for r in project_rows)
    issues_with_mismatches = sum(
        (r.get("issues_with_mismatches") or 0) for r in project_rows)

    if audited:
        fidelity_core = round(
            100.0 * (audited - holes - tails - core_mismatched_total)
            / audited, 2)
        fidelity_raw = round(
            100.0 * (audited - holes - tails - issues_with_mismatches)
            / audited, 2)
    else:
        fidelity_core = None
        fidelity_raw = None

    return {
        "systematic_gaps": systematic_gaps,
        "overall": {
            "fidelity_core": fidelity_core,
            "fidelity_raw": fidelity_raw,
            "core_mismatched_total": core_mismatched_total,
        },
        "per_project": per_project,
    }


def derive_fidelity_from_counts(
        project_rows: list[dict],
        finding_counts: dict,
        audited: int | None = None,
        *,
        _store=None,
        _run_id: int | None = None) -> dict:
    """Derive systematic gaps and core fidelity from SQL aggregates (no full row scan).

    This is the performance-efficient sibling of derive_fidelity. Instead of
    loading every finding into Python, it consumes aggregated counts returned by
    Store.issue_finding_counts() and Store.core_mismatch_counts().

    Args:
      project_rows: same as derive_fidelity — per-project dicts with `key`,
        `common`, `fidelity_pct` and `issues_with_mismatches`.
      finding_counts: dict returned by Store.issue_finding_counts():
        {holes: int, src_tails: int, field_agg: list[dict]}
        where each field_agg item has {project, kind, field,
        affected_issues, empty_issues}.
      audited: run-level source-issue total (same semantics as derive_fidelity).
      _store / _run_id: injected by the caller (analysis.py) so this function
        can call store.core_mismatch_counts() to compute core_mismatched per
        project after systematic fields are identified. Both must be provided
        together; if absent the function falls back to 0 for core_mismatched
        (conservative — raw == core in that case).

    Returns identical shape to derive_fidelity: {systematic_gaps, overall, per_project}.
    """
    rows_by_key = {r["key"]: r for r in project_rows}

    holes: int = finding_counts.get("holes") or 0
    tails: int = finding_counts.get("src_tails") or 0
    field_agg: list[dict] = finding_counts.get("field_agg") or []
    src_patterns: list[dict] = finding_counts.get("src_patterns") or []

    # ── group field aggregates by project ────────────────────────────────────
    by_project_field: dict[str, list[dict]] = defaultdict(list)
    for row in field_agg:
        by_project_field[row["project"]].append(row)

    # ── build per-(project, field) Counter of src values from src_patterns ───
    # src_patterns rows: {project, field, src_val, cnt}
    # Used to reproduce top_pattern for systematic gaps.
    src_counter: dict[tuple, Counter] = defaultdict(Counter)
    for sp in src_patterns:
        key_pf = (sp["project"], sp["field"])
        src_val = sp["src_val"]
        # json_extract returns bare scalar text; None for JSON null
        if src_val is None or src_val == "null":
            src_val = None  # normalise
        src_counter[key_pf][src_val] = (src_counter[key_pf][src_val]
                                         + (sp["cnt"] or 0))

    systematic_gaps: list[dict] = []
    per_project: list[dict] = []
    systematic_by_project: dict[str, set] = {}  # project -> set of systematic fields

    keys = list(rows_by_key.keys())
    for p in by_project_field:
        if p not in rows_by_key:
            keys.append(p)

    for key in keys:
        row = rows_by_key.get(key, {})
        common = row.get("common") or 0
        raw_fidelity = row.get("fidelity_pct")
        field_rows = by_project_field.get(key, [])

        # ── systematic-gap detection ─────────────────────────────────────────
        threshold = max(SYS_ABS_FLOOR, SYS_FRAC * common)
        systematic_fields: set = set()
        for fr in field_rows:
            affected = fr["affected_issues"] or 0
            empty = fr["empty_issues"] or 0
            empty_frac = (empty / affected) if affected else 0.0
            if affected >= threshold and empty_frac >= SYS_TARGET_EMPTY:
                systematic_fields.add(fr["field"])
                # Compute top_pattern from the src_patterns data
                pf_key = (key, fr["field"])
                ctr = src_counter.get(pf_key, Counter())
                top = ctr.most_common(1)
                if top:
                    raw_src, cnt = top[0]
                    src_label = _src_label(raw_src)
                    top_pattern = f"{src_label} -> (empty) x{cnt}"
                else:
                    top_pattern = "(empty) -> (empty) x0"
                systematic_gaps.append({
                    "project": key,
                    "field": fr["field"],
                    "affected_issues": affected,
                    "target_empty_pct": round(100.0 * empty_frac, 2),
                    "top_pattern": top_pattern,
                })

        if systematic_fields:
            systematic_by_project[key] = systematic_fields

        per_project.append({
            "key": key,
            "fidelity_core": None,   # filled in below after core_mismatch_counts
            "fidelity_raw": raw_fidelity,
            "common": common,
            "core_mismatched": 0,    # filled in below
            "mismatched": row.get("issues_with_mismatches", 0),
        })

    # ── core mismatch counts (second SQL pass) ───────────────────────────────
    if _store is not None and _run_id is not None:
        core_by_proj = _store.core_mismatch_counts(_run_id, systematic_by_project)
    else:
        core_by_proj = {}

    core_mismatched_total = 0
    for pp in per_project:
        key = pp["key"]
        common = pp["common"]
        core_mismatched = core_by_proj.get(key, 0)
        pp["core_mismatched"] = core_mismatched
        core_mismatched_total += core_mismatched
        if common:
            pp["fidelity_core"] = round(
                100.0 * (common - core_mismatched) / common, 2)
        else:
            pp["fidelity_core"] = None

    # ── overall fidelity ─────────────────────────────────────────────────────
    if audited is None:
        audited = sum((r.get("common") or 0) for r in project_rows)
    issues_with_mismatches = sum(
        (r.get("issues_with_mismatches") or 0) for r in project_rows)

    if audited:
        fidelity_core = round(
            100.0 * (audited - holes - tails - core_mismatched_total)
            / audited, 2)
        fidelity_raw = round(
            100.0 * (audited - holes - tails - issues_with_mismatches)
            / audited, 2)
    else:
        fidelity_core = None
        fidelity_raw = None

    return {
        "systematic_gaps": systematic_gaps,
        "overall": {
            "fidelity_core": fidelity_core,
            "fidelity_raw": fidelity_raw,
            "core_mismatched_total": core_mismatched_total,
        },
        "per_project": per_project,
    }


def _src_label(v) -> str:
    """Render a source value for the top_pattern string. Empty source becomes a
    readable token rather than a blank."""
    if _is_empty(v):
        return "(empty)"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v)


def _project_rows_from_stats(stats: dict) -> list[dict]:
    """Build derive_fidelity_from_counts' project_rows from a run's stored
    project_stats block (key + common + raw fidelity + mismatch count)."""
    rows = []
    for key, ps in (stats.get("project_stats") or {}).items():
        ps = ps or {}
        rows.append({"key": key,
                     "common": ps.get("common") or 0,
                     "fidelity_pct": ps.get("fidelity_pct"),
                     "issues_with_mismatches": ps.get("issues_with_mismatches") or 0})
    return rows


def compute_run_fidelity(store, run_id: int, stats: dict) -> dict:
    """Full derived-fidelity dict (overall, per_project, systematic_gaps) for a
    run, from SQL aggregates. Computed ONCE at finalize and cached in stats so
    the analysis page never re-aggregates a 400k-finding run on every view; the
    summary route falls back to calling this live only for pre-cache runs."""
    return derive_fidelity_from_counts(
        _project_rows_from_stats(stats),
        store.issue_finding_counts(run_id),
        audited=stats.get("issues_src_total"),
        _store=store, _run_id=run_id)
