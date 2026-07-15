"""Human-friendly executive-summary report for an environment audit.

Assembles the run's verdict + grade + KPIs + AI executive summary + prioritized
remediation roadmap + findings-by-problem-type + "what the rules can't check"
gaps into a print-styled HTML document, and renders it to PDF via WeasyPrint
(lazy-imported, so the app boots and the rest of the report works without it).

LOCAL-ONLY: the report is built on the operator's machine from already-stored
audit data and downloaded by them. Nothing is transmitted externally — this is
the operator's own config report for their stakeholders, not an AI payload, so
it is exempt from the metadata-only outbound boundary that governs analysis.py.
"""
from __future__ import annotations

import json
import re
import time

from auditor.envaudit.fixes import _FIXES, category_for


class ReportUnavailable(RuntimeError):
    """Raised when the PDF engine (WeasyPrint) is not installed."""


_SEV_RANK = {"high": 0, "medium": 1, "low": 2}
_SEV_NAME = {0: "high", 1: "medium", 2: "low", 3: "info"}
_TIER_LABEL = {"app": "App-fixable", "human": "Manual review",
               "unfixable": "Re-migration"}


def _fmt_date(ts):
    if not ts:
        return ""
    try:
        return time.strftime("%B %-d, %Y", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        try:
            return time.strftime("%B %d, %Y", time.localtime(float(ts)))
        except (TypeError, ValueError, OSError):
            return ""


def _first_sentences(text, n=2, cap=300):
    """The opening 1-2 sentences of the AI narrative — the cover's story hook."""
    text = (text or "").strip()
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = " ".join(parts[:n]).strip()
    return (out[:cap].rstrip() + "…") if len(out) > cap else out


def _group_findings(findings):
    """Roll findings up BY PROBLEM TYPE (kind): one row per kind with its count,
    worst severity, fix tier and label — sorted worst-severity-first then count."""
    groups: dict = {}
    for f in findings:
        kind = f.get("kind") or ""
        fix = f.get("fix") or _FIXES.get(kind) or {}
        g = groups.get(kind)
        if g is None:
            g = {"kind": kind, "count": 0, "_sev": 3,
                 "label": fix.get("label") or fix.get("title") or kind,
                 "tier": fix.get("tier") or "human",
                 "category": f.get("category") or category_for(kind)}
            groups[kind] = g
        g["count"] += 1
        g["_sev"] = min(g["_sev"], _SEV_RANK.get(f.get("severity"), 3))
    out = sorted(groups.values(), key=lambda g: (g["_sev"], -g["count"]))
    for g in out:
        g["severity"] = _SEV_NAME[g["_sev"]]
        g["tier_label"] = _TIER_LABEL.get(g["tier"], "Manual review")
    return out


def build_report_context(store, run_id):
    """Assemble the executive-summary context for an env_audit run, or None when
    the run is missing or is not an environment audit."""
    run = store.get_run(run_id)
    if run is None or run.get("kind") != "env_audit":
        return None
    mig = store.get_migration(run["migration_id"]) or {}
    stats = json.loads(run.get("stats_json") or "{}")
    ai = stats.get("ai") if isinstance(stats.get("ai"), dict) else {}

    findings = []
    for area in store.config_areas(run_id):
        for row in store.query_config(run_id, area):
            detail = row.get("detail") or {}
            findings.append({"area": row.get("area"), "name": row.get("name"),
                             "kind": row.get("kind"),
                             "severity": detail.get("severity"),
                             "category": detail.get("category"),
                             "fix": detail.get("fix")})
    groups = _group_findings(findings)
    app_fixable = sum(g["count"] for g in groups if g["tier"] == "app")

    health = stats.get("health_score")
    if health is None:
        health = ai.get("health_score")
    total = stats.get("findings")
    if total is None:
        total = len(findings)

    # The cover's story hook: the lead of the AI narrative, else the first
    # headline, else a plain factual line. When AI was SKIPPED, ai.summary is the
    # "AI analysis skipped…" notice — not a story hook — so fall through to the
    # factual line instead of printing the skip notice on the cover.
    bottom_line = "" if ai.get("skipped") else _first_sentences(ai.get("summary"))
    if not bottom_line:
        headlines = stats.get("headlines") or []
        bottom_line = headlines[0] if headlines else (
            f"{total} configuration finding(s) across {len(groups)} problem type(s).")

    return {
        "run_id": run_id,
        "env_name": mig.get("name") or "Environment",
        "product": (mig.get("product") or "jira").title(),
        "generated_at": _fmt_date(run.get("finished_at")),
        "verdict": run.get("verdict") or "—",
        "grade": stats.get("grade") or ai.get("grade") or "—",
        "health_score": health,
        # The AI's INDEPENDENT read (advisory) — the headline grade/score above
        # are deterministic. Surfaced only when it diverges, as a sanity cross-
        # check (was computed + shipped in the API but never displayed).
        "ai_health_score": stats.get("ai_health_score"),
        "ai_grade": stats.get("ai_grade"),
        "bottom_line": bottom_line,
        "total_findings": total,
        "high": int(stats.get("high") or 0),
        "medium": int(stats.get("medium") or 0),
        "low": int(stats.get("low") or 0),
        "app_fixable": app_fixable,
        "problem_types": len(groups),
        "finding_groups": groups,
        "headlines": stats.get("headlines") or [],
        "ai": {
            "skipped": bool(ai.get("skipped")),
            "summary": ai.get("summary") or "",
            "roadmap": ai.get("roadmap") or [],
            "top_risks": ai.get("top_risks") or [],
            "quick_wins": ai.get("quick_wins") or [],
            "gaps": ai.get("gaps") or [],
            "themes": ai.get("themes") or [],
            "ai_findings": ai.get("ai_findings") or [],
            "model": ai.get("model"),
        },
    }


def render_report_html(templates, context, template="report.html") -> str:
    """Render a standalone report template to an HTML string (no Request — the
    templates are self-contained and never reference url_for/request)."""
    return templates.env.get_template(template).render(context)


def _blocking_url_fetcher(url, *args, **kwargs):
    """Refuse ALL external/file resource fetches during PDF rendering.

    Defense-in-depth: WeasyPrint's default fetcher resolves http(s)://, file://,
    ftp:// and data: URLs, so an unescaped attacker value reaching a `url()` /
    `src` / `@import` context would become local-file read (file:///etc/passwd)
    or SSRF (http://169.254.169.254/...). The report template is fully
    self-contained (inline CSS, no images/fonts/remote assets), so a hard refuse
    costs nothing and closes the class permanently — independent of whether some
    future edit drops a `|safe` or builds report HTML by string concat."""
    raise ValueError(f"external resource blocked in report PDF: {url!r}")


def render_report_pdf(templates, context, template="report.html") -> bytes:
    html = render_report_html(templates, context, template)
    try:
        from weasyprint import HTML
    except (ImportError, OSError) as exc:  # ImportError: not installed.
        # OSError: a required system lib (pango/cairo/gobject) is missing — fails
        # at import; degrade gracefully instead of a 500.
        raise ReportUnavailable(
            "PDF export requires WeasyPrint and its system libraries "
            "(pango/cairo). Install with: pip install weasyprint") from exc
    return HTML(string=html, url_fetcher=_blocking_url_fetcher).write_pdf()


def report_filename(context) -> str:
    base = "".join(c if (c.isalnum() or c in "-_") else "-"
                   for c in (context.get("env_name") or "environment")).strip("-")
    return f"{base or 'environment'}-audit-summary.pdf"


# ===========================================================================
# Migration-audit report (source -> target fidelity, per-project, gaps)
# ===========================================================================

def _fidelity_class(fid):
    if not isinstance(fid, (int, float)):
        return "g-"
    if fid >= 95:
        return "g-A"
    if fid >= 80:
        return "g-C"
    return "g-D"


def build_migration_report_context(store, run_id):
    """Executive-summary context for a MIGRATION audit (kind='audit'), or None
    when the run is missing / not a migration audit. Reuses the same fidelity
    derivation the analysis page shows (core fidelity = systematic gaps removed)."""
    from auditor.aggregate import derive_fidelity

    run = store.get_run(run_id)
    if run is None or run.get("kind") != "audit":
        return None
    mig = store.get_migration(run["migration_id"]) or {}
    stats = json.loads(run.get("stats_json") or "{}")

    issue_findings = store.all_issue_findings(run_id)
    for r in issue_findings:
        try:
            r["detail"] = json.loads(r.get("detail_json") or "{}")
        except (TypeError, ValueError):
            r["detail"] = {}
    project_rows = [{"key": k, "common": ps.get("common") or 0,
                     "fidelity_pct": ps.get("fidelity_pct"),
                     "issues_with_mismatches": ps.get("issues_with_mismatches") or 0}
                    for k, ps in (stats.get("project_stats") or {}).items()]
    derived = derive_fidelity(project_rows, issue_findings,
                              audited=stats.get("issues_src_total"))
    overall = derived.get("overall") or {}
    fid = overall.get("fidelity_core")
    core_by_key = {p["key"]: p for p in (derived.get("per_project") or [])}

    projects = store.get_run_projects(run_id)
    proj_rows = []
    for p in projects:
        cp = core_by_key.get(p.get("key")) or {}
        fpct = cp.get("fidelity_core")
        if fpct is None:
            fpct = p.get("fidelity_pct")
        proj_rows.append({
            "key": p.get("key"), "name": p.get("name") or p.get("key"),
            "src": p.get("src_count"), "tgt": p.get("tgt_count"),
            "missing": p.get("missing"),
            "fidelity": round(fpct, 1) if isinstance(fpct, (int, float)) else None,
            "status": p.get("status")})
    # worst fidelity first — the projects that need attention lead.
    proj_rows.sort(key=lambda r: (r["fidelity"] if r["fidelity"] is not None
                                  else 999))

    # Cross-dialect surfacing: a DC->Cloud (wiki->ADF) comparison flags content/
    # comment mismatches as representation-sensitive. Surface it so an admin
    # reads expected representation drift as such, not as data loss.
    cross_dialect = any(r.get("detail", {}).get("cross_dialect")
                        for r in issue_findings)

    by_kind: dict = {}
    for r in issue_findings:
        k = r.get("kind") or "unknown"
        by_kind[k] = by_kind.get(k, 0) + 1
    findings_by_kind = sorted(({"kind": k, "count": c}
                               for k, c in by_kind.items()),
                              key=lambda x: -x["count"])

    fid_round = round(fid, 1) if isinstance(fid, (int, float)) else None
    missing_total = sum((p.get("missing") or 0) for p in projects)
    headlines = stats.get("headlines") or []
    bottom_line = headlines[0] if headlines else (
        f"{fid_round}% core fidelity across {len(projects)} "
        f"{'project' if len(projects) == 1 else 'projects'}; "
        f"{missing_total} item(s) missing on the target."
        if fid_round is not None else
        f"{len(issue_findings)} finding(s) across {len(projects)} project(s).")

    return {
        "env_name": mig.get("name") or "Migration",
        "product": (mig.get("product") or "jira").title(),
        "generated_at": _fmt_date(run.get("finished_at")),
        "verdict": run.get("verdict") or "—",
        "fidelity": fid_round,
        "cross_dialect": cross_dialect,
        # Content that exceeded the inline cap could not be verified, so the
        # fidelity % covers checkable content only — surface that next to it.
        "uncheckable_total": (int(stats.get("comments_uncheckable") or 0)
                              + int(stats.get("attachments_uncheckable") or 0)),
        "grade_class": _fidelity_class(fid),
        "bottom_line": bottom_line,
        "total_findings": len(issue_findings),
        "projects_count": len(projects),
        "missing_total": missing_total,
        "core_mismatched_total": overall.get("core_mismatched_total") or 0,
        "systematic_gaps": (derived.get("systematic_gaps") or [])[:10],
        "project_rows": proj_rows[:30],
        "project_overflow": max(len(proj_rows) - 30, 0),
        "findings_by_kind": findings_by_kind[:20],
        "headlines": headlines,
    }


def report_for_run(store, run_id):
    """Dispatch a run to the right report (template, context), or None. env_audit
    -> the AI executive summary; a migration audit -> the fidelity report."""
    run = store.get_run(run_id)
    if run is None:
        return None
    if run.get("kind") == "env_audit":
        ctx = build_report_context(store, run_id)
        return ("report.html", ctx) if ctx else None
    if run.get("kind") == "audit":
        ctx = build_migration_report_context(store, run_id)
        return ("report_migration.html", ctx) if ctx else None
    return None
