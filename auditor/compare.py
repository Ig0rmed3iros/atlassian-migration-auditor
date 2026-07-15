"""Field-by-field migration comparison for one project (port of compare.py).

No sampling: every common issue is compared on the SPECS ledger; presence is
split by the cutover line (missing keys numbered ABOVE the other side's max
key are post-cutover drift, not loss). Emits spec-shaped finding dicts plus a
stats block; persistence is the caller's job.
"""
from __future__ import annotations

import gzip
import json
from collections import Counter

from .cfvalues import SENSITIVE_KINDS
from .textnorm import content_fp

# sha of empty/None content — a universal collision, never a reliable identity
# for re-key matching (every issue with no description/body shares it).
_EMPTY_CONTENT_SHA = content_fp("")


def _cf_issue(f):
    """Immutable content fingerprint of a Jira issue's fields (re-key identity)."""
    return (f.get("created"), (f.get("description") or {}).get("sha"))


def _cf_page(f):
    """Immutable content fingerprint of a Confluence page's fields."""
    return (f.get("created"), (f.get("body") or {}).get("sha"))


def _rekey_pairs(missing, extra, src, tgt, fp_fn):
    """1-to-1 content-fingerprint matches between unmatched source and target
    keys (the SAME content under a renamed key). Excludes degenerate fps (None
    created or empty/missing content sha) and AMBIGUOUS matches (more than one
    candidate), so identical or empty issues never collide into a false pair.
    Returns [(src_key, tgt_key), ...]; only these leave the missing/extra sets,
    so every unmatched issue stays reported as genuine loss."""
    def _degenerate(fp):
        return fp[0] is None or not fp[1] or fp[1] == _EMPTY_CONTENT_SHA
    tgt_by_fp: dict = {}
    for k in extra:
        fp = fp_fn(tgt[k])
        if not _degenerate(fp):
            tgt_by_fp.setdefault(fp, []).append(k)
    pairs, used = [], set()
    for k in missing:
        fp = fp_fn(src[k])
        if _degenerate(fp):
            continue
        cands = [t for t in tgt_by_fp.get(fp, []) if t not in used]
        if len(cands) == 1:          # unambiguous same-content match only
            pairs.append((k, cands[0]))
            used.add(cands[0])
    return pairs


