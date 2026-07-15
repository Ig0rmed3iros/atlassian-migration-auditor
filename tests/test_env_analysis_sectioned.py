"""Map-reduce env-audit AI analysis (analyze_sectioned).

Instead of one holistic call, run a focused analysis per CONFIG AREA-GROUP in
PARALLEL (the "map"), then a synthesis pass that re-correlates across areas (the
"reduce") to produce the overall grade/score + cross-area findings. Trades cost
+ latency for depth — the per-area passes surface more, synthesis adds the
cross-area issues a single diluted prompt tends to miss.

No real CLI/network: a routing fake provider returns canned per-section and
synthesis JSON and records concurrency.
"""
from __future__ import annotations

import json
import threading
import time

from auditor.envaudit.analysis import (
    analyze_sectioned, _split_sections, summarize_for_ai,
)


# ---------------------------------------------------------------------------
# Routing fake provider
# ---------------------------------------------------------------------------

class _Router:
    """complete() routes on the user content: a 'Section: <label>' call returns
    that section's canned area_findings; anything else is the synthesis call."""

    def __init__(self, *, section_findings=None, synth=None, model="claude-test",
                 fail_sections=(), synth_error=None, delay=0.02):
        self.section_findings = section_findings or {}
        self.synth = synth if synth is not None else {
            "health_score": 72, "grade": "C", "summary": "ok",
            "themes": ["t"], "top_risks": ["r"], "quick_wins": ["w"],
            "ai_findings": [{"title": "Cross-area X", "area": "multiple",
                             "severity": "high", "observation": "o",
                             "recommendation": "rec"}]}
        self.model = model
        self.fail_sections = set(fail_sections)
        self.synth_error = synth_error
        self.delay = delay
        self.calls = []
        self._lock = threading.Lock()
        self._cur = 0
        self.max_concurrent = 0

    def complete(self, system, user_content, *, model=None, effort="medium"):
        with self._lock:
            self._cur += 1
            self.max_concurrent = max(self.max_concurrent, self._cur)
            self.calls.append(user_content)
        try:
            time.sleep(self.delay)
            if user_content.startswith("Section:"):
                label = user_content.split("Section:", 1)[1].splitlines()[0].strip()
                if label in self.fail_sections:
                    return {"text": None, "error": "section boom",
                            "refused": False, "model": self.model}
                af = self.section_findings.get(label, [])
                return {"text": json.dumps({"area_findings": af}),
                        "error": None, "refused": False, "model": self.model}
            # synthesis call
            if self.synth_error:
                return {"text": None, "error": self.synth_error,
                        "refused": False, "model": self.model}
            return {"text": json.dumps(self.synth), "error": None,
                    "refused": False, "model": self.model}
        finally:
            with self._lock:
                self._cur -= 1


def _snap():
    def area(n):
        return {"count": n, "names": [f"obj{i}" for i in range(min(n, 3))]}
    return {"deployment": "cloud", "projects": {"count": 5},
            "areas": {
                "workflows": area(8), "statuses": area(40),
                "custom_fields": area(120), "screens": area(60),
                "projects": area(5), "components": area(700),
                "permission_scheme_grants": area(30), "groups": area(20),
                "issue_quality": area(0)}}


def _findings():
    def f(kind, area, sev="low"):
        return {"kind": kind, "area": area, "name": kind + "-x", "severity": sev,
                "detail": {"fix": {"tier": "human"}}}
    return [f("scheme_unused", "workflows"), f("unused_custom_field", "custom_fields"),
            f("component_no_lead", "components", "medium"),
            f("permission_grant_overly_broad", "permission_scheme_grants", "high")]


def _labels_of(sections):
    return [s["label"] for s in sections]


# ---------------------------------------------------------------------------
# _split_sections — every area lands in exactly one section
# ---------------------------------------------------------------------------

def test_split_sections_assigns_every_area_exactly_once():
    summary = summarize_for_ai(_snap(), _findings(), product="jira")
    sections = _split_sections(summary, _findings(), "jira")
    seen = []
    for s in sections:
        seen += list(s["payload"]["areas"].keys())
    assert sorted(seen) == sorted(summary["areas"].keys())
    assert len(seen) == len(set(seen)), "an area was placed in two sections"


