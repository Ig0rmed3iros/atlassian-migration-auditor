import gzip
import json

import pytest

from auditor.compare import compare_project
from auditor.confluence.compare import compare_space
from auditor.textnorm import content_fp


def write_side(path, rows):
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def mk_page(title, parent=None, created="1700000000", creator="Igor Medeiros",
            version=1, labels=(), body="Acme operations prose",
            attachments=(), att_capped=False, att_size=None,
            comments=0, com_capped=False, com_size=None, macro_sig=None):
    """A slim extract row shaped exactly like confluence slim_page output."""
    return {"key": title, "id": title, "fields": {
        "title": title, "parent": parent, "created": created,
        "creator": creator, "version": version, "labels": list(labels),
        "body": {"len": len(body), "sha": content_fp(body), "head": body[:200]},
        "attachment": {"capped": att_capped, "size": att_size, "items": [
            {"filename": fn, "size": sz} for fn, sz in attachments]},
        "comment": {"count": comments, "capped": com_capped,
                    "size": com_size},
        "macros": {},
        "macro_sig": macro_sig if macro_sig is not None else content_fp(""),
    }}


@pytest.fixture()
def paths(tmp_path):
    return str(tmp_path / "src.gz"), str(tmp_path / "tgt.gz")


def kinds(findings):
    return sorted(f["kind"] for f in findings)


def test_presence_and_timestamp_tails(paths):
    src, tgt = paths
    # Common pages put the cutover line at created=1700000100 (source side).
    write_side(src, [mk_page("Acme Home", created="1700000000"),
                     mk_page("Acme Ops", created="1700000100"),
                     mk_page("Acme Postmortem", created="1700000110"),
                     mk_page("Acme Archive", created="1699999990")])
    write_side(tgt, [mk_page("Acme Home", created="1700000000"),
                     mk_page("Acme Ops", created="1700000100"),
                     mk_page("Globex Onboarding", created="1700000200"),
                     mk_page("Globex Legacy", created="1699999000")])
    out = compare_space("ENG", src, tgt)
    by = {(f["kind"], f.get("src_key") or f.get("tgt_key")): f
          for f in out["findings"]}
    # src-only page born AFTER the line = post-cutover drift, not loss.
    tail_src = by[("tail_post_cutover", "Acme Postmortem")]
    assert tail_src["detail"]["direction"] == "source"
    # src-only page born BEFORE the line = a genuine hole.
    assert ("missing_in_tgt", "Acme Archive") in by
    # tgt-only after the line = target-side drift; before = missing_in_src.
    tail_tgt = by[("tail_post_cutover", "Globex Onboarding")]
    assert tail_tgt["detail"]["direction"] == "target"
    assert ("missing_in_src", "Globex Legacy") in by
    assert out["stats"]["tails"] == 2
    assert out["stats"]["missing_in_tgt"] == 1
    assert out["stats"]["missing_in_src"] == 1

    # With NO common pages there is no cutover evidence: everything is
    # genuine missing/extra and nothing was compared (fidelity N/A).
    write_side(src, [mk_page("Acme Home", created="1700000500")])
    write_side(tgt, [mk_page("Globex Home", created="1700000600")])
    out = compare_space("ENG", src, tgt)
    assert kinds(out["findings"]) == ["missing_in_src", "missing_in_tgt"]
    assert out["stats"]["tails"] == 0
    assert out["stats"]["fidelity_pct"] is None


def test_collision_same_title_different_page(paths):
    src, tgt = paths
    write_side(src, [mk_page("Acme Home", created="1600000000",
                             creator="Igor Medeiros")])
    write_side(tgt, [mk_page("Acme Home", created="1750000000",
                             creator="Globex Importer", parent="Globex Root",
                             version=9)])
    out = compare_space("ENG", src, tgt)
    # The collision IS the finding; field diffs on a collided pair are noise.
    assert kinds(out["findings"]) == ["key_collision"]
    c = out["findings"][0]
    assert c["src_key"] == "Acme Home" and c["tgt_key"] == "Acme Home"
    assert c["detail"]["src_creator"] == "Igor Medeiros"
    assert c["detail"]["tgt_creator"] == "Globex Importer"
    assert out["stats"]["collisions"] == 1
    assert out["stats"]["fidelity_pct"] == 0.0   # 1 common, collided != clean
    # created drift with the SAME creator is not a collision — the diffs are
    # recorded as ordinary field mismatches instead.
    write_side(tgt, [mk_page("Acme Home", created="1750000000",
                             creator="Igor Medeiros")])
    out = compare_space("ENG", src, tgt)
    ks = kinds(out["findings"])
    assert "key_collision" not in ks and "field_mismatch" in ks