def _load(path: str) -> tuple[dict, dict]:
    """Returns (issues_by_key, header_meta). The header carries the custom-field
    inventory (cf_names / cf_ambiguous) so the comparator can tell a field that
    is ABSENT on an instance from one merely empty on an issue."""
    d, meta = {}, {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if "_extract_format" in r:   # format stamp header, not an issue
                meta = r
                continue
            d[r["key"]] = r["fields"]
    return d, meta


def _nm(o):
    if isinstance(o, dict):
        return o.get("name") or o.get("value")
    return o


def _person(o):
    return o.get("displayName") if isinstance(o, dict) else None


def _nameset(lst):
    return sorted([(x.get("name") if isinstance(x, dict) else x)
                   for x in (lst or [])])


def _num(k: str) -> int:
    try:
        return int(k.split("-")[-1])
    except (ValueError, AttributeError):
        return -1


SPECS = [
    ("summary",        lambda f: f.get("summary"), "high"),
    ("issuetype",      lambda f: _nm(f.get("issuetype")), "high"),
    ("status",         lambda f: _nm(f.get("status")), "high"),
    ("priority",       lambda f: _nm(f.get("priority")), "med"),
    ("resolution",     lambda f: _nm(f.get("resolution")), "high"),
    ("resolutiondate", lambda f: f.get("resolutiondate"), "med"),
    ("created",        lambda f: f.get("created"), "high"),
    ("duedate",        lambda f: f.get("duedate"), "med"),
    ("labels",         lambda f: sorted(f.get("labels") or []), "med"),
    ("components",     lambda f: _nameset(f.get("components")), "med"),
    ("fixVersions",    lambda f: _nameset(f.get("fixVersions")), "med"),
    ("versions",       lambda f: _nameset(f.get("versions")), "med"),
    ("parent",         lambda f: (f.get("parent") or {}).get("key"), "high"),
    ("environment",    lambda f: (f.get("environment") or {}).get("sha"), "med"),
    ("security",       lambda f: _nm(f.get("security")), "high"),
    ("assignee",       lambda f: _person(f.get("assignee")), "high"),
    ("reporter",       lambda f: _person(f.get("reporter")), "high"),
    ("creator",        lambda f: _person(f.get("creator")), "med"),
    ("worklog_total",  lambda f: (f.get("worklog") or {}).get("total"), "med"),
    ("votes",          lambda f: (f.get("votes") or {}).get("votes"), "low"),
    ("watches",        lambda f: (f.get("watches") or {}).get("watchCount"), "low"),
    ("issuelinks",     lambda f: sorted(
        f"{l.get('type')}|{l.get('inward')}|{l.get('outward')}"
        for l in (f.get("issuelinks") or [])), "med"),
]
_REMAP_FIELDS = ("status", "issuetype", "priority")
_FORMER = {"Former user", "Former User"}


def _desc_sha(f):
    return (f.get("description") or {}).get("sha")


def _att_set(f):
    return sorted(f"{a.get('filename')}|{a.get('size')}"
                  for a in (f.get("attachment") or []))


def _cf_finding(project, key, name, kind, summary, cf_kind, extra):
    """One custom-field value finding. Identity/representation-sensitive types
    (user/group pickers, cascading selects, app-provided, rich text) cannot be
    normalized reliably across instances, so they carry verify_sensitive so a
    reader treats the mismatch as 'verify' rather than certain loss — the same
    contract as the cross_dialect badge on bodies."""
    detail = {"sev": "med", "cf_kind": cf_kind}
    detail.update(extra)
    if cf_kind in SENSITIVE_KINDS:
        detail["verify_sensitive"] = True
        detail["verify_reason"] = (
            f"the '{cf_kind}' field type cannot be normalized reliably across "
            "instances (account remap / option structure / wiki-vs-ADF) — "
            "verify before treating as data loss")
    return {"project": project, "kind": kind, "src_key": key, "tgt_key": key,
            "field": name, "summary": summary, "detail": detail}


def compare_project(project: str, src_path: str, tgt_path: str,
                    cross_dialect: bool = False) -> dict:
    """cross_dialect: set when the two sides store bodies in different
    representations (DC wiki-markup vs Cloud ADF). Canonicalization makes
    same-prose bodies fingerprint-equal, but macro-heavy content can still
    drift structurally, so content/comment mismatch details carry
    cross_dialect=True to let readers badge them as representation-sensitive
    rather than certain data loss."""
    (src, src_meta), (tgt, tgt_meta) = _load(src_path), _load(tgt_path)
    # Custom-field name universes: authoritative from the header inventory, with
    # a fallback to the union of seen values for legacy/headerless extracts.
    src_cf_names = set(src_meta.get("cf_names") or
                       {n for f in src.values() for n in (f.get("_cf") or {})})
    tgt_cf_names = set(tgt_meta.get("cf_names") or
                       {n for f in tgt.values() for n in (f.get("_cf") or {})})
    cf_ambiguous = sorted(set(src_meta.get("cf_ambiguous") or [])
                          | set(tgt_meta.get("cf_ambiguous") or []))
    sk, tk = set(src), set(tgt)
    common = sorted(sk & tk, key=_num)
    missing = sorted(sk - tk, key=_num)
    extra = sorted(tk - sk, key=_num)
    tgt_max = max((_num(k) for k in tk), default=-1)
    src_max = max((_num(k) for k in sk), default=-1)
    # A tail is only a defensible inference when the two sides actually
    # overlap. With no common keys there is no evidence of a real cutover,
    # so every source-missing key is genuine loss and every target-extra key
    # is genuine extra (re-keyed or unrelated target).
    has_overlap = bool(common)

    # Re-key recovery: the SAME issue can survive under a NEW key (a project-key
    # rename), which key-only matching would mis-report as loss+extra. Pair the
    # unmatched source<->target issues 1-to-1 on an IMMUTABLE, NON-DEGENERATE
    # content fingerprint (created + body sha). Only UNAMBIGUOUS pairs (exactly
    # one candidate) and non-empty content count, so identical/empty issues never
    # collide into a false pair. Crucially, only the matched pairs leave the
    # missing/extra sets — every UNMATCHED issue stays reported as genuine loss,
    # so a partial loss is never hidden (no-bias review: the prior 'suppress all'
    # version hid real data loss).
    rekey_pairs = _rekey_pairs(missing, extra, src, tgt, _cf_issue)
    if rekey_pairs:
        m_src = {a for a, _ in rekey_pairs}
        m_tgt = {b for _, b in rekey_pairs}
        missing = [k for k in missing if k not in m_src]
        extra = [k for k in extra if k not in m_tgt]
    rekey_suspected = bool(rekey_pairs)

    findings: list[dict] = []
    if rekey_suspected:
        findings.append({"project": project, "kind": "rekey_suspected",
                         "src_key": None, "tgt_key": None, "field": None,
                         "summary": f"{len(rekey_pairs)} issue(s) matched across "
                                    f"renamed keys",
                         "detail": {"rekeyed": len(rekey_pairs),
                                    "still_missing": len(missing),
                                    "note": "these issues survived under new keys "
                                    "(a project-key rename); their per-issue "
                                    "fields were not compared — verify the key "
                                    "mapping. Any remaining missing/extra below "
                                    "is genuine."}})
    # presence: source side
    for k in missing:
        if has_overlap and _num(k) > tgt_max:
            findings.append({"project": project, "kind": "tail_post_cutover",
                             "src_key": k, "tgt_key": None, "field": None,
                             "summary": (src[k].get("summary") or "")[:200],
                             "detail": {"direction": "source",
                                        "created": src[k].get("created"),
                                        "cutover_max_key": tgt_max}})
        else:
            findings.append({"project": project, "kind": "missing_in_tgt",
                             "src_key": k, "tgt_key": None, "field": None,
                             "summary": (src[k].get("summary") or "")[:200],
                             "detail": {"created": src[k].get("created"),
                                        "below_cutover_line": True}})
    # presence: target side
    for k in extra:
        if has_overlap and _num(k) > src_max:
            findings.append({"project": project, "kind": "tail_post_cutover",
                             "src_key": None, "tgt_key": k, "field": None,
                             "summary": (tgt[k].get("summary") or "")[:200],
                             "detail": {"direction": "target",
                                        "created": tgt[k].get("created"),
                                        "cutover_max_key": src_max}})
        else:
            findings.append({"project": project, "kind": "missing_in_src",
                             "src_key": None, "tgt_key": k, "field": None,
                             "summary": (tgt[k].get("summary") or "")[:200],
                             "detail": {"created": tgt[k].get("created")}})

    remap = {f: Counter() for f in _REMAP_FIELDS}
    user_pairs: Counter = Counter()
    unmapped: Counter = Counter()
    mismatch_issue_keys: set = set()
    sev_count: Counter = Counter()
    field_counts: Counter = Counter()
    comments_uncheckable = 0
    cf_field_lost: Counter = Counter()   # src field absent on tgt -> #issues
    cf_kind_divergent: set = set()       # same name, different type per side
    cf_value_mismatches = 0

    for k in common:
        fs, ft = src[k], tgt[k]
        # collision: same key, different ISSUE. `created` is immutable in
        # normal Jira operation and is preserved by a faithful migration, so a
        # changed `created` is the load-bearing signal. reporter/summary are
        # legitimately mutable (renames, edits), so they can only corroborate
        # the invariant, never out-vote it: a collision fires only when
        # `created` differs AND at least one of reporter/summary also differs.
        created_diff = fs.get("created") != ft.get("created")
        corroborated = (
            _person(fs.get("reporter")) != _person(ft.get("reporter"))
            or fs.get("summary") != ft.get("summary"))
        if created_diff and corroborated:
            mismatch_issue_keys.add(k)   # the collision IS the strongest mismatch
            findings.append({"project": project, "kind": "key_collision",
                             "src_key": k, "tgt_key": k, "field": None,
                             "summary": (fs.get("summary") or "")[:200],
                             "detail": {"src_created": fs.get("created"),
                                        "tgt_created": ft.get("created"),
                                        "src_reporter": _person(fs.get("reporter")),
                                        "tgt_reporter": _person(ft.get("reporter"))}})
            continue   # a collided pair's field diffs are meaningless noise
        for name, fn, sev in SPECS:
            a, b = fn(fs), fn(ft)
            if a != b:
                mismatch_issue_keys.add(k)
                sev_count[sev] += 1
                field_counts[name] += 1
                kind = "link_mismatch" if name == "issuelinks" else "field_mismatch"
                findings.append({"project": project, "kind": kind,
                                 "src_key": k, "tgt_key": k, "field": name,
                                 "summary": f"{name} differs",
                                 "detail": {"src": a, "tgt": b, "sev": sev}})
                if name in remap:
                    remap[name][(a, b)] += 1
        if _desc_sha(fs) != _desc_sha(ft):
            mismatch_issue_keys.add(k)
            sev_count["high"] += 1
            detail = {"src_len": (fs.get("description") or {}).get("len"),
                      "tgt_len": (ft.get("description") or {}).get("len"),
                      "sev": "high"}
            if cross_dialect:
                detail["cross_dialect"] = True
            findings.append({"project": project, "kind": "content_mismatch",
                             "src_key": k, "tgt_key": k, "field": "description",
                             "summary": "description content differs",
                             "detail": detail})
        for role in ("assignee", "reporter", "creator"):
            a, b = _person(fs.get(role)), _person(ft.get(role))
            if a:
                user_pairs[(a, b)] += 1
                if b is None or b in _FORMER:
                    unmapped[a] += 1
        sc, tc = fs.get("comment") or {}, ft.get("comment") or {}
        cm_detail = None
        if sc.get("total") != tc.get("total"):
            cm_detail = {"src_total": sc.get("total"), "tgt_total": tc.get("total")}
        else:
            full = (sc.get("total") == sc.get("inline")
                    and tc.get("total") == tc.get("inline"))
            if full and (sc.get("total") or 0) > 0:
                s_sha = sorted(i["sha"] for i in sc.get("items", []))
                t_sha = sorted(i["sha"] for i in tc.get("items", []))
                if s_sha != t_sha:
                    cm_detail = {"content_differs": True,
                                 "total": sc.get("total")}
            elif ((sc.get("total") or 0) > (sc.get("inline") or 0)
                  or (tc.get("total") or 0) > (tc.get("inline") or 0)):
                # Counts MATCH but the API capped how many comments were
                # captured inline: content cannot be fully verified. This is a
                # coverage gap, NOT a mismatch — it does not dent fidelity.
                comments_uncheckable += 1
                findings.append({
                    "project": project, "kind": "comment_uncheckable",
                    "src_key": k, "tgt_key": k, "field": "comment",
                    "summary": "comment content not fully retrievable for "
                               "verification",
                    "detail": {"total": sc.get("total"),
                               "src_inline": sc.get("inline"),
                               "tgt_inline": tc.get("inline")}})
        if cm_detail:
            mismatch_issue_keys.add(k)
            sev_count["high"] += 1
            cm_detail["sev"] = "high"
            if cross_dialect:
                cm_detail["cross_dialect"] = True
            findings.append({"project": project, "kind": "comment_mismatch",
                             "src_key": k, "tgt_key": k, "field": "comment",
                             "summary": "comment fidelity differs",
                             "detail": cm_detail})
        sa, ta = _att_set(fs), _att_set(ft)
        if sa != ta:
            mismatch_issue_keys.add(k)
            sev_count["high"] += 1
            findings.append({"project": project, "kind": "attachment_mismatch",
                             "src_key": k, "tgt_key": k, "field": "attachment",
                             "summary": "attachments differ",
                             "detail": {"missing_in_tgt": sorted(set(sa) - set(ta)),
                                        "extra_in_tgt": sorted(set(ta) - set(sa)),
                                        "sev": "high"}})
        # ---- custom-field VALUE comparison (matched by NAME) -----------------
        scf, tcf = fs.get("_cf") or {}, ft.get("_cf") or {}
        for name in set(scf) | set(tcf):
            sv, tv = scf.get(name), tcf.get(name)
            if sv and tv:
                if sv["kind"] != tv["kind"]:
                    # The same field NAME normalized to DIFFERENT type-kinds on
                    # the two instances (e.g. one side's /field lacked a schema
                    # and fell to the app path). That is a cross-instance schema
                    # divergence, not a value mismatch — disclose, never dent.
                    cf_kind_divergent.add(name)
                elif sv["fp"] != tv["fp"]:
                    cf_value_mismatches += 1
                    mismatch_issue_keys.add(k)
                    sev_count["med"] += 1
                    findings.append(_cf_finding(
                        project, k, name, "cf_value_mismatch",
                        f"custom field {name} value differs", sv["kind"],
                        {"note": "value differs between source and target"}))
            elif sv and not tv:
                if name in tgt_cf_names:
                    # field exists on the target instance but this issue lost
                    # its value -> per-issue value loss.
                    extra = {"note": "value present in source, empty in target"}
                else:
                    # field absent on the target INSTANCE -> genuine loss for
                    # this issue (also rolled up to a project-level finding).
                    # Marked verify_sensitive: a missing field is often a rename.
                    cf_field_lost[name] += 1
                    extra = {"note": "field absent on the target instance — "
                                     "value lost",
                             "verify_sensitive": True,
                             "verify_reason": "the field is absent on the "
                             "target — this may be a rename; verify the field "
                             "mapping before treating it as data loss"}
                # Emit a PER-ISSUE finding either way so the loss flows through
                # derive_fidelity into the DISPLAYED fidelity (a null-keyed
                # rollup alone would leave the headline falsely at 100%).
                cf_value_mismatches += 1
                mismatch_issue_keys.add(k)
                sev_count["high"] += 1
                findings.append(_cf_finding(
                    project, k, name, "cf_value_mismatch",
                    f"custom field {name} value missing in target",
                    sv["kind"], extra))
            # tv and not sv with the field present on src = data ADDED on the
            # target (migration default / post-cutover edit), not loss -> no dent.

    # ---- project-level custom-field disclosures --------------------------
    # A field absent on the target instance loses every source value it held;
    # rolled up to one finding per field (the per-issue dent already happened).
    for name, cnt in cf_field_lost.most_common():
        findings.append({
            "project": project, "kind": "cf_field_not_in_target",
            "src_key": None, "tgt_key": None, "field": name,
            "summary": f"custom field {name} absent on the target instance",
            "detail": {"sev": "high", "affected_issues": cnt,
                       "verify_sensitive": True,
                       "note": "the field does not exist on the target instance; "
                               "every source value for it is lost — or the field "
                               "was renamed (verify the field mapping)"}})
    # Fields whose NAME is duplicated on an instance can't be matched 1-to-1
    # across sides, so they are disclosed rather than silently mis-compared.
    for name in cf_ambiguous:
        findings.append({
            "project": project, "kind": "cf_value_not_compared",
            "src_key": None, "tgt_key": None, "field": name,
            "summary": f"custom field {name} not value-compared (ambiguous name)",
            "detail": {"sev": "info",
                       "note": "more than one custom field shares this name; its "
                               "values cannot be matched across instances"}})
    # Fields whose type normalized differently on each instance: a schema
    # divergence (often one side's /field lacked a usable schema), not a value
    # diff — disclosed so it is verified, never scored as loss.
    for name in sorted(cf_kind_divergent):
        findings.append({
            "project": project, "kind": "cf_value_not_compared",
            "src_key": None, "tgt_key": None, "field": name,
            "summary": f"custom field {name} not value-compared (type differs "
                       "across instances)",
            "detail": {"sev": "info",
                       "note": "this field's type normalized differently on the "
                               "two instances; its values cannot be compared "
                               "reliably — verify the field schema mapping"}})

    holes = sum(1 for f in findings if f["kind"] == "missing_in_tgt")
    tails = sum(1 for f in findings if f["kind"] == "tail_post_cutover")
    clean_common = len(common) - len(mismatch_issue_keys)
    # No common issues = nothing was actually compared. Reporting 100% would
    # be a lie of omission (empty/re-keyed target reads as a perfect match),
    # so fidelity is None (json null) to force the caller to treat it as N/A.
    fidelity = round(100.0 * clean_common / len(common), 2) if common else None
    stats = {
        "project": project, "src": len(sk), "tgt": len(tk),
        "common": len(common), "missing_in_tgt": holes,
        "missing_in_src": sum(1 for f in findings if f["kind"] == "missing_in_src"),
        "tails": tails, "collisions": sum(1 for f in findings
                                          if f["kind"] == "key_collision"),
        "issues_with_mismatches": len(mismatch_issue_keys),
        "comments_uncheckable": comments_uncheckable,
        "rekey_suspected": rekey_suspected,
        "cf_value_mismatches": cf_value_mismatches,
        "cf_fields_absent_in_tgt": len(cf_field_lost),
        "cf_ambiguous": len(cf_ambiguous),
        "fidelity_pct": fidelity,
        "severity_totals": dict(sev_count),
        "field_mismatch_counts": dict(field_counts),
        "remap": {f: [{"src": s, "tgt": t, "count": c}
                      for (s, t), c in cnt.most_common(40)]
                  for f, cnt in remap.items() if cnt},
        "unmapped_users": [{"src": u, "occurrences": c}
                           for u, c in unmapped.most_common(60)],
        "distinct_src_people": len({a for (a, _) in user_pairs}),
    }
    return {"stats": stats, "findings": findings}
