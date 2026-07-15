import pytest

from auditor.envaudit.report import build_env_summary


def test_high_severity_is_critical():
    findings = [{"area": "workflows", "name": "WF", "kind": "workflow_no_transitions",
                 "severity": "high"}]
    out = build_env_summary(findings, {"skipped": True, "health_score": None})
    assert out["verdict"] == "CRITICAL"


def test_only_advisories_is_healthy_with_notes():
    findings = [{"area": "custom_fields", "name": "X", "kind": "unused_custom_field",
                 "severity": "low"}]
    out = build_env_summary(findings, {"skipped": False, "grade": "A", "health_score": 95})
    assert out["verdict"] == "HEALTHY_WITH_NOTES"
    # The headline score is now DETERMINISTIC (one low finding -> 99); the AI's
    # number is preserved separately as advisory (no-bias review / enterprise).
    assert out["stats"]["health_score"] == 99
    assert out["stats"]["ai_health_score"] == 95


def test_deterministic_health_present_and_reproducible_without_ai():
    # Critical/product: the headline score must have a deterministic basis —
    # present even when AI is OFF and identical across runs.
    findings = [{"severity": "high"}, {"severity": "medium"}, {"severity": "low"}]
    a = build_env_summary(findings, {"skipped": True})
    b = build_env_summary(findings, {"skipped": True})
    hs = a["stats"]["health_score"]
    assert hs is not None and 0 <= hs <= 100
    assert a["stats"]["grade"] in ("A", "B", "C", "D", "F")
    assert hs == b["stats"]["health_score"]              # reproducible
    clean = build_env_summary([], {"skipped": True})
    assert clean["stats"]["health_score"] == 100 and clean["stats"]["grade"] == "A"
    assert clean["stats"]["health_score"] > hs          # monotonic in severity


def test_score_and_grade_cannot_contradict_critical_verdict():
    # No-bias review P3: a single high made verdict=CRITICAL but grade B / score
    # 82 — shown side-by-side, misleading the go/no-go call. The headline score/
    # grade must be floored to agree with the verdict tier.
    out = build_env_summary([{"severity": "high"}], {"skipped": True})
    assert out["verdict"] == "CRITICAL"
    assert out["stats"]["health_score"] <= 55
    assert out["stats"]["grade"] in ("D", "F")


def test_score_floored_for_needs_attention():
    out = build_env_summary([{"severity": "medium"}], {"skipped": True})
    assert out["verdict"] == "NEEDS_ATTENTION"
    assert out["stats"]["health_score"] <= 79      # cannot read as an A/B
    assert out["stats"]["grade"] in ("C", "D", "F")


def test_deterministic_score_monotonic_more_highs_lower():
    one = build_env_summary([{"severity": "high"}], {"skipped": True})
    three = build_env_summary([{"severity": "high"}] * 3, {"skipped": True})
    assert three["stats"]["health_score"] < one["stats"]["health_score"]


def test_clean_is_healthy():
    out = build_env_summary([], {"skipped": True, "health_score": None})
    assert out["verdict"] == "HEALTHY"


# --- Issue 1: area_error (severity='warning') must raise verdict to NEEDS_ATTENTION
# and emit a dedicated headline, not silently produce HEALTHY.

def test_area_error_verdict_needs_attention():
    """An unexpected fetch failure (area_error/warning) must not produce HEALTHY."""
    findings = [{"area": "workflows", "name": "workflows",
                 "kind": "area_error", "severity": "warning"}]
    out = build_env_summary(findings, {"skipped": True, "health_score": None})
    assert out["verdict"] == "NEEDS_ATTENTION", (
        "area_error findings must escalate verdict to NEEDS_ATTENTION, not leave it HEALTHY"
    )


def test_area_error_has_headline():
    """An area_error finding must produce a visible headline, not 'No configuration issues detected.'"""
    findings = [{"area": "workflows", "name": "workflows",
                 "kind": "area_error", "severity": "warning"}]
    out = build_env_summary(findings, {"skipped": True, "health_score": None})
    # Must not fall through to the false-clean default headline
    assert out["headlines"] != ["No configuration issues detected."], (
        "area_error should not produce the clean-bill headline"
    )
    # At least one headline should mention the error
    assert any("area" in h.lower() or "fetch" in h.lower() or "error" in h.lower()
               or "evaluat" in h.lower() or "unavailable" in h.lower()
               for h in out["headlines"]), (
        f"Expected an area-error headline, got: {out['headlines']}"
    )


# --- Issue 2: medium-only findings must produce a prose headline.

def test_medium_only_needs_attention_has_headline():
    """NEEDS_ATTENTION driven by medium findings must not emit 'No configuration issues detected.'"""
    findings = [{"area": "statuses", "name": "Backlog",
                 "kind": "status_not_in_workflow", "severity": "medium"}]
    out = build_env_summary(findings, {"skipped": True, "health_score": None})
    assert out["verdict"] == "NEEDS_ATTENTION"
    assert out["headlines"] != ["No configuration issues detected."], (
        "NEEDS_ATTENTION driven by medium severity should not report a clean bill"
    )
    assert any("medium" in h.lower() or "issue" in h.lower() or "1" in h
               for h in out["headlines"]), (
        f"Expected a medium-finding headline, got: {out['headlines']}"
    )


