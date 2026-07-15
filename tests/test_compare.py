import gzip, json
import pytest
from auditor.client import h16
from auditor.compare import compare_project


def write_side(path, rows):
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def mk_issue(key, summary="s", desc="body", status="Open", created="2026-01-01T00:00:00.000+0000",
             reporter="Ana", comments=(), attachments=(), labels=(), links=()):
    return {"key": key, "id": key, "fields": {
        "summary": summary,
        "description": {"len": len(desc), "sha": h16(desc), "head": desc[:200]},
        "issuetype": {"name": "Task"}, "status": {"name": status},
        "priority": {"name": "P3"}, "resolution": None, "resolutiondate": None,
        "created": created, "updated": "x", "duedate": None,
        "labels": list(labels), "components": [], "fixVersions": [], "versions": [],
        "parent": None, "environment": None, "security": None,
        "assignee": {"displayName": "Bob"}, "reporter": {"displayName": reporter},
        "creator": {"displayName": reporter},
        "comment": {"total": len(comments), "inline": len(comments),
                    "items": [{"author": "A", "created": "c", "updated": "u",
                               "len": len(t), "sha": h16(t)} for t in comments]},
        "worklog": {"total": 0}, "votes": {"votes": 0}, "watches": {"watchCount": 0},
        "attachment": [{"filename": fn, "size": sz, "created": "c", "author": "A"}
                       for fn, sz in attachments],
        "issuelinks": [{"type": t, "inward": i, "outward": o} for t, i, o in links],
    }}


@pytest.fixture()
def paths(tmp_path):
    return str(tmp_path / "src.gz"), str(tmp_path / "tgt.gz")


def kinds(findings):
    return sorted(f["kind"] for f in findings)


def test_identical_sides_produce_no_findings(paths):
    src, tgt = paths
    rows = [mk_issue("AC-1"), mk_issue("AC-2")]
    write_side(src, rows); write_side(tgt, rows)
    out = compare_project("AC", src, tgt)
    assert out["findings"] == []
    assert out["stats"]["src"] == 2 and out["stats"]["common"] == 2
    assert out["stats"]["fidelity_pct"] == 100.0


def test_genuine_hole_vs_post_cutover_tail(paths):
    src, tgt = paths
    # target max key-num = 3; AC-2 missing below the line = HOLE,
    # AC-9 missing above the line = expected tail.
    write_side(src, [mk_issue("AC-1"), mk_issue("AC-2"), mk_issue("AC-3"),
                     mk_issue("AC-9")])
    write_side(tgt, [mk_issue("AC-1"), mk_issue("AC-3")])
    out = compare_project("AC", src, tgt)
    by_kind = {f["kind"]: f for f in out["findings"]}
    assert by_kind["missing_in_tgt"]["src_key"] == "AC-2"
    tail = by_kind["tail_post_cutover"]
    assert tail["src_key"] == "AC-9" and tail["detail"]["direction"] == "source"
    assert out["stats"]["missing_in_tgt"] == 1 and out["stats"]["tails"] == 1


def test_target_extra_above_src_max_is_target_tail(paths):
    src, tgt = paths
    write_side(src, [mk_issue("AC-1")])
    write_side(tgt, [mk_issue("AC-1"), mk_issue("AC-5")])
    out = compare_project("AC", src, tgt)
    f = out["findings"][0]
    assert f["kind"] == "tail_post_cutover" and f["detail"]["direction"] == "target"
    assert f["tgt_key"] == "AC-5"


def test_target_extra_below_src_max_is_missing_in_src(paths):
    src, tgt = paths
    write_side(src, [mk_issue("AC-1"), mk_issue("AC-9")])
    write_side(tgt, [mk_issue("AC-1"), mk_issue("AC-5"), mk_issue("AC-9")])
    out = compare_project("AC", src, tgt)
    assert kinds(out["findings"]) == ["missing_in_src"]


