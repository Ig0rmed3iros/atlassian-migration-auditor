"""Deterministic content-complete extraction (port of extract_core.py).

Every issue of a project, both audit-critical fields and content fingerprints
(description/comments reduced to sha16 + length so megabytes of prose become
comparable rows). Output: gzip JSONL, one slim issue per line, key-ordered.
Count-verified against approximate-count so a silent pagination gap can never
masquerade as a clean extraction.

Dialect-aware: Cloud bodies are ADF trees, DC bodies wiki-markup strings.
Shas are ALWAYS content_fp (the canonical fingerprint, spec §4.3) so the same
authored prose hashes equal across a DC→Cloud pair; len/head stay readable
display text. Timestamps pass through norm_ts for the same reason: DC's
+0000 and Cloud's +00:00 spellings of one instant must not read as drift.

Writes via temp-file + atomic rename, and commits ONLY when the extract is not
known-truncated: a mid-extraction crash leaves an orphaned .tmp, and a clean
run that paginated short of approximate-count is discarded the same way — so a
cached-extract reuse can never pick up a truncated file.
"""
from __future__ import annotations

import gzip
import json
import os
from collections import Counter
from typing import Callable

from .cfvalues import normalize_cf
from .client import JiraClient, escape_query_key
from .textnorm import adf_text, canon, content_fp, norm_ts, wiki_text

def _extract_page() -> int:
    """Issues per search page. Default 100 (the safe Jira Cloud /search/jql
    value; DC tolerates it). Fewer pages = fewer round-trips = less rate-limit
    exposure. Override with MA_EXTRACT_PAGE; clamped to >= 1."""
    raw = os.environ.get("MA_EXTRACT_PAGE")
    if raw is None:
        return 100
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 100
    return max(1, n)


# Version stamp of the .core.jsonl.gz workspace files (first line:
# {"_extract_format": N}). Bump it whenever slim()'s output stops being
# comparable with what an older build wrote — the run engine refuses to
# reuse a cached extract whose stamp is not current, because mixing formats
# across sides flags every common issue as drifted (confidently wrong).
#   1 = unstamped legacy files: display-text h16 shas, raw ISO timestamps.
#   2 = canonical content_fp shas + norm_ts epoch timestamps (spec §4.3).
#   3 = environment presence keyed on canonical text (not display text);
#       DC epic link folded into parent; confluence comment/attachment caps
#       carry the declared child size (floor for partial-data proofs).
#   4 = per-issue custom-field VALUE fingerprints (fields._cf: name -> {fp,kind},
#       type-normalized + matched by name), raw customfield_* values dropped;
#       header carries the cf_names inventory + cf_ambiguous (duplicate names).
#   5 = Confluence slim_page carries a macro_sig (macro-parameter + ri-ref
#       fingerprint) so a macro pointing at the wrong target is no longer a
#       false clean in the body sha.
#   6 = Confluence extract covers BLOG POSTS too (slim_page carries content_type;
#       blog rows are key-namespaced "[blog] <title>") — a migration that drops
#       or breaks blogs is no longer a false clean.
EXTRACT_FORMAT = 6