def test_field_and_body_mismatches(paths):
    src, tgt = paths
    write_side(src, [mk_page("Acme Home"),
                     mk_page("Acme Runbook", parent="Acme Home",
                             labels=("ops",), version=3,
                             body="restart the Globex feed nightly")])
    write_side(tgt, [mk_page("Acme Home"),
                     mk_page("Acme Runbook", parent="Acme Archive",
                             labels=("ops", "stale"), version=9,
                             body="page intentionally replaced")])
    out = compare_space("ENG", src, tgt)
    assert out["stats"]["field_mismatch_counts"] == {"parent": 1, "labels": 1,
                                                     "version": 1}
    fm = {f["field"]: f for f in out["findings"]
          if f["kind"] == "field_mismatch"}
    assert fm["parent"]["detail"] == {"src": "Acme Home",
                                      "tgt": "Acme Archive", "sev": "high"}
    assert fm["labels"]["detail"]["sev"] == "med"
    assert fm["version"]["detail"]["sev"] == "low"
    cm = next(f for f in out["findings"] if f["kind"] == "content_mismatch")
    assert cm["field"] == "body"
    assert cm["detail"]["src_len"] == len("restart the Globex feed nightly")
    assert "cross_dialect" not in cm["detail"]
    # one of two common pages mismatched -> (2-1)/2
    assert out["stats"]["issues_with_mismatches"] == 1
    assert out["stats"]["fidelity_pct"] == 50.0
    assert out["stats"]["severity_totals"]["high"] >= 2    # parent + body
    # cross-dialect passthrough badges the content finding.
    out2 = compare_space("ENG", src, tgt, cross_dialect=True)
    cm2 = next(f for f in out2["findings"] if f["kind"] == "content_mismatch")
    assert cm2["detail"]["cross_dialect"] is True


def test_attachment_and_comment_mismatches_when_fully_inline(paths):
    src, tgt = paths
    write_side(src, [mk_page("Acme Home",
                             attachments=(("diagram.png", 2048),),
                             comments=4)])
    write_side(tgt, [mk_page("Acme Home",
                             attachments=(("diagram.png", 2048),
                                          ("extra.bin", 5)),
                             comments=2)])
    out = compare_space("ENG", src, tgt)
    am = next(f for f in out["findings"] if f["kind"] == "attachment_mismatch")
    assert am["detail"]["extra_in_tgt"] == ["extra.bin|5"]
    cm = next(f for f in out["findings"] if f["kind"] == "comment_mismatch")
    assert cm["detail"] == {"src_total": 4, "tgt_total": 2, "sev": "high"}
    assert out["stats"]["issues_with_mismatches"] == 1
    assert out["stats"]["fidelity_pct"] == 0.0


def test_uncheckable_advisories_do_not_dent_fidelity(paths):
    src, tgt = paths
    # src caps overflowed and nothing visible PROVES a divergence: the extra
    # target attachment may sit in src's unfetched remainder, and the capped
    # src comment floor (25) does not exceed the complete tgt count (25).
    write_side(src, [mk_page("Acme Home",
                             attachments=(("diagram.png", 2048),),
                             att_capped=True,
                             comments=25, com_capped=True)])
    write_side(tgt, [mk_page("Acme Home",
                             attachments=(("diagram.png", 2048),
                                          ("other.png", 7)),
                             comments=25)])
    out = compare_space("ENG", src, tgt)
    ks = kinds(out["findings"])
    assert "attachment_uncheckable" in ks and "comment_uncheckable" in ks
    assert "attachment_mismatch" not in ks and "comment_mismatch" not in ks
    assert out["stats"]["attachments_uncheckable"] == 1
    assert out["stats"]["comments_uncheckable"] == 1
    assert out["stats"]["issues_with_mismatches"] == 0
    assert out["stats"]["fidelity_pct"] == 100.0   # coverage gap, not mismatch