def test_field_and_content_mismatches(paths):
    src, tgt = paths
    write_side(src, [mk_issue("AC-1", status="Open", desc="original")])
    write_side(tgt, [mk_issue("AC-1", status="Done", desc="rewritten")])
    out = compare_project("AC", src, tgt)
    ks = kinds(out["findings"])
    assert "field_mismatch" in ks and "content_mismatch" in ks
    fm = next(f for f in out["findings"] if f["kind"] == "field_mismatch")
    assert fm["field"] == "status" and fm["detail"]["src"] == "Open" \
        and fm["detail"]["tgt"] == "Done" and fm["detail"]["sev"] == "high"
    assert out["stats"]["remap"]["status"][0]["count"] == 1


def test_comment_and_attachment_fidelity(paths):
    src, tgt = paths
    write_side(src, [mk_issue("AC-1", comments=("hello", "world"),
                              attachments=(("a.png", 10),))])
    write_side(tgt, [mk_issue("AC-1", comments=("hello", "DIFFERENT"),
                              attachments=(("a.png", 10), ("b.png", 5)))])
    out = compare_project("AC", src, tgt)
    ks = kinds(out["findings"])
    assert "comment_mismatch" in ks and "attachment_mismatch" in ks
    am = next(f for f in out["findings"] if f["kind"] == "attachment_mismatch")
    assert am["detail"]["extra_in_tgt"] == ["b.png|5"]


def test_key_collision_when_identity_metadata_disagrees(paths):
    src, tgt = paths
    write_side(src, [mk_issue("AC-1", summary="real issue", reporter="Ana",
                              created="2020-01-01T00:00:00.000+0000")])
    write_side(tgt, [mk_issue("AC-1", summary="totally different", reporter="Zed",
                              created="2026-06-01T00:00:00.000+0000")])
    out = compare_project("AC", src, tgt)
    assert "key_collision" in kinds(out["findings"])


def test_unmapped_users_in_stats(paths):
    src, tgt = paths
    s = mk_issue("AC-1", reporter="Ana")
    t = mk_issue("AC-1", reporter="Ana")
    t["fields"]["assignee"] = {"displayName": "Former user"}
    write_side(src, [s]); write_side(tgt, [t])
    out = compare_project("AC", src, tgt)
    assert {"src": "Bob", "occurrences": 1} in out["stats"]["unmapped_users"]


def test_empty_target_is_genuine_loss_not_tail(paths):
    src, tgt = paths
    write_side(src, [mk_issue("AC-1"), mk_issue("AC-2")])
    write_side(tgt, [])
    out = compare_project("AC", src, tgt)
    assert kinds(out["findings"]) == ["missing_in_tgt", "missing_in_tgt"]
    assert out["stats"]["tails"] == 0
    assert out["stats"]["fidelity_pct"] is None     # nothing was compared


def test_rekeyed_target_is_paired_not_reported_as_loss(paths):
    # A project-key rename (AC-1 -> AC-101) with DISTINCT, content-matching issues
    # is paired across the key gap and NOT reported as loss. Re-keyed survivors
    # leave the missing set; nothing genuine is hidden (no-bias review).
    src, tgt = paths
    write_side(src, [mk_issue("AC-1", desc="alpha", created="2026-01-01T00:00:00.000+0000"),
                     mk_issue("AC-2", desc="beta", created="2026-01-02T00:00:00.000+0000")])
    write_side(tgt, [mk_issue("AC-101", desc="alpha", created="2026-01-01T00:00:00.000+0000"),
                     mk_issue("AC-102", desc="beta", created="2026-01-02T00:00:00.000+0000")])
    out = compare_project("AC", src, tgt)
    assert out["stats"]["rekey_suspected"] is True
    assert out["stats"]["missing_in_tgt"] == 0      # both re-keyed, none lost
    assert "rekey_suspected" in kinds(out["findings"])
    assert "missing_in_tgt" not in kinds(out["findings"])