def extract_format(path: str) -> int:
    """Format stamp of a cached extract; 1 for unstamped legacy files,
    0 for unreadable/empty files (never reusable)."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            first = fh.readline()
        row = json.loads(first)
    except (OSError, ValueError):
        return 0
    if isinstance(row, dict) and "_extract_format" in row:
        return int(row["_extract_format"])
    return 1


CORE_FIELDS = [
    "summary", "description", "issuetype", "status", "statuscategorychangedate",
    "priority", "resolution", "resolutiondate", "assignee", "reporter", "creator",
    "created", "updated", "duedate", "labels", "components", "fixVersions",
    "versions", "parent", "issuelinks", "subtasks", "comment", "attachment",
    "votes", "watches", "timetracking", "environment", "security", "worklog",
]


_TS_FIELDS = ("created", "updated", "resolutiondate", "statuscategorychangedate")


def _texts(body, dialect: str) -> tuple[str, str]:
    """One body field → (display text, canon input). Display keeps mentions
    and markup readable for len/head; canon input feeds content_fp so the
    same prose hashes equal across dialects (the fingerprint firewall)."""
    if dialect == "wiki":
        s = body or ""
        return s, wiki_text(s)
    return adf_text(body), adf_text(body, for_canon=True)


def slim(issue: dict, dialect: str = "adf",
         epic_link_field: str | None = None,
         cf_meta: dict | None = None) -> dict:
    f = dict(issue.get("fields", {}))
    if epic_link_field:
        # Cloud's parent-field unification serves fields.parent for
        # epic-linked issues; DC keeps the epic link in the gh-epic-link
        # customfield and serves parent only for subtasks. Mapping the DC
        # epic link onto parent keeps a faithful migration from mass-flagging
        # parent None-vs-PROJ-nn on every issue under an epic.
        epic_key = f.pop(epic_link_field, None)
        if epic_key and not f.get("parent"):
            f["parent"] = {"key": epic_key}
    dtext, dcanon = _texts(f.get("description"), dialect)
    f["description"] = {"len": len(dtext), "sha": content_fp(dcanon),
                        "head": dtext[:200]}
    c = f.get("comment") or {}
    items = []
    for cm in (c.get("comments") or []):
        ctext, ccanon = _texts(cm.get("body"), dialect)
        items.append({"author": (cm.get("author") or {}).get("displayName"),
                      "created": cm.get("created"), "updated": cm.get("updated"),
                      "len": len(ctext), "sha": content_fp(ccanon)})
    f["comment"] = {"total": c.get("total"), "inline": len(items), "items": items}
    f["attachment"] = [{"filename": a.get("filename"), "size": a.get("size"),
                        "created": a.get("created"),
                        "author": (a.get("author") or {}).get("displayName")}
                       for a in (f.get("attachment") or [])]
    wl = f.get("worklog") or {}
    f["worklog"] = {"total": wl.get("total")}
    f["issuelinks"] = [{"type": (l.get("type") or {}).get("name"),
                        "inward": (l.get("inwardIssue") or {}).get("key"),
                        "outward": (l.get("outwardIssue") or {}).get("key")}
                       for l in (f.get("issuelinks") or [])]
    etext, ecanon = _texts(f.get("environment"), dialect)
    # Presence gate keyed on the CANONICAL content, not the display text:
    # an image-only environment keeps raw markup as wiki display (truthy)
    # but renders empty from an ADF media node — a display-keyed gate stored
    # a fingerprint-of-nothing on one side and None on the other, a false
    # field_mismatch on every such issue. Canon emptiness is the same
    # question SPECS ultimately compares (the sha of the canon text).
    f["environment"] = ({"len": len(etext), "sha": content_fp(ecanon)}
                        if canon(ecanon) else None)
    for ts in _TS_FIELDS:
        if ts in f:
            f[ts] = norm_ts(f[ts])
    # Custom-field VALUE fingerprints, keyed by field NAME (ids differ per
    # instance) and type-normalized so the two migration sides compare. Only
    # non-empty values are kept; the epic-link field is skipped (folded into
    # parent above). The raw customfield_* values are then dropped entirely —
    # they are never stored (extract size + privacy); only fp + kind survive.
    if cf_meta is not None:
        cfmap: dict = {}
        for cid, meta in cf_meta.items():
            if cid == epic_link_field:
                continue
            nv = normalize_cf(f.get(cid), meta.get("schema"), dialect)
            if nv:
                cfmap[meta["name"]] = nv
        f["_cf"] = cfmap
    for cid in [k for k in f if isinstance(k, str)
                and k.startswith("customfield_")]:
        del f[cid]
    return {"key": issue["key"], "id": issue.get("id"), "fields": f}


_EPIC_LINK_SCHEMA = "com.pyxis.greenhopper.jira:gh-epic-link"


def _field_meta(client: JiraClient):
    """One /field fetch -> (cf_meta, epic_link_id, ambiguous_names).

    cf_meta maps each UNIQUELY-NAMED custom field id -> {"name", "schema"} for
    per-issue value comparison. The epic-link field (schema-matched, never
    name-matched — names are locale/rename-prone) is excluded: it is folded into
    `parent` by slim(). Custom fields that SHARE a name are ambiguous to match
    across instances, so they are dropped from cf_meta and returned in
    ambiguous_names for the header (disclosed, never silently mis-compared).
    Returns ({}, None, []) on any failure so extraction proceeds without value
    comparison rather than aborting."""
    st, d = client.req(f"{client.api_prefix}/field")
    if st != 200 or not isinstance(d, list):
        return {}, None, []
    epic = None
    customs = []
    for f in d:
        schema = f.get("schema") or {}
        if schema.get("custom") == _EPIC_LINK_SCHEMA:
            epic = f.get("id")
        if schema.get("custom") and f.get("id") and f.get("name"):
            customs.append((f["id"], f["name"], schema))
    customs = [(i, n, s) for (i, n, s) in customs if i != epic]
    counts = Counter(n for _, n, _ in customs)
    cf_meta = {i: {"name": n, "schema": s} for (i, n, s) in customs
               if counts[n] == 1}
    ambiguous = sorted({n for n, c in counts.items() if c > 1})
    return cf_meta, epic, ambiguous


def extract_project(client: JiraClient, project_key: str, out_path: str,
                    extra_fields: tuple = (),
                    progress: Callable[[int], None] | None = None) -> dict:
    dc = client.conn.deployment == "dc"
    dialect = "wiki" if dc else "adf"
    cf_meta, epic_link, cf_ambiguous = _field_meta(client)
    if dc:
        # DC search is a GET; enumerating hundreds of custom-field ids would
        # overflow the request URL (enterprise instances commonly have 700+
        # fields) and ABORT the extract. *all returns every field — core,
        # custom, and the epic-link customfield — in one token. slim() still
        # picks core + cf_meta from the response. (Requesting *all also avoids
        # naming statuscategorychangedate, which is Cloud-only and DC versions
        # handle inconsistently.)
        fields = ["*all"] + list(extra_fields)
    else:
        # Cloud search is a POST (fields in the body), so enumerating ids is
        # free and keeps the response lean. Cloud has no epic-link customfield.
        fields = list(CORE_FIELDS) + list(cf_meta.keys()) + list(extra_fields)
    cf_names = sorted(m["name"] for m in cf_meta.values())
    header = {"_extract_format": EXTRACT_FORMAT,
              "cf_names": cf_names, "cf_ambiguous": cf_ambiguous}
    # Keys are server-derived; escaping is defense in depth so a key carrying
    # a quote can never break out of the JQL literal.
    key = escape_query_key(project_key)
    tmp_path = out_path + ".tmp"
    n = 0
    with gzip.open(tmp_path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(header) + "\n")
        for iss in client.search_jql(
                f'project = "{key}" ORDER BY key ASC', fields,
                page=_extract_page()):
            fh.write(json.dumps(slim(iss, dialect, epic_link_field=epic_link,
                                     cf_meta=cf_meta),
                                default=str) + "\n")
            n += 1
            if progress and n % 500 == 0:
                progress(n)
    ac = client.approx_count(f'project = "{key}"')
    verified = isinstance(ac, int) and n == ac
    # Commit only when the extract is NOT known-truncated. A clean run that
    # paginated short of the authoritative count (ac is a known int and n != ac)
    # would otherwise cache a valid-looking, current-format file that a later
    # reuse run trusts -> confidently-wrong fidelity (review Bug 5). Discard the
    # partial like a crash; leave any prior complete out_path untouched. When ac
    # is unavailable (None / error string) it's not a KNOWN truncation, so the
    # best-effort extract is still committed (the caller decides via verified).
    if isinstance(ac, int) and n != ac:
        os.remove(tmp_path)
    else:
        os.replace(tmp_path, out_path)
    if progress:
        progress(n)
    return {"extracted": n, "approx": ac, "verified": verified}
