"""Environment-audit run summary: verdict ladder + stats + headlines (R6)."""
from __future__ import annotations
from collections import Counter

_PERFORMANCE_KINDS = frozenset({
    "field_sprawl", "large_option_set", "workflow_sprawl",
    "status_sprawl", "screen_sprawl", "permission_scheme_sprawl",
    "resolution_sprawl", "priority_sprawl", "issue_type_sprawl",
    "link_type_sprawl", "large_workflow",
    # All four Cloud guardrail-proximity kinds roll up under Performance
    # (kept in sync with fixes._CATEGORY_MAP).
    "near_field_limit", "near_issue_type_limit",
    "near_priority_limit", "near_workflow_limit",
    "dashboard_filter_volume_high",
    # Confluence performance kinds.
    "large_space", "space_count_near_guardrail",
})
_SECURITY_KINDS = frozenset({
    "permission_grant_overly_broad", "public_browse_grant",
    "large_group_admin_bloat", "anonymous_write_grant",
    "admin_grant_to_logged_in",
    "shared_object_owned_by_inactive", "public_shared_filter",
    "public_shared_dashboard",
    # Confluence security kinds (anonymous_write_grant shared with Jira above).
    "space_no_admin", "anonymous_space_access", "space_permission_to_anyone",
    "restricted_pages", "permission_grant_to_empty_group",
})


def deterministic_health(sev) -> tuple[int, str]:
    """A reproducible, rules-based health score (0-100) + letter grade derived
    purely from the deterministic finding severities — so the report's headline
    verdict has a deterministic basis, is identical across runs, and is present
    even when the AI assessment is off (the AI's own number stays advisory).

    Penalties are per-severity and capped so one category can't instantly floor
    the score; highs dominate, area_errors (warning) dent confidence."""
    high = sev.get("high", 0)
    medium = sev.get("medium", 0)
    low = sev.get("low", 0)
    warning = sev.get("warning", 0)        # area_error = an un-evaluable blind spot
    score = 100
    score -= min(75, 18 * high)
    score -= min(35, 5 * medium)
    score -= min(12, low)
    score -= min(20, 6 * warning)
    score = max(0, score)
    return score, _grade_for(score)


def _grade_for(score: int) -> str:
    return ("A" if score >= 90 else "B" if score >= 80 else
            "C" if score >= 70 else "D" if score >= 55 else "F")


# The verdict is the categorical go/no-go signal; cap the headline score/grade
# so they can never read BETTER than the verdict (a single high makes the
# verdict CRITICAL — the grade must not simultaneously show a reassuring B).
_VERDICT_SCORE_CEILING = {"CRITICAL": 55, "NEEDS_ATTENTION": 79}


def build_env_summary(findings: list, ai: dict) -> dict:
    findings = findings or []
    sev = Counter(f.get("severity") for f in findings)
    kinds = Counter(f.get("kind") for f in findings)

    # Only use AI grade when the AI assessment was actually computed.
    ai_skipped = bool((ai or {}).get("skipped"))
    grade = None if ai_skipped else (ai or {}).get("grade")

    if sev.get("high"):
        verdict = "CRITICAL"
    elif sev.get("medium") or sev.get("warning") or grade in ("C", "D", "F"):
        # warning severity = area_error (unexpected fetch failure) — treat as
        # NEEDS_ATTENTION so unexpected Cloud-environment failures are not silent.
        verdict = "NEEDS_ATTENTION"
    elif sev.get("low"):
        verdict = "HEALTHY_WITH_NOTES"
    else:
        verdict = "HEALTHY"

    headlines = []
    if (ai or {}).get("summary") and not (ai or {}).get("skipped"):
        headlines.append(ai["summary"])
    if sev.get("high"):
        headlines.append(f"{sev['high']} high-severity configuration issue(s) "
                         f"need attention.")
    if sev.get("medium"):
        headlines.append(f"{sev['medium']} medium-severity issue(s) require review.")
    area_errors = kinds.get("area_error", 0)
    if area_errors:
        headlines.append(f"{area_errors} area(s) could not be evaluated due to "
                         f"unexpected fetch errors — results may be incomplete.")
    caps = kinds.get("capability_gap", 0)
    if caps:
        headlines.append(f"{caps} area(s) could not be evaluated (no Data Center "
                         f"API) — coverage is partial.")
    # B: performance-risk headlines (field/workflow/screen sprawl, large option sets)
    perf_count = sum(kinds.get(k, 0) for k in _PERFORMANCE_KINDS)
    if perf_count:
        headlines.append(
            f"{perf_count} performance-risk finding(s) detected "
            f"(field/workflow/screen sprawl or large option sets).")
    # B: security headlines (overly broad permission grants)
    sec_count = sum(kinds.get(k, 0) for k in _SECURITY_KINDS)
    if sec_count:
        headlines.append(
            f"{sec_count} security finding(s) detected "
            f"(overly broad permission grant).")
    if not headlines:
        headlines.append("No configuration issues detected.")
    # The HEADLINE score/grade are deterministic (reproducible, always present);
    # the AI's own number is kept separately as advisory. Floor the score by the
    # verdict tier so the grade can never contradict a CRITICAL/NEEDS_ATTENTION
    # verdict (review P3).
    det_score, det_grade = deterministic_health(sev)
    ceiling = _VERDICT_SCORE_CEILING.get(verdict)
    if ceiling is not None and det_score > ceiling:
        det_score = ceiling
        det_grade = _grade_for(det_score)
    ai_health_score = None if ai_skipped else (ai or {}).get("health_score")
    return {"verdict": verdict, "headlines": headlines,
            "stats": {"findings": len(findings), "high": sev.get("high", 0),
                      "medium": sev.get("medium", 0), "low": sev.get("low", 0),
                      "capability_gaps": caps,
                      "by_kind": dict(kinds),
                      "health_score": det_score, "grade": det_grade,
                      "ai_health_score": ai_health_score, "ai_grade": grade,
                      "ai_skipped": ai_skipped}}