def test_partial_loss_with_rekey_still_reports_the_losses(paths):
    # CRITICAL guard (no-bias review): a migration that re-keys some survivors AND
    # loses others must STILL report the genuine losses — never hide them.
    src, tgt = paths
    write_side(src, [
        mk_issue("AC-1", desc="alpha", created="2026-01-01T00:00:00.000+0000"),  # re-keyed
        mk_issue("AC-2", desc="lost-a", created="2026-01-02T00:00:00.000+0000"),  # LOST
        mk_issue("AC-3", desc="lost-b", created="2026-01-03T00:00:00.000+0000")])  # LOST
    write_side(tgt, [
        mk_issue("AC-101", desc="alpha", created="2026-01-01T00:00:00.000+0000")])  # survivor
    out = compare_project("AC", src, tgt)
    assert out["stats"]["rekey_suspected"] is True
    assert out["stats"]["missing_in_tgt"] == 2      # the two genuine losses survive
    miss = {f.get("src_key") for f in out["findings"] if f["kind"] == "missing_in_tgt"}
    assert miss == {"AC-2", "AC-3"}


def test_unrelated_issues_no_overlap_are_loss_not_rekey(paths):
    # No key overlap but DIFFERENT content (different created/desc) is genuine
    # loss + extra, NOT a re-key. The content fingerprint must distinguish them.
    src, tgt = paths
    write_side(src, [mk_issue("AC-1", desc="alpha", created="2026-01-01T00:00:00.000+0000")])
    write_side(tgt, [mk_issue("ZZ-9", desc="omega", created="2026-02-02T00:00:00.000+0000")])
    out = compare_project("AC", src, tgt)
    assert out["stats"].get("rekey_suspected") is False
    assert "missing_in_tgt" in kinds(out["findings"])


def test_empty_target_is_loss_not_rekey(paths):
    # An empty target is genuine total loss, NOT a re-key — the comparable-
    # population guard must keep reporting it as missing.
    src, tgt = paths
    write_side(src, [mk_issue("AC-1"), mk_issue("AC-2")])
    write_side(tgt, [])
    out = compare_project("AC", src, tgt)
    assert out["stats"].get("rekey_suspected") in (False, None)
    assert out["stats"]["missing_in_tgt"] == 2


def test_collision_counts_against_fidelity(paths):
    src, tgt = paths
    write_side(src, [mk_issue("AC-1", summary="real", reporter="Ana",
                              created="2020-01-01T00:00:00.000+0000")])
    write_side(tgt, [mk_issue("AC-1", summary="different", reporter="Zed",
                              created="2026-06-01T00:00:00.000+0000")])
    out = compare_project("AC", src, tgt)
    assert out["stats"]["collisions"] == 1
    assert out["stats"]["fidelity_pct"] == 0.0      # 1 common, collided != clean


def test_renamed_reporter_and_edited_summary_is_not_a_collision(paths):
    src, tgt = paths
    same_created = "2020-01-01T00:00:00.000+0000"
    write_side(src, [mk_issue("AC-1", summary="orig", reporter="Jane Smith",
                              created=same_created)])
    write_side(tgt, [mk_issue("AC-1", summary="edited", reporter="Jane Doe",
                              created=same_created)])
    out = compare_project("AC", src, tgt)
    ks = kinds(out["findings"])
    assert "key_collision" not in ks
    assert "field_mismatch" in ks                   # the real diffs are recorded


def test_big_comment_issues_surface_as_uncheckable(paths):
    src, tgt = paths
    s = mk_issue("AC-1", comments=("a",) * 3)
    t = mk_issue("AC-1", comments=("b",) * 3)
    for side_issue in (s, t):
        side_issue["fields"]["comment"]["total"] = 150   # > inline
    write_side(src, [s]); write_side(tgt, [t])
    out = compare_project("AC", src, tgt)
    assert out["stats"]["comments_uncheckable"] == 1
    assert "comment_uncheckable" in kinds(out["findings"])
    assert out["stats"]["fidelity_pct"] == 100.0    # coverage gap, not a mismatch


def test_cross_dialect_flag_on_content_findings(paths):
    src, tgt = paths
    write_side(src, [mk_issue("AC-1", desc="original prose",
                              comments=("hello",))])
    write_side(tgt, [mk_issue("AC-1", desc="rewritten prose",
                              comments=("changed",))])
    out = compare_project("AC", src, tgt, cross_dialect=True)
    by_kind = {f["kind"]: f for f in out["findings"]}
    assert by_kind["content_mismatch"]["detail"]["cross_dialect"] is True
    assert by_kind["comment_mismatch"]["detail"]["cross_dialect"] is True
    out = compare_project("AC", src, tgt)
    by_kind = {f["kind"]: f for f in out["findings"]}
    assert "cross_dialect" not in by_kind["content_mismatch"]["detail"]
    assert "cross_dialect" not in by_kind["comment_mismatch"]["detail"]


