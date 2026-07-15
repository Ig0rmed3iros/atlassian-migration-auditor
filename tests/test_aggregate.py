"""Unit tests for auditor.aggregate.derive_fidelity — display-time fidelity
derivation that retroactively splits systematic gaps out of core fidelity.

Synthetic data only (Acme / Globex / Igor Medeiros / placeholders).
"""
from auditor.aggregate import (
    derive_fidelity, SYS_FRAC, SYS_TARGET_EMPTY, SYS_ABS_FLOOR,
)


def _fm(project, key, field, src, tgt, kind="field_mismatch"):
    """A field/link mismatch finding as stored: values live in detail.src/tgt."""
    return {"project": project, "kind": kind, "src_key": key, "tgt_key": key,
            "field": field, "summary": f"{field} differs",
            "detail": {"src": src, "tgt": tgt, "sev": "med"}}


def _prow(key, common, mismatched):
    """A per-project row as derived from stored project_stats."""
    raw = round(100.0 * (common - mismatched) / common, 2) if common else None
    return {"key": key, "common": common, "issues_with_mismatches": mismatched,
            "fidelity_pct": raw}


# Floors are big in prod (200); tests scale the project so a single field
# mismatched on N issues clears BOTH floors: N >= SYS_ABS_FLOOR (absolute) and
# N >= SYS_FRAC * COMMON (fractional). COMMON is chosen so 0.30*COMMON < N.
N = SYS_ABS_FLOOR + 50               # 250 — clears the absolute floor
COMMON = int(N / SYS_FRAC) - 50      # ~783 — so SYS_FRAC*COMMON (~235) < N


def test_systematic_gap_flagged_and_excluded_from_core():
    """(a) A field empty-on-target on >=30% of issues with >=85% empty is
    flagged systematic and removed from core fidelity."""
    findings = []
    # N distinct issues, all with `environment` empty on target -> systematic.
    for i in range(N):
        findings.append(_fm("ACME", f"ACME-{i}", "environment",
                            "linux/prod", None))
    rows = [_prow("ACME", COMMON, N)]
    out = derive_fidelity(rows, findings)

    gaps = out["systematic_gaps"]
    assert len(gaps) == 1
    g = gaps[0]
    assert g["project"] == "ACME" and g["field"] == "environment"
    assert g["affected_issues"] == N
    assert g["target_empty_pct"] == 100.0

    # Core fidelity must EXCLUDE those issues: every mismatch was systematic.
    pp = {p["key"]: p for p in out["per_project"]}["ACME"]
    assert pp["core_mismatched"] == 0
    assert pp["fidelity_core"] == 100.0
    # Raw still reflects the stored value (all N count against it).
    assert pp["fidelity_raw"] == rows[0]["fidelity_pct"]
    assert pp["fidelity_core"] > pp["fidelity_raw"]


def test_few_issues_not_flagged():
    """(b) A field mismatched on only a handful of issues is NOT systematic."""
    findings = [
        _fm("ACME", f"ACME-{i}", "priority", "High", None) for i in range(5)
    ]
    rows = [_prow("ACME", COMMON, 5)]
    out = derive_fidelity(rows, findings)
    assert out["systematic_gaps"] == []
    pp = {p["key"]: p for p in out["per_project"]}["ACME"]
    # All 5 remain core (nothing excluded).
    assert pp["core_mismatched"] == 5
    assert pp["fidelity_core"] == pp["fidelity_raw"]


def test_issue_with_systematic_and_real_mismatch_still_counts_core():
    """(c) An issue that has a systematic-gap field AND another genuine
    mismatch still counts against core fidelity."""
    findings = []
    for i in range(N):
        findings.append(_fm("ACME", f"ACME-{i}", "environment",
                            "linux/prod", None))
    # The FIRST issue also has a real (non-systematic) status mismatch.
    findings.append(_fm("ACME", "ACME-0", "status", "Open", "Done"))
    rows = [_prow("ACME", COMMON, N)]  # N distinct mismatched issues total
    out = derive_fidelity(rows, findings)

    assert len(out["systematic_gaps"]) == 1   # environment still systematic
    pp = {p["key"]: p for p in out["per_project"]}["ACME"]
    # Exactly one issue (ACME-0) has a non-systematic mismatch.
    assert pp["core_mismatched"] == 1