def test_split_sections_payload_is_only_allowlisted_metadata():
    summary = summarize_for_ai(_snap(), _findings(), product="jira")
    sections = _split_sections(summary, _findings(), "jira")
    # each section's area entries must be the SAME objects summarize_for_ai built
    for s in sections:
        for an, entry in s["payload"]["areas"].items():
            assert entry == summary["areas"][an]


def test_split_sections_carries_full_per_kind_rule_findings():
    """Each section gets the COMPLETE per-kind rollup of its areas' findings —
    true counts + examples — not a slice of a global cap."""
    summary = summarize_for_ai(_snap(), _findings(), product="jira")
    sections = _split_sections(summary, _findings(), "jira")
    kinds = {rf["kind"]: rf for s in sections for rf in s["payload"]["rule_findings"]}
    assert "component_no_lead" in kinds and kinds["component_no_lead"]["count"] == 1
    assert "permission_grant_overly_broad" in kinds


# ---------------------------------------------------------------------------
# analyze_sectioned — orchestration
# ---------------------------------------------------------------------------

def test_calls_each_section_then_one_synthesis():
    summary = summarize_for_ai(_snap(), _findings(), product="jira")
    n_sections = len(_split_sections(summary, _findings(), "jira"))
    prov = _Router()
    analyze_sectioned(_snap(), _findings(), prov, product="jira")
    section_calls = [c for c in prov.calls if c.startswith("Section:")]
    synth_calls = [c for c in prov.calls if not c.startswith("Section:")]
    assert len(section_calls) == n_sections
    assert len(synth_calls) == 1


def test_overall_grade_and_score_come_from_synthesis():
    prov = _Router(synth={"health_score": 88, "grade": "B", "summary": "s",
                          "themes": [], "top_risks": [], "quick_wins": [],
                          "ai_findings": []})
    out = analyze_sectioned(_snap(), _findings(), prov, product="jira")
    assert out["health_score"] == 88 and out["grade"] == "B"
    assert out["skipped"] is False and out["error"] is None


def test_section_findings_and_cross_area_findings_are_merged():
    sf = {  # keyed by section label
        "Workflows, statuses & schemes": [
            {"title": "Status sprawl", "area": "statuses", "severity": "medium",
             "observation": "o", "recommendation": "r"}],
    }
    prov = _Router(section_findings=sf)  # synth default adds "Cross-area X"
    out = analyze_sectioned(_snap(), _findings(), prov, product="jira")
    titles = {f["title"] for f in out["ai_findings"]}
    assert "Status sprawl" in titles          # from the per-area map pass
    assert "Cross-area X" in titles           # from the synthesis reduce pass


def test_duplicate_findings_are_deduped():
    dup = {"title": "Same", "area": "statuses", "severity": "low",
           "observation": "o", "recommendation": "r"}
    prov = _Router(section_findings={"Workflows, statuses & schemes": [dup]},
                   synth={"health_score": 70, "grade": "C", "summary": "s",
                          "themes": [], "top_risks": [], "quick_wins": [],
                          "ai_findings": [dict(dup)]})
    out = analyze_sectioned(_snap(), _findings(), prov, product="jira")
    same = [f for f in out["ai_findings"]
            if f["title"] == "Same" and f["area"] == "statuses"]
    assert len(same) == 1


def test_a_failing_section_is_isolated_and_synthesis_still_runs():
    prov = _Router(fail_sections={"Security & access"})
    out = analyze_sectioned(_snap(), _findings(), prov, product="jira")
    # still produces an overall result (synthesis ran), not a crash
    assert out["skipped"] is False
    assert out["grade"] is not None
    assert "Cross-area X" in {f["title"] for f in out["ai_findings"]}