# ------------------------------------------------------------- finding 5
def test_capped_comment_floor_proves_mismatch_against_complete_side(paths):
    """An uncapped side's count is COMPLETE by definition; a capped side's
    inline count is a floor. floor > complete proves the counts differ —
    downgrading a provable divergence to a mere coverage advisory reported
    real comment loss as clean (audit finding 5)."""
    src, tgt = paths
    write_side(src, [mk_page("Acme Home", comments=2)])               # exact 2
    write_side(tgt, [mk_page("Acme Home", comments=25, com_capped=True)])
    out = compare_space("ENG", src, tgt)
    cm = next(f for f in out["findings"] if f["kind"] == "comment_mismatch")
    assert cm["detail"]["proven_from_partial"] is True
    assert "comment_uncheckable" not in kinds(out["findings"])
    assert out["stats"]["comments_uncheckable"] == 0
    assert out["stats"]["issues_with_mismatches"] == 1
    assert out["stats"]["fidelity_pct"] == 0.0


def test_capped_comment_declared_size_is_also_a_floor(paths):
    src, tgt = paths
    write_side(src, [mk_page("Acme Home", comments=25, com_capped=True,
                             com_size=40)])     # floor 40
    write_side(tgt, [mk_page("Acme Home", comments=30)])              # exact 30
    out = compare_space("ENG", src, tgt)
    assert any(f["kind"] == "comment_mismatch" for f in out["findings"])


def test_capped_attachment_floor_and_membership_prove_mismatch(paths):
    src, tgt = paths
    # tgt capped, but its INLINE rows already contain a file the complete
    # source set does not have — that file provably exists only on target.
    write_side(src, [mk_page("Acme Home",
                             attachments=(("diagram.png", 2048),))])
    write_side(tgt, [mk_page("Acme Home",
                             attachments=(("diagram.png", 2048),
                                          ("rogue.bin", 7)),
                             att_capped=True)])
    out = compare_space("ENG", src, tgt)
    am = next(f for f in out["findings"] if f["kind"] == "attachment_mismatch")
    assert am["detail"]["proven_from_partial"] is True
    assert am["detail"]["extra_in_tgt"] == ["rogue.bin|7"]
    assert out["stats"]["attachments_uncheckable"] == 0
    assert out["stats"]["issues_with_mismatches"] == 1
    # count-floor proof: capped side declares more children than the
    # complete side has in total.
    write_side(src, [mk_page("Acme Home",
                             attachments=(("diagram.png", 2048),))])
    write_side(tgt, [mk_page("Acme Home",
                             attachments=(("diagram.png", 2048),),
                             att_capped=True, att_size=40)])
    out2 = compare_space("ENG", src, tgt)
    assert any(f["kind"] == "attachment_mismatch" for f in out2["findings"])


def test_both_sides_capped_is_never_provable(paths):
    src, tgt = paths
    write_side(src, [mk_page("Acme Home", comments=25, com_capped=True,
                             attachments=(("a.png", 1),), att_capped=True)])
    write_side(tgt, [mk_page("Acme Home", comments=25, com_capped=True,
                             attachments=(("b.png", 2),), att_capped=True)])
    out = compare_space("ENG", src, tgt)
    ks = kinds(out["findings"])
    assert "comment_mismatch" not in ks and "attachment_mismatch" not in ks
    assert "comment_uncheckable" in ks and "attachment_uncheckable" in ks


def test_stats_contract_keys_match_jira(tmp_path):
    # The firewall: findings.py / aggregate.py / analysis.py read stats by
    # jira's key names, so EVERY jira key must exist in confluence stats.
    # The single allowed extra is the documented advisory counter
    # attachments_uncheckable (jira has per-issue attachment lists and never
    # needs it; consumers read by name, so an additive key is harmless).
    jsrc, jtgt = str(tmp_path / "j_src.gz"), str(tmp_path / "j_tgt.gz")
    issue = {"key": "AC-1", "fields": {"summary": "synthetic"}}
    write_side(jsrc, [issue])
    write_side(jtgt, [issue])
    jira_stats = compare_project("AC", jsrc, jtgt)["stats"]
    csrc, ctgt = str(tmp_path / "c_src.gz"), str(tmp_path / "c_tgt.gz")
    write_side(csrc, [mk_page("Acme Home")])
    write_side(ctgt, [mk_page("Acme Home")])
    conf_stats = compare_space("ENG", csrc, ctgt)["stats"]
    assert set(jira_stats) <= set(conf_stats)
    assert set(conf_stats) - set(jira_stats) == {"attachments_uncheckable"}
    # jira-only concepts carry their empty shapes, not missing keys.
    assert conf_stats["remap"] == {} and conf_stats["unmapped_users"] == []
    assert conf_stats["distinct_src_people"] == 1


