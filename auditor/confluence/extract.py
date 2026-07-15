"""Confluence page extraction — same workspace contract as auditor/extract.py.

Every current page of a space, slimmed to audit-critical fields plus a body
fingerprint (storage XHTML → canon text sha, so megabytes of prose become
comparable rows). Output: gzip JSONL stamped with the shared EXTRACT_FORMAT
header, one slim page per line KEYED BY TITLE — current pages are
title-unique per space, and title is what survives a migration (ids never
do). Count-verified against the CQL totalSize (R11) so a silent _links.next
gap can never masquerade as a clean extraction.

No dialect axis here: both Cloud and DC serve body.storage XHTML, so one
storage_text pass fingerprints both sides (spec non-goal: other
representations). Macro occurrences are counted per page at extraction time
because the raw body is gone by compare time — fields["macros"] is the sole
input to the R7 macro inventory audit.

Writes via temp-file + atomic rename: a mid-extraction crash (or a tripped
risk guard) leaves only an orphaned .tmp, so cached-extract reuse can never
pick up a truncated or refused file.
"""
from __future__ import annotations

import gzip
import json
import os
import re
from collections import Counter
from typing import Callable

from ..extract import EXTRACT_FORMAT
from ..textnorm import content_fp, macro_signature, norm_ts, storage_text

MACRO_RE = re.compile(r'<ac:structured-macro[^>]*\bac:name="([^"]+)"')


def slim_page(page: dict) -> dict:
    """One expanded v1 content row → slim audit row. Defensive .get chains
    throughout: a missing expansion reads as empty, never as a crash —
    except the body, whose absence the extract_space tripwire catches."""
    body = ((page.get("body") or {}).get("storage") or {}).get("value") or ""
    display = storage_text(body)
    ancestors = page.get("ancestors") or []
    # ancestors is root-first lineage; the LAST entry is the direct parent.
    parent = (ancestors[-1].get("title") if ancestors else None)
    hist = page.get("history") or {}
    labels = sorted(l.get("name") for l in
                    ((page.get("metadata") or {}).get("labels") or {})
                    .get("results", [])
                    if l.get("name"))
    # Inline child expansions are CAPPED (~25 rows): a next link, or a
    # declared size larger than what came inline, means the set is partial
    # and compare must treat the inline rows as a FLOOR, never diff a
    # truncated list as if it were the whole truth. Both belts apply to both
    # child types — next-link presence alone is unreliable, and a comment
    # envelope that truncates without one used to read capped=False, letting
    # two sides silently capped at the same inline count verify as equal.
    # The declared size travels in the row so compare can use it as a floor.
    catt = (page.get("children") or {}).get("attachment") or {}
    att_results = catt.get("results") or []
    att_capped = bool((catt.get("_links") or {}).get("next")) or \
        (catt.get("size") is not None and len(att_results) < catt.get("size", 0))
    ccom = (page.get("children") or {}).get("comment") or {}
    com_results = ccom.get("results") or []
    com_capped = bool((ccom.get("_links") or {}).get("next")) or \
        (ccom.get("size") is not None and len(com_results) < ccom.get("size", 0))
    # key=None would poison compare_space (None can't sort against str
    # titles; the presence summary slices the key) — a title-less row keys
    # by its id, reading as an honest presence finding, never a crash.
    base_key = page.get("title") or str(page.get("id") or "")
    # Blog posts are NAMESPACED so a page and a blog post sharing a title in the
    # same space don't collide into one comparison row (they are distinct
    # content). content_type travels for display/grouping.
    ctype = page.get("type") or "page"
    key = base_key if ctype != "blogpost" else f"[blog] {base_key}"
    return {"key": key, "id": page.get("id"), "fields": {
        "title": page.get("title"),
        "content_type": ctype,
        "parent": parent,
        "created": norm_ts(hist.get("createdDate")),
        "creator": ((hist.get("createdBy") or {}).get("displayName")),
        "version": (page.get("version") or {}).get("number"),
        "labels": labels,
        "body": {"len": len(display), "sha": content_fp(display),
                 "head": display[:200]},
        "attachment": {"capped": att_capped, "size": catt.get("size"),
                       "items": [
                           {"filename": a.get("title"),
                            "size": ((a.get("extensions") or {}).get("fileSize"))}
                           for a in att_results]},
        "comment": {"count": len(com_results), "capped": com_capped,
                    "size": ccom.get("size")},
        "macros": dict(Counter(MACRO_RE.findall(body))),
        # Macro TARGET config (ac:parameter values + ri:* refs) that
        # storage_text strips from the body sha — so a macro pointing at the
        # wrong JQL / included page / attachment is no longer a false clean.
        "macro_sig": macro_signature(body),
    }}


def extract_space(client, space_key: str, out_path: str,
                  progress: Callable[[int], None] | None = None) -> dict:
    """Identical contract to extract_project: stream, stamp, count-verify,
    atomic rename, return {"extracted","approx","verified"}."""
    tmp_path = out_path + ".tmp"
    n = 0
    empty_bodies = 0
    with gzip.open(tmp_path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"_extract_format": EXTRACT_FORMAT}) + "\n")
        for page in client.space_content(space_key):
            row = slim_page(page)
            if row["fields"]["body"]["len"] == 0:
                empty_bodies += 1
            fh.write(json.dumps(row, default=str) + "\n")
            n += 1
            if progress and n % 500 == 0:
                progress(n)
    # Risk guard: Cloud's content/search expand behavior is spec-verified but
    # not runtime-verified. If it ever stops returning body.storage, every
    # page would fingerprint as empty and compare would emit a mass false
    # content_mismatch — or a false CLEAN against an equally empty other
    # side. Raising BEFORE the rename keeps the refused extract out of the
    # cache. Threshold 10 avoids false-tripping tiny stub spaces.
    if n >= 10 and empty_bodies == n:
        raise RuntimeError(
            f"{space_key}: body.storage expansion returned no content for "
            f"any of {n} pages — the content API's expand behavior has "
            f"changed; refusing to fingerprint")
    ac = client.count_content(space_key)
    verified = isinstance(ac, int) and n == ac
    # Commit only when NOT known-truncated: a clean run that paginated short of
    # the authoritative count would otherwise cache a valid-looking, current-
    # format file that a later reuse run trusts -> wrong fidelity (review Bug 5).
    # ac unavailable (None) is not a KNOWN truncation -> still committed.
    if isinstance(ac, int) and n != ac:
        os.remove(tmp_path)
    else:
        os.replace(tmp_path, out_path)
    if progress:
        progress(n)
    return {"extracted": n, "approx": ac, "verified": verified}
