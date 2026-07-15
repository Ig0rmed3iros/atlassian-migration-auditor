"""JSON API powering the analysis UI. Server-side pagination/filtering so a
40k-issue run never ships to the browser at once."""
from __future__ import annotations

import json
from collections import Counter, defaultdict

from fastapi import APIRouter, HTTPException, Request

from auditor.aggregate import (_FIELD_LIKE_KINDS, _is_empty, _issue_key,
                               _src_label, compute_run_fidelity,
                               derive_fidelity)
from auditor.connectors import get_connector
from auditor.envaudit.fixes import category_for

# Fidelity kinds always surfaced in a per-project kind_breakdown, so a project
# with zero of a kind still reports it (a stable shape for the UI).
_KIND_BREAKDOWN_KEYS = ("field_mismatch", "comment_mismatch", "link_mismatch",
                        "content_mismatch", "attachment_mismatch",
                        "cf_value_mismatch", "cf_field_not_in_target",
                        "macro_param_mismatch")


def _store(request: Request):
    return request.app.state.store


def _all_issue_findings(store, run_id: int) -> list[dict]:
    """Read every stored per-issue finding for a run, detail_json parsed.

    Uses the bulk store read (one statement) — the result feeds derive_fidelity
    (which is pure) and must see the whole set. The earlier page-by-200 loop
    issued one COUNT plus an OFFSET scan per page, which is O(n^2) and stalls
    the summary endpoint on real runs of tens of thousands of findings."""
    rows = store.all_issue_findings(run_id)
    for row in rows:
        row["detail"] = _detail(row)
    return rows


def _project_rows_from_stats(stats: dict) -> list[dict]:
    """Build derive_fidelity's project_rows from the stored project_stats block.
    Each carries `common`, `fidelity_pct` (raw) and `issues_with_mismatches`."""
    rows = []
    for key, ps in (stats.get("project_stats") or {}).items():
        rows.append({
            "key": key,
            "common": ps.get("common") or 0,
            "fidelity_pct": ps.get("fidelity_pct"),
            "issues_with_mismatches": ps.get("issues_with_mismatches") or 0,
        })
    return rows


def _run_or_404(store, run_id: int) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run


def _product_labels(store, run: dict) -> tuple[str, str, str]:
    """(product, container_label, item_label) for a run, via its migration.

    One round-trip serves the UI's whole relabeling pass (stats keys stay
    issue-named). A legacy row naming an unregistered product degrades to
    jira vocabulary instead of 500ing the analysis page — same posture as
    the elevation guard in main.py."""
    mig = store.get_migration(run["migration_id"]) or {}
    product = mig.get("product") or "jira"
    try:
        connector = get_connector(product)
    except ValueError:
        return product, "project", "issue"
    return product, connector.container_label, connector.item_label


def _detail(row):
    raw = row.pop("detail_json", None) or "{}"
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {"_unparseable": str(raw)[:200]}


def _project_findings(store, run_id: int, project: str) -> list[dict]:
    """Read every stored per-issue finding for ONE project, detail_json parsed.

    One scoped statement. Mirrors _all_issue_findings; a project with tens of
    thousands of findings is read in a single pass rather than an O(n^2) page
    loop that stalled the per-project drill-down endpoint."""
    rows = store.all_issue_findings(run_id, project=project)
    for row in rows:
        row["detail"] = _detail(row)
    return rows


def _field_breakdown(findings: list[dict], systematic_fields: set) -> list[dict]:
    """Per field-like field: distinct affected issues, target-empty pct, and
    whether it was flagged systematic. Sorted by affected issues desc."""
    field_issues: dict[str, set] = defaultdict(set)
    field_empty: dict[str, set] = defaultdict(set)
    for f in findings:
        if f.get("kind") not in _FIELD_LIKE_KINDS:
            continue
        field = f.get("field")
        if not field:
            continue
        ik = _issue_key(f)
        field_issues[field].add(ik)
        if _is_empty((f.get("detail") or {}).get("tgt")):
            field_empty[field].add(ik)
    rows = []
    for field, issues in field_issues.items():
        affected = len(issues)
        empty = len(field_empty[field])
        rows.append({
            "field": field,
            "issues": affected,
            "target_empty_pct": round(100.0 * empty / affected, 2)
            if affected else 0.0,
            "systematic": field in systematic_fields,
        })
    rows.sort(key=lambda r: (r["issues"], r["field"]), reverse=True)
    return rows


def _top_value_pairs(findings: list[dict], worst_field: str | None) -> list[dict]:
    """For the worst field, the most common (src -> tgt) value transitions.
    Empty values render as a readable token, not a blank."""
    if not worst_field:
        return []
    pairs: Counter = Counter()
    for f in findings:
        if f.get("kind") not in _FIELD_LIKE_KINDS:
            continue
        if f.get("field") != worst_field:
            continue
        detail = f.get("detail") or {}
        pairs[(_src_label(detail.get("src")),
               _src_label(detail.get("tgt")))] += 1
    return [{"field": worst_field, "src": s, "tgt": t, "count": c}
            for (s, t), c in pairs.most_common(20)]