def test_value_pair_and_top_pattern():
    """(d) top_pattern reports the dominant src value -> (empty) xN."""
    findings = []
    # 200 issues with src "Acme Cloud", 50 with src "Globex" — both empty tgt.
    for i in range(200):
        findings.append(_fm("ACME", f"ACME-{i}", "environment", "Acme Cloud", ""))
    for i in range(200, 250):
        findings.append(_fm("ACME", f"ACME-{i}", "environment", "Globex", None))
    rows = [_prow("ACME", COMMON, 250)]
    out = derive_fidelity(rows, findings)
    g = out["systematic_gaps"][0]
    assert g["affected_issues"] == 250
    assert g["top_pattern"] == "Acme Cloud -> (empty) x200"


def test_mostly_nonempty_target_not_flagged():
    """A field mismatched on many issues but where target is mostly NON-empty
    (a remap, not a gap) is NOT systematic — empty fraction below threshold."""
    findings = []
    for i in range(N):
        # target non-empty -> remapped value, not a hole
        findings.append(_fm("ACME", f"ACME-{i}", "status", "Open", "Reopened"))
    rows = [_prow("ACME", COMMON, N)]
    out = derive_fidelity(rows, findings)
    assert out["systematic_gaps"] == []
    pp = {p["key"]: p for p in out["per_project"]}["ACME"]
    assert pp["core_mismatched"] == N


def test_content_comment_attachment_kinds_are_not_fields():
    """content/comment/attachment mismatches are their own kinds, never folded
    into a field gap — and they always count as core mismatches."""
    findings = []
    for i in range(N):
        findings.append({"project": "ACME", "kind": "content_mismatch",
                         "src_key": f"ACME-{i}", "tgt_key": f"ACME-{i}",
                         "field": "description", "summary": "x",
                         "detail": {"src_len": 10, "tgt_len": 0, "sev": "high"}})
    rows = [_prow("ACME", COMMON, N)]
    out = derive_fidelity(rows, findings)
    assert out["systematic_gaps"] == []     # description is content, not a field
    pp = {p["key"]: p for p in out["per_project"]}["ACME"]
    assert pp["core_mismatched"] == N        # all count against core


def test_attachment_uncheckable_skipped_in_fidelity():
    """Confluence's capped-inline advisory is COVERAGE information, like
    comment_uncheckable: it must never count a page as core-mismatched."""
    findings = [{"project": "ENG", "kind": "attachment_uncheckable",
                 "src_key": f"Acme Page {i}", "tgt_key": f"Acme Page {i}",
                 "field": "attachment", "summary": "x",
                 "detail": {"src_inline": 25, "tgt_inline": 25,
                            "src_capped": True, "tgt_capped": False}}
                for i in range(N)]
    rows = [_prow("ENG", COMMON, 0)]
    out = derive_fidelity(rows, findings)
    assert out["systematic_gaps"] == []
    pp = {p["key"]: p for p in out["per_project"]}["ENG"]
    assert pp["core_mismatched"] == 0
    assert pp["fidelity_core"] == 100.0


def test_overall_fidelity_core_and_raw():
    """Overall block: core excludes systematic-only issues; raw does not."""
    findings = []
    for i in range(N):
        findings.append(_fm("ACME", f"ACME-{i}", "environment", "x", None))
    # one genuine extra mismatch on a fresh issue
    findings.append(_fm("ACME", "ACME-999", "status", "Open", "Done"))
    rows = [_prow("ACME", COMMON, N + 1)]
    out = derive_fidelity(rows, findings)
    o = out["overall"]
    # core_mismatched_total = just ACME-999
    assert o["core_mismatched_total"] == 1
    assert o["fidelity_core"] > o["fidelity_raw"]


def test_audited_param_sets_overall_denominator():
    """The overall denominator is the run-level `audited` total when supplied
    (matches the raw frontend formula), else falls back to sum of `common`."""
    findings = [_fm("ACME", "ACME-1", "status", "Open", "Done")]  # 1 core mismatch
    rows = [_prow("ACME", 100, 1)]  # common=100, issues_with_mismatches=1

    # default: denominator = sum(common) = 100 -> (100-0-0-1)/100 = 99.0
    default = derive_fidelity(rows, findings)["overall"]
    assert default["fidelity_core"] == 99.0
    assert default["fidelity_raw"] == 99.0

    # explicit run-level audited=200 -> (200-0-0-1)/200 = 99.5
    overridden = derive_fidelity(rows, findings, audited=200)["overall"]
    assert overridden["fidelity_core"] == 99.5
    assert overridden["fidelity_raw"] == 99.5