def test_all_sections_failed_degrades_no_false_grade():
    """If EVERY per-area pass failed, the synthesis must NOT manufacture a
    confident grade out of nothing — the result degrades (null grade + error)."""
    class _AllSectionsFail:
        def complete(self, system, user_content, *, model=None, effort="medium"):
            if user_content.startswith("Section:"):
                return {"text": None, "error": "section endpoint down",
                        "refused": False, "model": "m"}
            # synthesis would otherwise "succeed" with an invented grade
            return {"text": '{"grade": "A", "health_score": 95, '
                            '"summary": "all good"}',
                    "error": None, "refused": False, "model": "m"}

    out = analyze_sectioned(_snap(), _findings(), _AllSectionsFail(),
                            product="jira")
    assert out["grade"] is None and out["health_score"] is None
    assert out["skipped"] is False
    assert out["error"] and "per-area" in out["error"]
    assert out["ai_findings"] == []


def test_synthesis_failure_keeps_section_findings_degraded():
    prov = _Router(section_findings={
        "Fields & screens": [{"title": "Field sprawl", "area": "custom_fields",
                              "severity": "medium", "observation": "o",
                              "recommendation": "r"}]},
        synth_error="synthesis endpoint down")
    out = analyze_sectioned(_snap(), _findings(), prov, product="jira")
    assert out["error"] == "synthesis endpoint down"
    assert out["health_score"] is None and out["grade"] is None
    # depth survives: the per-area findings are still returned
    assert "Field sprawl" in {f["title"] for f in out["ai_findings"]}


def test_provider_none_skips_cleanly():
    out = analyze_sectioned(_snap(), _findings(), None, product="jira")
    assert out["skipped"] is True and out["error"] is None
    assert out["ai_findings"] == [] and out["health_score"] is None


def test_sections_run_concurrently():
    prov = _Router(delay=0.05)
    analyze_sectioned(_snap(), _findings(), prov, product="jira", workers=4)
    assert prov.max_concurrent >= 2, "section passes did not overlap"


def test_rich_finding_fields_survive_into_output():
    """root_cause / risk / priority / remediation_steps from a section pass must
    reach the final ai_findings, not be flattened to a one-liner."""
    rich = {"title": "Migration debris", "area": "custom_fields",
            "severity": "high", "priority": 1, "affected_count": 105,
            "root_cause": "an unfinished import left orphaned fields",
            "risk": "field picker clutter slows every issue edit",
            "remediation_steps": ["export the field list", "delete the orphans"],
            "effort": "M"}
    prov = _Router(section_findings={"Fields & screens": [rich]},
                   synth={"health_score": 60, "grade": "D", "summary": "s",
                          "themes": [], "top_risks": [], "quick_wins": [],
                          "roadmap": [{"step": "Purge migration debris",
                                       "rationale": "root cause", "addresses": ["x"],
                                       "effort": "M"}],
                          "gaps": ["no check for X"], "ai_findings": []})
    out = analyze_sectioned(_snap(), _findings(), prov, product="jira")
    f = next(x for x in out["ai_findings"] if x["title"] == "Migration debris")
    assert f["root_cause"].startswith("an unfinished import")
    assert f["priority"] == 1 and f["effort"] == "M"
    assert f["remediation_steps"] == ["export the field list", "delete the orphans"]
    # roadmap + gaps surface on the result too
    assert out["roadmap"] and out["roadmap"][0]["step"] == "Purge migration debris"
    assert out["gaps"] == ["no check for X"]


def test_synthesis_gets_higher_effort_than_sections():
    """Per-area passes run at the requested effort; synthesis one notch higher."""
    seen = []

    class _EffortSpy:
        def complete(self, system, user_content, *, model=None, effort="medium"):
            seen.append((user_content.startswith("Section:"), effort))
            body = ('{"area_findings": []}' if user_content.startswith("Section:")
                    else '{"grade": "B", "health_score": 80}')
            return {"text": body, "error": None, "refused": False, "model": "m"}

    analyze_sectioned(_snap(), _findings(), _EffortSpy(), product="jira",
                      effort="high")
    section_efforts = {e for is_sec, e in seen if is_sec}
    synth_efforts = {e for is_sec, e in seen if not is_sec}
    assert section_efforts == {"high"}
    assert synth_efforts == {"xhigh"}      # one notch above high