def make_router() -> APIRouter:
    r = APIRouter()

    # Category and severity rank tables used for sorting env findings.
    # DataQuality (issue-level defects) ranks just below Structure: its flagship
    # done_but_unresolved is a high-severity, broadly-damaging data defect.
    _CATEGORY_RANK = {"Performance": 0, "Security": 1, "Structure": 2,
                      "DataQuality": 3, "Hygiene": 4, "Coverage": 5}
    _SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2, "info": 3, "warning": 4}

    @r.get("/api/runs/{run_id}/summary")
    def summary(run_id: int, request: Request):
        store = _store(request)
        run = _run_or_404(store, run_id)
        stats = json.loads(run["stats_json"] or "{}")

        # A2: env_audit runs return a focused response with a reconstructed
        # findings array. Migration runs use the existing path unchanged.
        if run.get("kind") == "env_audit":
            findings = []
            for area in store.config_areas(run_id):
                for row in store.query_config(run_id, area):
                    # row["detail"] is already parsed by query_config (json.loads)
                    detail = row["detail"]
                    kind = row["kind"]
                    findings.append({
                        "area": row["area"],
                        "name": row.get("name"),
                        "kind": kind,
                        "severity": detail.get("severity"),
                        "category": detail.get("category") or category_for(kind),
                        "fix": detail.get("fix"),
                        "admin_link": detail.get("admin_link"),
                    })
            # Sort: category rank → severity rank → name
            findings.sort(key=lambda f: (
                _CATEGORY_RANK.get(f.get("category"), 99),
                _SEVERITY_RANK.get(f.get("severity"), 99),
                (f.get("name") or ""),
            ))
            return {
                "run_id": run_id,
                "status": run["status"],
                "phase": run["phase"],
                "verdict": run["verdict"],
                "started_at": run["started_at"],
                "finished_at": run["finished_at"],
                "headlines": stats.pop("headlines", []),
                "stats": stats,
                "findings": findings,
            }

        # Display-time fidelity derivation over ALREADY-STORED findings: split
        # systematic gaps out of core fidelity. Uses SQL aggregates (a few
        # hundred rows) instead of loading all findings (potentially 400k+ rows)
        # so the summary page does not stall on large runs. The _store/_run_id
        # args allow core_mismatch_counts() to run a second targeted SQL pass
        # after systematic fields are identified.
        # Prefer the fidelity precomputed at finalize (O(1) read); only
        # re-aggregate live for runs finalized before precompute existed.
        derived = stats.pop("derived_fidelity", None)
        if derived is None:
            derived = compute_run_fidelity(store, run_id, stats)
        overall = derived["overall"]
        core_by_key = {p["key"]: p for p in derived["per_project"]}

        project_stats = stats.pop("project_stats", {})
        # Attach per-project core fidelity onto the existing project_stats block
        # without disturbing any existing field (back-compat).
        for key, ps in project_stats.items():
            cp = core_by_key.get(key)
            if cp is not None:
                ps["fidelity_core"] = cp["fidelity_core"]
                ps["fidelity_raw"] = cp["fidelity_raw"]
                ps["core_mismatched"] = cp["core_mismatched"]

        product, container_label, item_label = _product_labels(store, run)
        return {"run_id": run_id, "status": run["status"],
                "phase": run["phase"], "verdict": run["verdict"],
                "product": product,
                "container_label": container_label,
                "item_label": item_label,
                "started_at": run["started_at"],
                "finished_at": run["finished_at"],
                "headlines": stats.pop("headlines", []),
                "project_stats": project_stats,
                "areas": stats.pop("areas", {}),
                "systematic_gaps": derived["systematic_gaps"],
                "fidelity": {
                    "core": overall["fidelity_core"],
                    "raw": overall["fidelity_raw"],
                    "core_mismatched_total": overall["core_mismatched_total"],
                    "per_project": derived["per_project"],
                },
                "stats": stats}

    @r.get("/api/runs/{run_id}/projects")
    def projects(run_id: int, request: Request):
        store = _store(request)
        run = _run_or_404(store, run_id)
        rows = store.get_run_projects(run_id)
        # Attach per-project CORE fidelity (systematic gaps removed) so the
        # project-health table shows the same corrected number as the headline
        # and the drill-down — not the raw stored fidelity_pct.
        stats = json.loads(run["stats_json"] or "{}")
        derived = stats.get("derived_fidelity")
        if derived is None:
            derived = compute_run_fidelity(store, run_id, stats)
        core_by_key = {p["key"]: p for p in derived["per_project"]}
        for row in rows:
            cp = core_by_key.get(row.get("key"))
            if cp is not None:
                row["fidelity_core"] = cp["fidelity_core"]
                row["fidelity_raw"] = cp["fidelity_raw"]
        return rows

    @r.get("/api/runs/{run_id}/projects/{key}")
    def project_detail(run_id: int, key: str, request: Request):
        store = _store(request)
        run = _run_or_404(store, run_id)

        # The run_projects row carries presence/coverage stats (holes/tails,
        # blind_spot, status, name). The stats_json project_stats block carries
        # the compare-time `common`/`mismatched`/raw fidelity. 404 if neither
        # knows this project key.
        prow = next((p for p in store.get_run_projects(run_id)
                     if p["key"] == key), None)
        stats = json.loads(run["stats_json"] or "{}")
        ps = (stats.get("project_stats") or {}).get(key)
        if prow is None and ps is None:
            raise HTTPException(404, "project not found")
        prow = prow or {}
        ps = ps or {}

        # Scope the pure derivation to THIS project only: build a single
        # project_row and feed only this project's stored findings.
        common = ps.get("common")
        if common is None:
            common = prow.get("src_count")
        project_row = {
            "key": key,
            "common": common or 0,
            "fidelity_pct": ps.get("fidelity_pct", prow.get("fidelity_pct")),
            "issues_with_mismatches": ps.get("issues_with_mismatches") or 0,
        }
        findings = _project_findings(store, run_id, key)
        derived = derive_fidelity([project_row], findings)
        per = derived["per_project"][0] if derived["per_project"] else {}
        gaps = derived["systematic_gaps"]
        systematic_fields = {g["field"] for g in gaps}

        fb = _field_breakdown(findings, systematic_fields)
        # worst field = the field with the most affected issues (already first).
        worst = fb[0]["field"] if fb else None

        kb = {k: 0 for k in _KIND_BREAKDOWN_KEYS}
        for f in findings:
            k = f.get("kind")
            if k in kb:
                kb[k] += 1

        project = {
            "key": key,
            "name": prow.get("name"),
            "src": ps.get("src", prow.get("src_count")),
            "tgt": ps.get("tgt", prow.get("tgt_count")),
            "holes": ps.get("missing_in_tgt", prow.get("missing")),
            "tails": ps.get("tails", prow.get("tail_count")),
            "common": project_row["common"],
            "mismatched": per.get("mismatched", project_row[
                "issues_with_mismatches"]),
            "fidelity_core": per.get("fidelity_core"),
            "fidelity_raw": per.get("fidelity_raw", project_row["fidelity_pct"]),
            "blind_spot": bool(prow.get("blind_spot")),
            "status": prow.get("status"),
        }
        return {
            "project": project,
            "field_breakdown": fb,
            "kind_breakdown": kb,
            "top_value_pairs": _top_value_pairs(findings, worst),
            "systematic_gaps": gaps,
        }

    @r.get("/api/runs/{run_id}/issues")
    def issues(run_id: int, request: Request, project: str | None = None,
               kind: str | None = None, q: str | None = None,
               page: int = 1, size: int = 50):
        store = _store(request)
        _run_or_404(store, run_id)
        page = max(1, page)
        size = max(1, min(size, 200))
        rows, total = store.query_issues(run_id, project=project, kind=kind,
                                         q=q, page=page, size=size)
        for row in rows:
            row["detail"] = _detail(row)
        return {"rows": rows, "total": total, "page": page, "size": size}

    @r.get("/api/runs/{run_id}/issues/kinds")
    def kinds(run_id: int, request: Request, project: str | None = None):
        store = _store(request)
        _run_or_404(store, run_id)
        return store.issue_kind_counts(run_id, project=project)

    @r.get("/api/runs/{run_id}/config")
    def config(run_id: int, request: Request, area: str | None = None):
        store = _store(request)
        run = _run_or_404(store, run_id)
        if area is None:
            # findings-backed areas come from findings_config rows; SKIPPED
            # areas (R4: no API on a side) emit zero rows by design and live
            # only in stats_json — surface them explicitly so no client can
            # render an unaudited area as clean.
            stats = json.loads(run["stats_json"] or "{}")
            skipped = {
                name: (a.get("reason") or "skipped")
                for name, a in (stats.get("areas") or {}).items()
                if isinstance(a, dict) and a.get("skipped")}
            return {"areas": store.config_areas(run_id), "skipped": skipped}
        rows = store.query_config(run_id, area)
        for row in rows:
            row["detail"] = _detail(row)
        return {"rows": rows}

    @r.get("/api/runs/{run_id}/events")
    def events(run_id: int, request: Request, after: int = 0):
        store = _store(request)
        _run_or_404(store, run_id)
        return store.get_events(run_id, after_id=after)

    return r
