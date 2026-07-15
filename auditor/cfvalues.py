"""Type-aware normalization of one custom-field VALUE to a comparable token.

Custom-field IDs differ per instance, so a migration's two sides are matched by
field NAME and each value is normalized to the SAME canonical fingerprint keyed
by the field's schema TYPE — option *value* (not id), user *displayName* (not
accountId), canonical numbers/timestamps, sorted multi-value sets, wiki/ADF
prose canonicalized. Only a fingerprint + a `kind` are returned; the raw value
is never stored (extract size + privacy).

Reliability is not uniform across types, so this module does NOT decide truth —
it tags each value with a `kind`. Types whose cross-instance identity is
inherently uncertain (user/group pickers remap accounts; cascading selects;
app-provided types; rich text across dialects) are listed in SENSITIVE_KINDS so
the comparator can badge a mismatch as "verify" rather than certain data loss,
exactly like the existing cross_dialect badge on bodies.
"""
from __future__ import annotations

import json

from .textnorm import adf_text, canon, content_fp, h16, norm_ts, wiki_text

# Kinds whose normalization can legitimately differ across a faithful migration
# (account remap, option-id/structure, third-party shape, wiki<->ADF), so a
# mismatch on one of these is a VERIFY signal, not asserted data loss.
SENSITIVE_KINDS = frozenset({
    "user", "user_multi", "group", "group_multi",
    "version", "version_multi", "project",
    "cascade", "app", "text_rich",
})


def _is_empty(raw) -> bool:
    # 0 / 0.0 / False are REAL values — only None, "", [], {} are absent.
    return raw is None or raw == "" or (isinstance(raw, (list, dict))
                                        and len(raw) == 0)


def _exact(value, kind: str) -> dict | None:
    """Exact fingerprint (no canonicalization) for values a faithful migration
    preserves verbatim — option names, numbers, dates, codes. Uses h16 directly
    so the sign/punctuation/decimal point are NOT stripped (canon would collapse
    -1.0 and 1.0). A value that is None or blank/whitespace-only AFTER extraction
    is treated as ABSENT (None), so a value-less shape (e.g. {"value": null},
    [null]) never false-mismatches against a faithfully-cleared counterpart."""
    if value is None:
        return None
    s = str(value)
    if not s.strip():
        return None
    return {"fp": h16(s), "kind": kind}


def _prose(raw, dialect: str, kind: str) -> dict | None:
    """Canonical-prose fingerprint for rich text; an empty canon (blank or
    image-only body) is ABSENT."""
    canon_in = _canon_text(raw, dialect)
    if not canon(canon_in):
        return None
    return {"fp": content_fp(canon_in), "kind": kind}


def _opt(o):
    if isinstance(o, dict):
        return o.get("value") or o.get("name")
    return o


def _person(o):
    if isinstance(o, dict):
        return o.get("displayName") or o.get("name")
    return o


def _grp(o):
    if isinstance(o, dict):
        return o.get("name")
    return o


def _rich(custom: str) -> bool:
    return "textarea" in (custom or "")


def _canon_text(raw, dialect: str) -> str:
    """Rich-text CF prose -> canon input, dialect-aware (the same fingerprint
    firewall slim() uses for descriptions)."""
    if dialect == "wiki":
        return wiki_text(raw if isinstance(raw, str) else "")
    return adf_text(raw, for_canon=True)


def _sorted_join(items, project) -> str:
    return "|".join(sorted((project(x) or "") for x in items))


def normalize_cf(raw, schema: dict | None, dialect: str = "adf") -> dict | None:
    """Return {"fp": <token>, "kind": <type-class>} or None for an empty value.
    Never raises: an unrecognised shape falls through to the app/canonical-JSON
    path rather than failing the extract."""
    if _is_empty(raw):
        return None
    schema = schema or {}
    typ = schema.get("type")
    items = schema.get("items")
    custom = schema.get("custom") or ""

    try:
        if typ == "number":
            try:
                return _exact(repr(float(raw)), "number")
            except (TypeError, ValueError):
                return _exact(raw, "number")
        if typ in ("date", "datetime"):
            # norm_ts collapses tz/millis spellings of one instant; date-only
            # and unparseable inputs pass through unchanged (defensive for both).
            return _exact(norm_ts(str(raw)) or str(raw),
                          "date" if typ == "date" else "datetime")
        if typ == "option":
            return _exact(_opt(raw), "option")
        if typ == "option-with-child":
            parent = _opt(raw)
            child = _opt(raw.get("child")) if isinstance(raw, dict) else None
            return _exact(f"{parent}>{child}" if child else parent, "cascade")
        if typ == "user":
            return _exact(_person(raw), "user")
        if typ == "group":
            return _exact(_grp(raw), "group")
        if typ == "version":
            return _exact(_opt(raw), "version")
        if typ == "project":
            return _exact(raw.get("key") if isinstance(raw, dict) else raw,
                          "project")
        if typ == "string":
            if _rich(custom):
                return _prose(raw, dialect, "text_rich")
            return _exact(raw, "text")
        if typ == "array":
            vals = raw if isinstance(raw, list) else [raw]
            if items == "string":
                return _exact(_sorted_join(vals, lambda x: str(x)), "labels")
            if items == "option":
                return _exact(_sorted_join(vals, _opt), "option_multi")
            if items == "user":
                return _exact(_sorted_join(vals, _person), "user_multi")
            if items == "group":
                return _exact(_sorted_join(vals, _grp), "group_multi")
            if items == "version":
                return _exact(_sorted_join(vals, _opt), "version_multi")
            # unknown array element type -> app/canonical
            return _exact(_canon_json(vals), "app")
    except (AttributeError, TypeError):
        # An unexpected shape for the declared type -> fall through to canonical
        # JSON rather than crash the extract.
        pass
    # Unknown / app-provided type (and any shape that defied its declared type).
    return _exact(_canon_json(raw), "app")


def _canon_json(raw) -> str:
    try:
        return json.dumps(raw, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(raw)
