from auditor.findings import build_run_summary

_UNSET = object()  # sentinel for fidelity_pct default


def proj(missing=0, tails=0, collisions=0, mismatched=0, src=100, common=None,
         fidelity_pct=_UNSET):
    common = common if common is not None else src - missing - tails
    if fidelity_pct is _UNSET:
        fidelity_pct = round(100 * (common - mismatched) /
                             common, 2) if common else 100.0
    return {"stats": {"project": "AC", "src": src, "tgt": common,
                      "common": common, "missing_in_tgt": missing,
                      "missing_in_src": 0, "tails": tails,
                      "collisions": collisions,
                      "issues_with_mismatches": mismatched,
                      "fidelity_pct": fidelity_pct}}


def cfg(n_missing=0, extra_findings=None):
    findings = [
        {"area": "statuses", "name": f"S{i}", "kind": "missing_in_tgt",
         "detail": {}} for i in range(n_missing)
    ]
    if extra_findings:
        findings.extend(extra_findings)
    return {"areas": {}, "findings": findings}


# ── Plan tests (verbatim from task-09.md) ────────────────────────────────────

def test_clean():
    s = build_run_summary({"AC": proj()}, cfg(), [])
    assert s["verdict"] == "CLEAN"
    assert s["stats"]["issues_src_total"] == 100


def test_tails_only_is_clean_with_tails():
    s = build_run_summary({"AC": proj(tails=5)}, cfg(), [])
    assert s["verdict"] == "CLEAN_WITH_TAILS"
    assert any("tail" in h.lower() for h in s["headlines"])


def test_mismatches_or_config_gaps_are_gaps_found():
    assert build_run_summary({"AC": proj(mismatched=3)}, cfg(), [])["verdict"] \
        == "GAPS_FOUND"
    assert build_run_summary({"AC": proj()}, cfg(5), [])["verdict"] == "GAPS_FOUND"


def test_rekey_suspected_is_gaps_found_not_critical():
    # A re-keyed pairing must land at GAPS_FOUND (needs verification), never
    # CRITICAL "data loss" and never CLEAN (no-bias review: false 100% loss).
    s = build_run_summary({"AC": {"stats": {
        "project": "AC", "src": 100, "tgt": 100, "common": 0,
        "missing_in_tgt": 0, "missing_in_src": 0, "tails": 0, "collisions": 0,
        "issues_with_mismatches": 0, "rekey_suspected": True,
        "fidelity_pct": None}}}, cfg(), [])
    assert s["verdict"] == "GAPS_FOUND"
    assert any("share no" in h for h in s["headlines"])


def test_holes_collisions_or_blindspots_are_critical():
    assert build_run_summary({"AC": proj(missing=2)}, cfg(), [])["verdict"] \
        == "CRITICAL"
    assert build_run_summary({"AC": proj(collisions=1)}, cfg(), [])["verdict"] \
        == "CRITICAL"
    bs = [{"key": "MS", "search_count": 0, "insight_count": 16016,
           "blind_spot": True}]
    s = build_run_summary({"AC": proj()}, cfg(), bs)
    assert s["verdict"] == "CRITICAL"
    assert any("blind" in h.lower() for h in s["headlines"])


def test_headlines_name_the_worst_project():
    s = build_run_summary({"AC": proj(missing=7)}, cfg(), [])
    assert any("AC" in h and "7" in h for h in s["headlines"])


# ── Amendment 1: area_error findings ─────────────────────────────────────────
# An area_error means a side was unreachable — we cannot certify what we
# couldn't read. Verdict must be AT LEAST GAPS_FOUND.

def test_area_error_gives_gaps_found():
    """A single area_error in config findings lifts verdict to GAPS_FOUND."""
    area_err = {"area": "fields", "name": "fields",
                "kind": "area_error",
                "detail": {"side": "source", "error": "ERR403:Forbidden"}}
    s = build_run_summary({"AC": proj()}, cfg(extra_findings=[area_err]), [])
    assert s["verdict"] == "GAPS_FOUND"


def test_area_error_headline_and_count():
    """area_errors are surfaced as a headline and as stats['area_errors']."""
    area_err = {"area": "workflows", "name": "workflows",
                "kind": "area_error",
                "detail": {"side": "target", "error": "ERR401:Unauthorized"}}
    s = build_run_summary({"AC": proj()}, cfg(extra_findings=[area_err]), [])
    # headline must mention the count AND incompleteness
    assert any("area" in h.lower() and "read" in h.lower()
               for h in s["headlines"]), s["headlines"]
    # stats must expose it explicitly, not just fold into cfg_other
    assert s["stats"]["area_errors"] == 1


def test_area_error_does_not_override_critical():
    """When holes exist too, verdict stays CRITICAL (not downgraded)."""
    area_err = {"area": "fields", "name": "fields",
                "kind": "area_error",
                "detail": {"side": "source", "error": "ERR403"}}
    s = build_run_summary({"AC": proj(missing=1)}, cfg(extra_findings=[area_err]), [])
    assert s["verdict"] == "CRITICAL"


# ── Amendment 2: fidelity_pct can be None ────────────────────────────────────
# compare_project returns None when no issues were compared.  build_run_summary
# must not crash and must produce a sane verdict.