def test_content_findings_feed_severity_totals(paths):
    src, tgt = paths
    write_side(src, [mk_issue("AC-1", desc="original",
                              attachments=(("a.png", 1),))])
    write_side(tgt, [mk_issue("AC-1", desc="rewritten", attachments=())])
    out = compare_project("AC", src, tgt)
    assert out["stats"]["severity_totals"].get("high", 0) >= 2


def test_load_skips_format_header_line(paths):
    src, tgt = paths
    from auditor.extract import EXTRACT_FORMAT
    header = {"_extract_format": EXTRACT_FORMAT}
    write_side(src, [header, mk_issue("AC-1")])
    write_side(tgt, [mk_issue("AC-1")])           # legacy: no header
    out = compare_project("AC", src, tgt)
    assert out["findings"] == []
    assert out["stats"]["common"] == 1 and out["stats"]["src"] == 1


# ---------------------------------------------------------------------------
# Custom-field VALUE comparison (EXTRACT_FORMAT 4). Values are matched by NAME;
# a drift or loss DENTS fidelity, added data does not, identity/representation-
# sensitive types are badged verify_sensitive, and ambiguous / one-side-only
# fields are disclosed rather than silently scored.
# ---------------------------------------------------------------------------
from auditor.extract import EXTRACT_FORMAT


def write_with_header(path, rows, cf_names=(), cf_ambiguous=()):
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"_extract_format": EXTRACT_FORMAT,
                             "cf_names": list(cf_names),
                             "cf_ambiguous": list(cf_ambiguous)}) + "\n")
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def issue_with_cf(key, cfvals):
    iss = mk_issue(key)
    iss["fields"]["_cf"] = cfvals
    return iss


def _v(fp, kind="option"):
    return {"fp": fp, "kind": kind}


def test_cf_value_mismatch_dents_fidelity(paths):
    src, tgt = paths
    write_with_header(src, [issue_with_cf("AC-1", {"Severity": _v("AAA")})],
                      cf_names=["Severity"])
    write_with_header(tgt, [issue_with_cf("AC-1", {"Severity": _v("BBB")})],
                      cf_names=["Severity"])
    out = compare_project("AC", src, tgt)
    cfm = [f for f in out["findings"] if f["kind"] == "cf_value_mismatch"]
    assert cfm and cfm[0]["field"] == "Severity"
    assert out["stats"]["fidelity_pct"] == 0.0     # 1 common, 1 dented


def test_cf_value_match_no_finding(paths):
    src, tgt = paths
    rows = [issue_with_cf("AC-1", {"Severity": _v("AAA")})]
    write_with_header(src, rows, cf_names=["Severity"])
    write_with_header(tgt, rows, cf_names=["Severity"])
    out = compare_project("AC", src, tgt)
    assert not [f for f in out["findings"] if f["kind"].startswith("cf_")]
    assert out["stats"]["fidelity_pct"] == 100.0


def test_cf_value_lost_when_field_exists_on_target(paths):
    # Source issue has a value; the field EXISTS on the target instance but the
    # target issue's value is empty -> per-issue value loss -> dents.
    src, tgt = paths
    write_with_header(src, [issue_with_cf("AC-1", {"Severity": _v("AAA")})],
                      cf_names=["Severity"])
    write_with_header(tgt, [issue_with_cf("AC-1", {})], cf_names=["Severity"])
    out = compare_project("AC", src, tgt)
    assert [f for f in out["findings"] if f["kind"] == "cf_value_mismatch"]
    assert out["stats"]["fidelity_pct"] == 0.0