def test_link_mismatch_is_field_like():
    """link_mismatch participates in systematic-gap detection like a field."""
    findings = [_fm("ACME", f"ACME-{i}", "issuelinks", "blocks X", None,
                    kind="link_mismatch") for i in range(N)]
    rows = [_prow("ACME", COMMON, N)]
    out = derive_fidelity(rows, findings)
    assert len(out["systematic_gaps"]) == 1
    assert out["systematic_gaps"][0]["field"] == "issuelinks"


def test_constants_documented():
    assert SYS_FRAC == 0.30
    assert SYS_TARGET_EMPTY == 0.85
    assert SYS_ABS_FLOOR == 200


def test_target_direction_tails_do_not_deflate_overall_fidelity():
    """Items born on the LIVE TARGET after cutover are not part of the source
    population: subtracting them from a source-based denominator drove a
    perfect migration's headline fidelity below zero (audit finding 1)."""
    rows = [{"key": "ENG", "common": 10, "fidelity_pct": 100.0,
             "issues_with_mismatches": 0}]
    findings = [{"project": "ENG", "kind": "tail_post_cutover",
                 "src_key": None, "tgt_key": f"Target Page {i}",
                 "field": None, "summary": "x",
                 "detail": {"direction": "target", "cutover_epoch": 1.0}}
                for i in range(30)]
    out = derive_fidelity(rows, findings, audited=10)
    assert out["overall"]["fidelity_core"] == 100.0
    assert out["overall"]["fidelity_raw"] == 100.0


def test_source_direction_tails_still_subtract_from_overall():
    """Source-side tails ARE in the audited population (issues_src_total) and
    keep denting the overall number exactly as before."""
    rows = [{"key": "ENG", "common": 8, "fidelity_pct": 100.0,
             "issues_with_mismatches": 0}]
    findings = [{"project": "ENG", "kind": "tail_post_cutover",
                 "src_key": f"ENG-{i}", "tgt_key": None, "field": None,
                 "summary": "x", "detail": {"direction": "source"}}
                for i in range(2)]
    out = derive_fidelity(rows, findings, audited=10)
    assert out["overall"]["fidelity_core"] == 80.0
    assert out["overall"]["fidelity_raw"] == 80.0


def test_tail_direction_falls_back_to_src_key_when_detail_lacks_it():
    """Stored legacy findings may lack detail.direction: a row with only a
    tgt_key must still be excluded from the source-based subtraction."""
    rows = [{"key": "ENG", "common": 5, "fidelity_pct": 100.0,
             "issues_with_mismatches": 0}]
    findings = [
        {"project": "ENG", "kind": "tail_post_cutover", "src_key": "ENG-9",
         "tgt_key": None, "field": None, "summary": "x", "detail": {}},
        {"project": "ENG", "kind": "tail_post_cutover", "src_key": None,
         "tgt_key": "ENG-77", "field": None, "summary": "x", "detail": {}},
    ]
    out = derive_fidelity(rows, findings, audited=5)
    # only the src-keyed tail subtracts: (5-1)/5
    assert out["overall"]["fidelity_core"] == 80.0


def test_cf_value_mismatch_dents_core_fidelity():
    """A custom-field value mismatch is a real per-issue divergence; it must
    dent the DISPLAYED core fidelity and never be forgiven as a systematic gap
    (even when an entire field is lost on every issue)."""
    findings = [{"project": "ACME", "kind": "cf_value_mismatch",
                 "src_key": f"ACME-{i}", "tgt_key": f"ACME-{i}",
                 "field": "Impact", "summary": "Impact value missing in target",
                 "detail": {"sev": "high", "note": "lost"}} for i in range(N)]
    rows = [_prow("ACME", COMMON, N)]
    out = derive_fidelity(rows, findings)
    pp = {p["key"]: p for p in out["per_project"]}["ACME"]
    assert pp["core_mismatched"] == N            # NOT forgiven as systematic
    assert pp["fidelity_core"] < 100.0
    assert out["overall"]["fidelity_core"] < 100.0
    assert not out["systematic_gaps"]            # never forgiven as systematic