def test_none_fidelity_pct_no_crash():
    """fidelity_pct=None (nothing compared) must not crash; verdict is CLEAN."""
    s = build_run_summary({"AC": proj(fidelity_pct=None)}, cfg(), [])
    # no exception, sane verdict
    assert s["verdict"] in ("CLEAN", "CLEAN_WITH_TAILS", "GAPS_FOUND", "CRITICAL")


def test_none_fidelity_pct_with_config_gaps():
    """fidelity_pct=None + config gaps → GAPS_FOUND (not a crash)."""
    s = build_run_summary({"AC": proj(fidelity_pct=None)}, cfg(3), [])
    assert s["verdict"] == "GAPS_FOUND"


# ── Advisory headlines: orphans and uncheckable comments ─────────────────────
# These are NOT verdict-changing — they surface advisory information only.

def test_orphans_surface_as_headline_without_changing_verdict():
    p = proj()                      # clean project
    p["stats"]["missing_in_src"] = 5
    s = build_run_summary({"AC": p}, cfg(), [])
    assert s["verdict"] == "CLEAN"                 # no source loss
    assert s["stats"]["orphans"] == 5
    assert any("target but not the source" in h for h in s["headlines"])
    assert not any(h == "Every audited issue and config object matched. "
                   "Clean migration." for h in s["headlines"])


def test_uncheckable_comments_surface_as_headline():
    p = proj()
    p["stats"]["comments_uncheckable"] = 12
    s = build_run_summary({"AC": p}, cfg(), [])
    assert s["stats"]["comments_uncheckable"] == 12
    assert any("could not be fully verified" in h for h in s["headlines"])


def test_uncheckable_attachments_surface_as_headline():
    """Confluence compare emits attachments_uncheckable (jira never does):
    advisory only — surfaced as a stat + headline, verdict untouched."""
    p = proj()
    p["stats"]["attachments_uncheckable"] = 3
    s = build_run_summary({"AC": p}, cfg(), [])
    assert s["verdict"] == "CLEAN"
    assert s["stats"]["attachments_uncheckable"] == 3
    assert any("attachment sets could not be fully verified" in h
               and "issue(s)" in h for h in s["headlines"])
    assert not any("Clean migration" in h for h in s["headlines"])
    # jira project stats lack the key entirely: aggregate must read 0, not crash.
    clean = build_run_summary({"AC": proj()}, cfg(), [])
    assert clean["stats"]["attachments_uncheckable"] == 0


# ── Product vocabulary: headlines template on the connector labels ───────────
# Stats KEYS stay issue-named (the UI relabels); only the prose adapts.

def test_headlines_use_item_label():
    p = proj(mismatched=3, tails=2)
    p["stats"]["missing_in_src"] = 1
    p["stats"]["comments_uncheckable"] = 4
    p["stats"]["attachments_uncheckable"] = 2
    s = build_run_summary({"DOCS": p}, cfg(), [],
                          item_label="page", container_label="space")
    joined = " ".join(s["headlines"])
    assert "page(s)" in joined
    assert "issue" not in joined.lower()
    # stats keys are the cross-product contract: unchanged by the labels
    assert s["stats"]["issues_with_mismatches"] == 3


def test_worst_container_headline_uses_both_labels():
    # proj() names the container AC in its stats block (the key the headline
    # reads); the dict key is only the aggregation index.
    s = build_run_summary({"AC": proj(missing=7)}, cfg(), [],
                          item_label="page", container_label="space")
    assert any("Space AC" in h and "7" in h and "pages" in h
               for h in s["headlines"])


def test_clean_fallback_uses_item_label():
    s = build_run_summary({"DOCS": proj()}, cfg(), [],
                          item_label="page", container_label="space")
    assert any("Every audited page" in h for h in s["headlines"])


# ── R4 honesty surfaced to the operator: skipped config areas ────────────────
# Areas with no DC API emit zero findings BY DESIGN, so without an explicit
# headline + stat a DC→Cloud run could read as a fully-clean config audit
# while five areas were never looked at.

def test_skipped_config_areas_surface_as_headline_and_stat():
    c = cfg()
    c["areas"] = {
        "statuses": {"label": "statuses", "src": 3, "tgt": 3,
                     "source_only": [], "target_only": []},
        "workflow_schemes": {"label": "workflow_schemes", "skipped": True,
                             "reason": "no Data Center API — verify manually"},
        "screen_schemes": {"label": "screen_schemes", "skipped": True,
                           "reason": "no Data Center API — verify manually"},
    }
    s = build_run_summary({"AC": proj()}, c, [])
    # capability honesty, not a gap: the verdict stays CLEAN…
    assert s["verdict"] == "CLEAN"
    assert s["stats"]["config_skipped"] == 2
    # …but the operator-facing headline must say the audit is partial, and
    # the unconditional clean-migration line must NOT appear.
    skipped_lines = [h for h in s["headlines"] if "skipped" in h]
    assert skipped_lines and "workflow_schemes" in skipped_lines[0]
    assert "screen_schemes" in skipped_lines[0]
    assert not any("Clean migration" in h for h in s["headlines"])


def test_no_skipped_areas_keeps_clean_headline():
    c = cfg()
    c["areas"] = {"statuses": {"label": "statuses", "src": 3, "tgt": 3}}
    s = build_run_summary({"AC": proj()}, c, [])
    assert s["stats"]["config_skipped"] == 0
    assert any("Clean migration" in h for h in s["headlines"])
