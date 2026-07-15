"""Run-level summary: aggregate stats, verdict, and prose headlines.

Verdict ladder (worst wins):
  CRITICAL          genuine holes (missing below the cutover line), key
                    collisions, or unresolved permission blind spots —
                    the audit cannot be called clean.
  GAPS_FOUND        field/content mismatches or config objects missing in
                    the target, OR any config area that could not be read
                    on either side (area_error). Data may be present but
                    is not fully verified / faithful.
  CLEAN_WITH_TAILS  only post-cutover drift (expected when a source stays
                    live after the snapshot).
  CLEAN             nothing found.

NOTE: fidelity_pct in project stats can be None (compare returns None when
no issues were compared — e.g. an empty project).  This function never
treats fidelity_pct as a number; the verdict is driven entirely by
countable finding fields.
"""
from __future__ import annotations


def build_run_summary(project_results: dict, config_result: dict,
                      blind_spots: list, item_label: str = "issue",
                      container_label: str = "project") -> dict:
    # The labels are prose-only (spec §4.8): a confluence run reads page/space
    # in every headline while the stats KEYS stay issue-named — they are the
    # cross-product contract every consumer reads by name, and the UI relabels.
    stats_list = [r["stats"] for r in project_results.values()]

    # Issue-level aggregates — fidelity_pct is intentionally NOT read here
    # because it can be None (compare found nothing to compare).
    holes = sum(s.get("missing_in_tgt", 0) for s in stats_list)
    tails = sum(s.get("tails", 0) for s in stats_list)
    collisions = sum(s.get("collisions", 0) for s in stats_list)
    mismatched = sum(s.get("issues_with_mismatches", 0) for s in stats_list)
    # Advisory-only: do NOT enter the verdict ladder. attachments_uncheckable
    # exists only in confluence stats (a jira attachment list is always
    # complete inline) — .get keeps jira aggregation key-error free.
    orphans = sum(s.get("missing_in_src", 0) for s in stats_list)
    # Re-key suspected: a side pairing with no shared keys but comparable
    # populations could not be matched by key. NOT clean (we couldn't verify) and
    # NOT genuine loss (probably a rename) -> it belongs at the GAPS_FOUND tier.
    rekey = [s["project"] for s in stats_list if s.get("rekey_suspected")]
    comments_uncheckable = sum(s.get("comments_uncheckable", 0) for s in stats_list)
    attachments_uncheckable = sum(
        s.get("attachments_uncheckable", 0) for s in stats_list)

    # Config-level aggregates.
    # area_error is broken out explicitly (Amendment 1): it means a side was
    # unreachable, so the audit is provably incomplete for that area.
    # It also counts toward cfg_other (kind != missing_in_tgt), which already
    # triggers GAPS_FOUND — the explicit count and headline make it visible.
    findings = config_result.get("findings", [])
    cfg_missing = sum(1 for f in findings if f["kind"] == "missing_in_tgt")
    area_errors = sum(1 for f in findings if f["kind"] == "area_error")
    cfg_other = sum(1 for f in findings if f["kind"] != "missing_in_tgt")
    # cfg_other >= area_errors always (area_error is a subset of cfg_other).

    # R4: areas with no API on a side are SKIPPED — they emit zero findings
    # by design, so they must be surfaced here or a DC→Cloud run with no
    # other config findings reads as a fully-audited clean config. Skipped
    # is capability honesty, not a gap: it gets a headline and a stat but
    # does NOT enter the verdict ladder.
    skipped_areas = sorted(
        name for name, a in (config_result.get("areas") or {}).items()
        if isinstance(a, dict) and a.get("skipped"))

    live_blind = [b for b in blind_spots if b.get("blind_spot")]

    # ── Verdict ladder ────────────────────────────────────────────────────────
    if holes or collisions or live_blind:
        verdict = "CRITICAL"
    elif mismatched or cfg_missing or cfg_other or rekey:
        # cfg_other includes area_error, so any area_error → GAPS_FOUND.
        verdict = "GAPS_FOUND"
    elif tails:
        verdict = "CLEAN_WITH_TAILS"
    else:
        verdict = "CLEAN"

    # ── Prose headlines ───────────────────────────────────────────────────────
    headlines: list[str] = []

    for b in live_blind:
        headlines.append(
            f"Permission blind spot on {b['key']}: search sees "
            f"{b.get('search_count')} of {b.get('insight_count')} "
            f"{item_label}s. "
            f"Counts below it are NOT trustworthy until access is fixed.")

    worst = sorted(stats_list, key=lambda s: -s.get("missing_in_tgt", 0))
    if worst and worst[0].get("missing_in_tgt"):
        w = worst[0]
        # stats carry the container key under "project" for every product —
        # part of the issue-named key contract above.
        headlines.append(
            f"{container_label.capitalize()} {w['project']} has "
            f"{w['missing_in_tgt']} {item_label}s missing in the "
            f"target below the cutover line. This is genuine data loss until "
            f"proven otherwise.")

    for proj in rekey:
        headlines.append(
            f"{container_label.capitalize()} {proj}: source and target share no "
            f"{item_label} keys — likely a project-key rename or a wrong "
            f"source/target pairing. Per-{item_label} fidelity could not be "
            f"computed; verify the key mapping.")

    if collisions:
        headlines.append(
            f"{collisions} key collision(s): same key, different "
            f"{item_label} identity on each side. Treat matched-field stats "
            f"for those keys as noise.")

    if tails and not holes:
        headlines.append(
            f"{tails} {item_label}(s) exist only as post-cutover tail "
            f"(created after the snapshot). Expected drift, not loss.")

    if mismatched:
        headlines.append(
            f"{mismatched} migrated {item_label}(s) have at least one field "
            f"or content difference.")

    if cfg_missing:
        headlines.append(
            f"{cfg_missing} config object(s) from the source are missing in "
            f"the target (statuses, fields, screens, schemes or JSM objects).")

    # Amendment 1: area_error headline — loud incompleteness signal.
    if area_errors:
        headlines.append(
            f"{area_errors} config area(s) could not be read on the "
            f"source/target — results are incomplete. Review access before "
            f"treating this audit as authoritative.")

    # Advisory headlines — appended BEFORE the clean-migration fallback so a
    # run with only orphans/uncheckable no longer prints "Clean migration."
    if orphans:
        headlines.append(
            f"{orphans} {item_label}(s) exist on the target but not the "
            f"source (over-migration or target-side edits). Not data loss, "
            f"but worth confirming.")

    if comments_uncheckable:
        headlines.append(
            f"{comments_uncheckable} {item_label}(s) had more comments than "
            f"the API returns inline; their comment content could not be "
            f"fully verified.")

    if attachments_uncheckable:
        headlines.append(
            f"{attachments_uncheckable} {item_label}(s) had more attachments "
            f"than the API returns inline; their attachment sets could not "
            f"be fully verified.")

    if skipped_areas:
        headlines.append(
            f"{len(skipped_areas)} config area(s) were skipped (no API on "
            f"this deployment) and are NOT covered by this verdict: "
            f"{', '.join(skipped_areas)}. Verify them manually.")

    if not headlines:
        headlines.append(f"Every audited {item_label} and config object "
                         f"matched. Clean migration.")

    return {
        "stats": {
            "projects": len(stats_list),
            "issues_src_total": sum(s.get("src", 0) for s in stats_list),
            "issues_tgt_total": sum(s.get("tgt", 0) for s in stats_list),
            "holes": holes, "tails": tails, "collisions": collisions,
            "issues_with_mismatches": mismatched,
            "orphans": orphans,                    # advisory: target-only
            "comments_uncheckable": comments_uncheckable,  # advisory
            "attachments_uncheckable": attachments_uncheckable,  # advisory
            "config_missing": cfg_missing,
            "area_errors": area_errors,   # Amendment 1: explicit, not folded
            "config_other": cfg_other,
            "config_skipped": len(skipped_areas),  # R4: unaudited, not clean
            "blind_spots": len(live_blind),
        },
        "verdict": verdict,
        "headlines": headlines,
    }