def test_titleless_rows_compare_without_crashing(paths):
    # Integration seam of slim_page's no-crash invariant: a content row the
    # API served without a title flows extract -> compare as an id-keyed row.
    # Pre-fix this crashed the whole run — sorted(sk & tk) when both sides
    # carry the None key, summary k[:200] when only one does.
    from auditor.confluence.extract import slim_page
    src, tgt = paths
    titleless = {"id": "98304",
                 "body": {"storage": {"value": "<p>orphan prose</p>"}},
                 "version": {"number": 1},
                 "history": {"createdDate": "2024-01-02T03:04:05.000Z",
                             "createdBy": {"displayName": "Igor Medeiros"}}}
    # Both sides carry the title-less row (same id on both extracts).
    write_side(src, [slim_page(titleless), mk_page("Acme Home")])
    write_side(tgt, [slim_page(titleless), mk_page("Acme Home")])
    out = compare_space("ENG", src, tgt)
    assert out["stats"]["common"] == 2
    assert out["stats"]["issues_with_mismatches"] == 0
    # Source-only title-less row: an id-keyed presence finding, not a crash.
    write_side(src, [slim_page(titleless)])
    write_side(tgt, [mk_page("Acme Home")])
    out2 = compare_space("ENG", src, tgt)
    assert ("missing_in_tgt", "98304") in {
        (f["kind"], f.get("src_key")) for f in out2["findings"]}


def test_load_skips_format_header_line(paths):
    from auditor.extract import EXTRACT_FORMAT
    src, tgt = paths
    write_side(src, [{"_extract_format": EXTRACT_FORMAT},
                     mk_page("Acme Home")])
    write_side(tgt, [mk_page("Acme Home")])         # legacy: no header
    out = compare_space("ENG", src, tgt)
    assert out["findings"] == []
    assert out["stats"]["common"] == 1 and out["stats"]["src"] == 1


def test_macro_param_mismatch_dents_when_only_macro_target_differs(paths):
    # Same prose (same body sha), but a macro points at a different target ->
    # the body sha is EQUAL (storage_text strips params), so without macro_sig
    # this is a false clean. macro_sig must catch it and dent fidelity.
    src, tgt = paths
    write_side(src, [mk_page("Dashboard", body="same prose", macro_sig="AAAA")])
    write_side(tgt, [mk_page("Dashboard", body="same prose", macro_sig="BBBB")])
    out = compare_space("ENG", src, tgt)
    hits = [f for f in out["findings"] if f["kind"] == "macro_param_mismatch"]
    assert hits and hits[0]["field"] == "macros"
    assert out["stats"]["fidelity_pct"] == 0.0


def test_macro_sig_equal_no_finding(paths):
    src, tgt = paths
    rows = [mk_page("Dashboard", macro_sig="AAAA")]
    write_side(src, rows); write_side(tgt, rows)
    out = compare_space("ENG", src, tgt)
    assert not [f for f in out["findings"] if f["kind"] == "macro_param_mismatch"]
    assert out["stats"]["fidelity_pct"] == 100.0


def _blog(title, **kw):
    row = mk_page(title, **kw)
    row["key"] = f"[blog] {title}"
    row["fields"]["content_type"] = "blogpost"
    return row


def test_blog_dropped_on_target_is_reported_not_a_false_clean(paths):
    # A migration that drops a blog post must NOT read clean — the blog is its
    # own comparison row (namespaced key), so its absence is a presence finding.
    src, tgt = paths
    write_side(src, [mk_page("Home"), _blog("Launch Day")])
    write_side(tgt, [mk_page("Home")])               # the blog was dropped
    out = compare_space("ENG", src, tgt)
    reported = {f.get("src_key") for f in out["findings"]}
    assert "[blog] Launch Day" in reported           # not silently clean
    assert out["stats"]["fidelity_pct"] is not None


def test_page_and_blog_same_title_do_not_collide(paths):
    # A page and a blog sharing a title are distinct rows on both sides.
    src, tgt = paths
    rows = [mk_page("Roadmap"), _blog("Roadmap")]
    write_side(src, rows); write_side(tgt, rows)
    out = compare_space("ENG", src, tgt)
    assert out["stats"]["common"] == 2               # two distinct rows, both clean
    assert out["stats"]["fidelity_pct"] == 100.0
