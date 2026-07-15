"""Title-keyed page comparison for one space (Confluence analog of compare.py).

Same return contract as compare_project — every jira stats key is present
(plus one documented advisory counter) and the finding kinds are shared — so
findings.py, aggregate.py, analysis.py and the UI consume either product
unchanged. Presence is split by a TIMESTAMP cutover line instead of key
numbers (R6): titles carry no sequence, but `created` is immutable and
preserved by a faithful migration, so the latest creation among COMMON pages
approximates when the migration snapshot was taken; a side-only page born
after that line is post-cutover drift, not loss. Inline child caps recorded
at extraction time downgrade attachment/comment checks to *_uncheckable
advisories — a truncated set must never be diffed as the whole truth.
"""
from __future__ import annotations

from collections import Counter

from ..compare import _load, _rekey_pairs, _cf_page


def _epoch(v) -> float | None:
    """Slim rows carry norm_ts output: epoch strings normally, but date-only
    or unparseable inputs pass through verbatim and absent reads as None.
    Anything non-numeric carries no cutover evidence, so it can never place
    a page after the line (it falls to genuine missing/extra)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


CONF_SPECS = [
    ("parent",  lambda f: f.get("parent"), "high"),
    ("created", lambda f: f.get("created"), "high"),
    ("creator", lambda f: f.get("creator"), "high"),
    ("version", lambda f: f.get("version"), "low"),
    ("labels",  lambda f: sorted(f.get("labels") or []), "med"),
]


def _att_set(att: dict) -> list[str]:
    return sorted(f"{a.get('filename')}|{a.get('size')}"
                  for a in (att.get("items") or []))


def _child_floor(d: dict, inline: int) -> int:
    """Lower bound on a capped side's REAL child count: the inline rows
    exist, and a declared envelope size larger than them is also a floor."""
    size = d.get("size")
    return max(inline, size) if isinstance(size, int) else inline


def compare_space(space: str, src_path: str, tgt_path: str,
                  cross_dialect: bool = False,
                  cutover_ts: str | float | None = None) -> dict:
    """cross_dialect is accepted for contract uniformity with
    compare_project; both Confluence deployments author body.storage XHTML,
    so stages never sets it — but a True passthrough still badges content
    findings, exactly like jira (Task 5 semantics).

    cutover_ts: operator-supplied cutover epoch. The INFERRED line
    (max created over common pages) cannot tell end-of-timeline loss from
    drift — a migration that dropped every page newer than the last
    migrated one places all the lost pages above the inferred line. An
    explicit timestamp pins the line to the known migration date and makes
    everything created before it a genuine hole."""
    # Confluence has no custom fields, so the header meta (cf_names) is ignored.
    (src, _), (tgt, _) = _load(src_path), _load(tgt_path)
    sk, tk = set(src), set(tgt)
    common = sorted(sk & tk)
    missing = sorted(sk - tk)
    extra = sorted(tk - sk)
    supplied_cut = _epoch(cutover_ts)
    if supplied_cut is not None:
        # External cutover evidence: applies even with no overlap.
        has_overlap = True
        cut = supplied_cut
    else:
        # A tail is only a defensible inference when the sides actually
        # overlap: no common titles = no cutover evidence, all genuine.
        has_overlap = bool(common)
        cut = max((e for k in common
                   if (e := _epoch(src[k].get("created"))) is not None),
                  default=None)

    # Re-key recovery (parity with the Jira path): pages can survive under a
    # renamed key. Pair unmatched source<->target 1-to-1 on a non-degenerate
    # content fingerprint (created + body sha); only matched pairs leave the
    # missing/extra sets, so unmatched pages stay reported as genuine loss. An
    # external cutover (supplied_cut) means missing IS real loss -> no pairing.
    rekey_pairs = ([] if supplied_cut is not None
                   else _rekey_pairs(missing, extra, src, tgt, _cf_page))
    if rekey_pairs:
        m_src = {a for a, _ in rekey_pairs}
        m_tgt = {b for _, b in rekey_pairs}
        missing = [k for k in missing if k not in m_src]
        extra = [k for k in extra if k not in m_tgt]
    rekey_suspected = bool(rekey_pairs)

    findings: list[dict] = []
    if rekey_suspected:
        findings.append({"project": space, "kind": "rekey_suspected",
                         "src_key": None, "tgt_key": None, "field": None,
                         "summary": f"{len(rekey_pairs)} page(s) matched across "
                                    f"renamed keys",
                         "detail": {"rekeyed": len(rekey_pairs),
                                    "still_missing": len(missing),
                                    "note": "these pages survived under new keys "
                                    "(a rename); their per-page fields were not "
                                    "compared — verify the pairing. Any remaining "
                                    "missing/extra below is genuine."}})
    # presence: source side
    for k in missing:
        e = _epoch(src[k].get("created"))
        if has_overlap and cut is not None and e is not None and e > cut:
            findings.append({"project": space, "kind": "tail_post_cutover",
                             "src_key": k, "tgt_key": None, "field": None,
                             "summary": k[:200],
                             "detail": {"direction": "source",
                                        "created": src[k].get("created"),
                                        "cutover_epoch": cut}})
        else:
            findings.append({"project": space, "kind": "missing_in_tgt",
                             "src_key": k, "tgt_key": None, "field": None,
                             "summary": k[:200],
                             "detail": {"created": src[k].get("created"),
                                        "below_cutover_line": True}})
    # presence: target side
    for k in extra:
        e = _epoch(tgt[k].get("created"))
        if has_overlap and cut is not None and e is not None and e > cut:
            findings.append({"project": space, "kind": "tail_post_cutover",
                             "src_key": None, "tgt_key": k, "field": None,
                             "summary": k[:200],
                             "detail": {"direction": "target",
                                        "created": tgt[k].get("created"),
                                        "cutover_epoch": cut}})
        else:
            findings.append({"project": space, "kind": "missing_in_src",
                             "src_key": None, "tgt_key": k, "field": None,
                             "summary": k[:200],
                             "detail": {"created": tgt[k].get("created")}})

    mismatch_page_keys: set = set()
    sev_count: Counter = Counter()
    field_counts: Counter = Counter()
    comments_uncheckable = 0
    attachments_uncheckable = 0
    creators: set = set()

    for k in common:
        fs, ft = src[k], tgt[k]
        # collision: same title, different PAGE. `created` is immutable and
        # creator never changes on a real page, so both differing together
        # means the title was reused for unrelated content — its field diffs
        # are meaningless noise and are skipped.
        if (fs.get("created") != ft.get("created")
                and fs.get("creator") != ft.get("creator")):
            mismatch_page_keys.add(k)   # the collision IS the mismatch
            findings.append({"project": space, "kind": "key_collision",
                             "src_key": k, "tgt_key": k, "field": None,
                             "summary": k[:200],
                             "detail": {"src_created": fs.get("created"),
                                        "tgt_created": ft.get("created"),
                                        "src_creator": fs.get("creator"),
                                        "tgt_creator": ft.get("creator")}})
            continue
        if fs.get("creator"):
            creators.add(fs["creator"])
        for name, fn, sev in CONF_SPECS:
            a, b = fn(fs), fn(ft)
            if a != b:
                mismatch_page_keys.add(k)
                sev_count[sev] += 1
                field_counts[name] += 1
                findings.append({"project": space, "kind": "field_mismatch",
                                 "src_key": k, "tgt_key": k, "field": name,
                                 "summary": f"{name} differs",
                                 "detail": {"src": a, "tgt": b, "sev": sev}})
        sb, tb = fs.get("body") or {}, ft.get("body") or {}
        if sb.get("sha") != tb.get("sha"):
            mismatch_page_keys.add(k)
            sev_count["high"] += 1
            detail = {"src_len": sb.get("len"), "tgt_len": tb.get("len"),
                      "sev": "high"}
            if cross_dialect:
                detail["cross_dialect"] = True
            findings.append({"project": space, "kind": "content_mismatch",
                             "src_key": k, "tgt_key": k, "field": "body",
                             "summary": "body content differs",
                             "detail": detail})
        # Macro TARGET fidelity: storage_text() strips macro parameters + ri:*
        # refs from the body sha, so a macro pointing at a different JQL /
        # included page / attachment / space fingerprints EQUAL in the body
        # (a false clean — the macro renders the wrong content or breaks after
        # migration). macro_sig captures exactly those stripped targets.
        ssig, tsig = fs.get("macro_sig"), ft.get("macro_sig")
        if ssig is not None and tsig is not None and ssig != tsig:
            mismatch_page_keys.add(k)
            sev_count["med"] += 1
            findings.append({"project": space, "kind": "macro_param_mismatch",
                             "src_key": k, "tgt_key": k, "field": "macros",
                             "summary": "macro target/parameters differ",
                             "detail": {"sev": "med",
                                        "note": "a macro's target (JQL, included "
                                        "page, attachment, or space reference) "
                                        "differs — the macro may render the "
                                        "wrong content or break after migration; "
                                        "verify the macro on the target page"}})
        # Attachments: a capped side means the inline item list is PARTIAL —
        # but partial data can still PROVE a mismatch: the complete side's
        # set is exact, so (a) a capped side's count floor exceeding the
        # complete side's total, or (b) an inline item on the capped side
        # absent from the complete set, is a real divergence. Downgrading a
        # provable mismatch to a coverage advisory reported real loss as
        # clean. Only the genuinely unprovable cases stay advisory.
        sa, ta = fs.get("attachment") or {}, ft.get("attachment") or {}
        sa_capped, ta_capped = bool(sa.get("capped")), bool(ta.get("capped"))
        if sa_capped or ta_capped:
            s_set, t_set = set(_att_set(sa)), set(_att_set(ta))
            s_n, t_n = len(sa.get("items") or []), len(ta.get("items") or [])
            proven_missing: list = []
            proven_extra: list = []
            count_proof = False
            if sa_capped and not ta_capped:
                proven_missing = sorted(s_set - t_set)  # inline src not in
                count_proof = _child_floor(sa, s_n) > t_n  # complete tgt
            elif ta_capped and not sa_capped:
                proven_extra = sorted(t_set - s_set)
                count_proof = _child_floor(ta, t_n) > s_n
            if proven_missing or proven_extra or count_proof:
                mismatch_page_keys.add(k)
                sev_count["high"] += 1
                findings.append({
                    "project": space, "kind": "attachment_mismatch",
                    "src_key": k, "tgt_key": k, "field": "attachment",
                    "summary": "attachments differ",
                    "detail": {"missing_in_tgt": proven_missing,
                               "extra_in_tgt": proven_extra,
                               "proven_from_partial": True,
                               "src_capped": sa_capped,
                               "tgt_capped": ta_capped,
                               "src_floor": _child_floor(sa, s_n),
                               "tgt_floor": _child_floor(ta, t_n),
                               "sev": "high"}})
            else:
                attachments_uncheckable += 1
                findings.append({
                    "project": space, "kind": "attachment_uncheckable",
                    "src_key": k, "tgt_key": k, "field": "attachment",
                    "summary": "attachment set not fully retrievable for "
                               "verification",
                    "detail": {"src_inline": s_n, "tgt_inline": t_n,
                               "src_capped": sa_capped,
                               "tgt_capped": ta_capped}})
        else:
            s_set, t_set = _att_set(sa), _att_set(ta)
            if s_set != t_set:
                mismatch_page_keys.add(k)
                sev_count["high"] += 1
                findings.append({
                    "project": space, "kind": "attachment_mismatch",
                    "src_key": k, "tgt_key": k, "field": "attachment",
                    "summary": "attachments differ",
                    "detail": {"missing_in_tgt": sorted(set(s_set) - set(t_set)),
                               "extra_in_tgt": sorted(set(t_set) - set(s_set)),
                               "sev": "high"}})
        # Comments: the count is len(inline results), so a capped side makes
        # it a FLOOR rather than a total. An uncapped side's count is exact,
        # so a capped floor exceeding it proves the counts differ; everything
        # else (equal floors, both sides capped) stays advisory, mirroring
        # jira's equal-totals-but-capped-inline behavior.
        sc, tc = fs.get("comment") or {}, ft.get("comment") or {}
        sc_capped, tc_capped = bool(sc.get("capped")), bool(tc.get("capped"))
        if sc_capped or tc_capped:
            s_floor = _child_floor(sc, sc.get("count") or 0)
            t_floor = _child_floor(tc, tc.get("count") or 0)
            proven = ((sc_capped and not tc_capped
                       and s_floor > (tc.get("count") or 0))
                      or (tc_capped and not sc_capped
                          and t_floor > (sc.get("count") or 0)))
            if proven:
                mismatch_page_keys.add(k)
                sev_count["high"] += 1
                findings.append({
                    "project": space, "kind": "comment_mismatch",
                    "src_key": k, "tgt_key": k, "field": "comment",
                    "summary": "comment fidelity differs",
                    "detail": {"src_total": sc.get("count"),
                               "tgt_total": tc.get("count"),
                               "src_floor": s_floor, "tgt_floor": t_floor,
                               "proven_from_partial": True,
                               "src_capped": sc_capped,
                               "tgt_capped": tc_capped,
                               "sev": "high"}})
            else:
                comments_uncheckable += 1
                findings.append({
                    "project": space, "kind": "comment_uncheckable",
                    "src_key": k, "tgt_key": k, "field": "comment",
                    "summary": "comment content not fully retrievable for "
                               "verification",
                    "detail": {"src_count": sc.get("count"),
                               "tgt_count": tc.get("count"),
                               "src_capped": sc_capped,
                               "tgt_capped": tc_capped}})
        elif sc.get("count") != tc.get("count"):
            mismatch_page_keys.add(k)
            sev_count["high"] += 1
            findings.append({"project": space, "kind": "comment_mismatch",
                             "src_key": k, "tgt_key": k, "field": "comment",
                             "summary": "comment fidelity differs",
                             "detail": {"src_total": sc.get("count"),
                                        "tgt_total": tc.get("count"),
                                        "sev": "high"}})

    holes = sum(1 for f in findings if f["kind"] == "missing_in_tgt")
    tails = sum(1 for f in findings if f["kind"] == "tail_post_cutover")
    clean_common = len(common) - len(mismatch_page_keys)
    # No common pages = nothing compared; None (json null) forces N/A, never
    # a flattering 100% against an empty or re-titled target.
    fidelity = round(100.0 * clean_common / len(common), 2) if common else None
    stats = {
        "project": space, "src": len(sk), "tgt": len(tk),
        "common": len(common), "missing_in_tgt": holes,
        "missing_in_src": sum(1 for f in findings
                              if f["kind"] == "missing_in_src"),
        "tails": tails, "collisions": sum(1 for f in findings
                                          if f["kind"] == "key_collision"),
        "issues_with_mismatches": len(mismatch_page_keys),
        "comments_uncheckable": comments_uncheckable,
        "rekey_suspected": rekey_suspected,
        # Jira custom-field value-comparison counters: Confluence has no custom
        # fields, so they are always zero — present so jira-shaped consumers
        # (findings/aggregate/analysis) read them by name without a KeyError.
        "cf_value_mismatches": 0,
        "cf_fields_absent_in_tgt": 0,
        "cf_ambiguous": 0,
        # The one key jira compare does not emit: a per-page attachment list
        # is always complete on jira, only Confluence's inline expansion can
        # overflow. Additive and advisory — every consumer reads stats by
        # name, so jira-shaped readers are unaffected.
        "attachments_uncheckable": attachments_uncheckable,
        "fidelity_pct": fidelity,
        "severity_totals": dict(sev_count),
        "field_mismatch_counts": dict(field_counts),
        # jira-only concepts keep their empty shapes so consumers never
        # KeyError: Confluence has no status/type remaps and creator
        # displayNames alone cannot prove an unmapped account.
        "remap": {},
        "unmapped_users": [],
        "distinct_src_people": len(creators),
    }
    return {"stats": stats, "findings": findings}