def test_cf_field_absent_on_target_is_disclosed_and_dents(paths):
    # The field does NOT exist on the target instance at all -> genuine value
    # loss for every source issue that had a value. Disclosed once (high), and
    # the affected issues dent fidelity (never a false clean).
    src, tgt = paths
    write_with_header(src, [issue_with_cf("AC-1", {"Impact": _v("X")})],
                      cf_names=["Impact"])
    write_with_header(tgt, [issue_with_cf("AC-1", {})], cf_names=[])
    out = compare_project("AC", src, tgt)
    d = [f for f in out["findings"] if f["kind"] == "cf_field_not_in_target"]
    assert d and d[0]["detail"]["affected_issues"] == 1
    assert out["stats"]["fidelity_pct"] == 0.0


def test_cf_value_added_on_target_does_not_dent(paths):
    # Target issue has a value the source issue lacked, but the field exists on
    # the source instance -> added data, not loss -> must NOT dent fidelity.
    src, tgt = paths
    write_with_header(src, [issue_with_cf("AC-1", {})], cf_names=["Note"])
    write_with_header(tgt, [issue_with_cf("AC-1", {"Note": _v("Y")})],
                      cf_names=["Note"])
    out = compare_project("AC", src, tgt)
    assert out["stats"]["fidelity_pct"] == 100.0
    assert not [f for f in out["findings"] if f["kind"] == "cf_value_mismatch"]


def test_sensitive_kind_mismatch_is_badged(paths):
    src, tgt = paths
    write_with_header(src, [issue_with_cf("AC-1", {"Owner": _v("AAA", "user")})],
                      cf_names=["Owner"])
    write_with_header(tgt, [issue_with_cf("AC-1", {"Owner": _v("BBB", "user")})],
                      cf_names=["Owner"])
    out = compare_project("AC", src, tgt)
    cfm = [f for f in out["findings"] if f["kind"] == "cf_value_mismatch"]
    assert cfm and cfm[0]["detail"].get("verify_sensitive") is True


def test_ambiguous_cf_names_disclosed_without_denting(paths):
    src, tgt = paths
    write_with_header(src, [issue_with_cf("AC-1", {})],
                      cf_names=["Severity"], cf_ambiguous=["Region"])
    write_with_header(tgt, [issue_with_cf("AC-1", {})],
                      cf_names=["Severity"], cf_ambiguous=["Region"])
    out = compare_project("AC", src, tgt)
    assert [f for f in out["findings"] if f["kind"] == "cf_value_not_compared"]
    assert out["stats"]["fidelity_pct"] == 100.0


def test_cf_kind_mismatch_across_instances_is_disclosed_not_dented(paths):
    # Same field name, DIFFERENT normalized kind per side (e.g. DC /field lacks
    # schema -> app, Cloud typed -> option) is a cross-instance SCHEMA
    # divergence, not a value mismatch. Disclose; never false-dent.
    src, tgt = paths
    write_with_header(src, [issue_with_cf("AC-1", {"F": _v("AAA", "option")})],
                      cf_names=["F"])
    write_with_header(tgt, [issue_with_cf("AC-1", {"F": _v("BBB", "app")})],
                      cf_names=["F"])
    out = compare_project("AC", src, tgt)
    assert not [f for f in out["findings"] if f["kind"] == "cf_value_mismatch"]
    assert [f for f in out["findings"] if f["kind"] == "cf_value_not_compared"]
    assert out["stats"]["fidelity_pct"] == 100.0


def test_cf_field_absent_emits_per_issue_finding(paths):
    # The absent-field loss must emit a finding with a REAL src_key, so it flows
    # through derive_fidelity into the DISPLAYED fidelity_core — a null-keyed
    # project rollup alone is a false clean (the headline would stay 100%).
    src, tgt = paths
    write_with_header(src, [issue_with_cf("AC-1", {"Impact": _v("X")})],
                      cf_names=["Impact"])
    write_with_header(tgt, [issue_with_cf("AC-1", {})], cf_names=[])
    out = compare_project("AC", src, tgt)
    per_issue = [f for f in out["findings"]
                 if f["kind"] == "cf_value_mismatch" and f["src_key"] == "AC-1"]
    assert per_issue, "absent-field loss must emit a per-issue finding"