# --- Issue 3: AI grade must be ignored when ai['skipped'] is True.

def test_skipped_ai_grade_not_used_in_verdict():
    """A stale/partially-initialised ai dict with skipped=True and grade='D' must not
    push the verdict to NEEDS_ATTENTION — the grade was never computed."""
    findings = []  # no findings at all
    out = build_env_summary(findings, {"skipped": True, "grade": "D",
                                       "health_score": None})
    assert out["verdict"] == "HEALTHY", (
        "grade from a skipped AI result must not influence the verdict"
    )


# --- Issue 4: findings=None must not raise TypeError.

def test_none_findings_does_not_raise():
    """build_env_summary(None, ...) must not raise TypeError."""
    out = build_env_summary(None, {"skipped": True, "health_score": None})
    assert out["verdict"] == "HEALTHY"
    assert out["stats"]["findings"] == 0


# --- Bonus: ai summary with skipped=False must appear in headlines.

def test_ai_summary_skipped_false_appears_in_headlines():
    """When ai['skipped'] is False and a summary is present, it must be a headline."""
    findings = []
    ai = {"skipped": False, "grade": "B", "health_score": 80,
          "summary": "Configuration looks generally healthy."}
    out = build_env_summary(findings, ai)
    assert "Configuration looks generally healthy." in out["headlines"]


def test_grade_c_escalates_to_needs_attention():
    """Spec: NEEDS_ATTENTION for AI grade <= C. An env graded C with no
    deterministic findings must not fall through to a false-clean HEALTHY."""
    out = build_env_summary([], {"skipped": False, "grade": "C", "health_score": 65})
    assert out["verdict"] == "NEEDS_ATTENTION"


def test_performance_findings_headline():
    """Performance-category findings (e.g. field_sprawl) produce a dedicated headline."""
    findings = [
        {"area": "custom_fields", "name": "X", "kind": "field_sprawl", "severity": "medium"},
        {"area": "custom_fields", "name": "Y", "kind": "large_option_set", "severity": "low"},
    ]
    out = build_env_summary(findings, {"skipped": True})
    headlines_text = " ".join(out["headlines"]).lower()
    assert "performance" in headlines_text, (
        f"Expected a performance headline, got: {out['headlines']}"
    )


def test_security_findings_headline():
    """Security-category findings (permission_grant_overly_broad) produce a security headline."""
    findings = [
        {"area": "permission_scheme_grants", "name": "Dev Scheme",
         "kind": "permission_grant_overly_broad", "severity": "medium"},
    ]
    out = build_env_summary(findings, {"skipped": True})
    headlines_text = " ".join(out["headlines"]).lower()
    assert "security" in headlines_text, (
        f"Expected a security headline, got: {out['headlines']}"
    )


def test_mixed_perf_security_both_in_headlines():
    """Both performance and security findings produce both dedicated headlines."""
    findings = [
        {"area": "custom_fields", "name": "X", "kind": "field_sprawl", "severity": "medium"},
        {"area": "permission_scheme_grants", "name": "S",
         "kind": "permission_grant_overly_broad", "severity": "medium"},
    ]
    out = build_env_summary(findings, {"skipped": True})
    headlines_text = " ".join(out["headlines"]).lower()
    assert "performance" in headlines_text, f"Missing performance headline: {out['headlines']}"
    assert "security" in headlines_text, f"Missing security headline: {out['headlines']}"


# --- Section-1 new kinds must count into the headline rollups. ---------------

def test_near_field_limit_counts_as_performance():
    """The guardrail-aligned near_field_limit kind rolls up under Performance."""
    findings = [
        {"area": "custom_fields", "name": "custom_fields",
         "kind": "near_field_limit", "severity": "medium"},
    ]
    out = build_env_summary(findings, {"skipped": True})
    headlines_text = " ".join(out["headlines"]).lower()
    assert "performance" in headlines_text, (
        f"near_field_limit should produce a performance headline, got: {out['headlines']}"
    )


@pytest.mark.parametrize("kind", [
    "near_issue_type_limit", "near_priority_limit", "near_workflow_limit",
])
def test_all_guardrail_kinds_count_as_performance(kind):
    """Every near_* guardrail kind must roll up under Performance (the report
    perf headline previously listed only near_field_limit, under-counting)."""
    findings = [{"area": "x", "name": "x", "kind": kind, "severity": "medium"}]
    out = build_env_summary(findings, {"skipped": True})
    assert "performance" in " ".join(out["headlines"]).lower(), (
        f"{kind} should produce a performance headline, got: {out['headlines']}")


def test_anonymous_write_grant_counts_as_security():
    """anonymous_write_grant rolls up under the Security headline."""
    findings = [
        {"area": "permission_scheme_grants", "name": "Default",
         "kind": "anonymous_write_grant", "severity": "medium"},
    ]
    out = build_env_summary(findings, {"skipped": True})
    headlines_text = " ".join(out["headlines"]).lower()
    assert "security" in headlines_text, (
        f"anonymous_write_grant should produce a security headline, got: {out['headlines']}"
    )
