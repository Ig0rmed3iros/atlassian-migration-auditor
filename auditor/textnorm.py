"""Cross-dialect text canonicalization — the fingerprint firewall (spec §4.3).

The compare engine never sees raw bodies, only sha16 fingerprints, so the
same authored prose MUST hash equal regardless of which dialect a side stores
it in: Jira Cloud bodies are ADF trees, Jira DC bodies are wiki-markup
strings, Confluence bodies are storage XHTML. Three dialect extractors reduce
each representation to approximate plain text; canon() then keeps only
lowercase unicode alphanumerics, so markup residue, whitespace and
punctuation differences can never leak into the hash. The extractors are
deliberately conservative: residue is harmless (canon kills it), the only
fatal mistakes are DELETING authored prose or KEEPING per-platform artifacts.

Mentions, emoji and inline cards are excluded from canon input on every
dialect — they render platform-specifically (an ADF mention carries a display
name, the wiki one a username) and are not authored prose. Display text
(len/head in extracts) keeps them.

This module must not import auditor.client: client re-exports adf_text/h16
from here for back-compat, so the dependency arrow points client → textnorm.
"""
from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone


def h16(s: str | None) -> str:
    return hashlib.sha1((s or "").encode("utf-8", "replace")).hexdigest()[:16]


def adf_text(node, *, for_canon: bool = False) -> str:
    """ADF tree → text. for_canon=True skips mention/emoji/inlineCard nodes
    so the canonical fingerprint matches a wiki side where [~user] is
    likewise removed; display text keeps them."""
    out: list[str] = []

    def walk(n):
        if isinstance(n, dict):
            t = n.get("type")
            if t == "text":
                out.append(n.get("text", "") or "")
            elif t == "hardBreak":
                out.append("\n")
            elif t == "mention" and not for_canon:
                out.append("@" + ((n.get("attrs") or {}).get("text", "") or ""))
            elif t == "emoji" and not for_canon:
                out.append((n.get("attrs") or {}).get("shortName", "") or "")
            elif t == "inlineCard" and not for_canon:
                out.append((n.get("attrs") or {}).get("url", "") or "")
            for c in (n.get("content") or []):
                walk(c)
        elif isinstance(n, list):
            for c in n:
                walk(c)

    walk(node)
    return "".join(out)


# Ordered passes: block tokens first (keep inner text), then per-platform
# artifacts (images, mentions, link urls), then line-leading markers, then
# inline emphasis unwraps. Mentions go before ~sub~ so [~user] can't feed it.
_WIKI_PASSES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\{code(?::[^}]*)?\}"), ""),
    (re.compile(r"\{noformat\}"), ""),
    (re.compile(r"\{quote\}"), ""),
    (re.compile(r"\{panel(?::[^}]*)?\}"), ""),
    (re.compile(r"\{color(?::[^}]*)?\}"), ""),
    # Wiki emoticons render as platform emoji — the ADF side stores them as
    # emoji NODES that for_canon skips, so they must be excluded here too.
    # Only the letter-bearing tokens matter for canon (punctuation-only ones
    # like :) or (!) die in canon anyway): (y) (n) (i) (x) (on) (off)
    # (*r)/(*g)/(*b)/(*y) and :P/:D. The colon forms only count when not
    # glued to alphanumerics on either side (host:Port stays prose).
    (re.compile(r"\((?:[ynix]|on|off|\*[rgby])\)", re.I), ""),
    (re.compile(r"(?<![a-zA-Z0-9]):[PDpd](?![a-zA-Z0-9])"), ""),
    # Media spans only: no whitespace inside AND at least one dot/pipe/colon
    # (image.png, image.png|thumb, attachment:x). Anything looser deletes
    # authored prose between adjacent exclamation marks ("failed!Retry!now"),
    # manufacturing cross-dialect false content mismatches.
    (re.compile(r"![^!\s]*[.|:][^!\s]*!"), ""),       # !image.png|thumb!
    (re.compile(r"\[~[^\]]+\]"), ""),                 # [~user] mentions
    (re.compile(r"\[([^|\]]+)\|[^\]]*\]"), r"\1"),    # [text|url] → text
    (re.compile(r"\[([^\]]+)\]"), r"\1"),             # [url] → url
    (re.compile(r"^h[1-6]\.\s", re.M), ""),           # headings
    (re.compile(r"^bq\.\s", re.M), ""),               # blockquote
    (re.compile(r"^[*#\-]+\s", re.M), ""),            # list markers
    (re.compile(r"\*([^*\n]+)\*"), r"\1"),            # *bold*
    (re.compile(r"_([^_\n]+)_"), r"\1"),              # _italic_
    (re.compile(r"\+([^+\n]+)\+"), r"\1"),            # +underline+
    (re.compile(r"~([^~\n]+)~"), r"\1"),              # ~subscript~
    (re.compile(r"\^([^^\n]+)\^"), r"\1"),            # ^superscript^
    (re.compile(r"\?\?([^?\n]+)\?\?"), r"\1"),        # ??citation??
    (re.compile(r"\{\{([^}\n]+)\}\}"), r"\1"),        # {{monospace}}
]


def wiki_text(s: str | None) -> str:
    """Jira DC wiki markup → approximate plain text."""
    out = s or ""
    for pat, repl in _WIKI_PASSES:
        out = pat.sub(repl, out)
    return out


_AC_PARAM = re.compile(r"<ac:parameter[^>]*>.*?</ac:parameter>", re.S)
_CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
_RI_TAG = re.compile(r"<ri:[^>]*>")
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


_CDATA_SLOT = re.compile("\x00(\\d+)\x00")


def storage_text(s: str | None) -> str:
    """Confluence storage XHTML → plain text. Macro parameters and ri:*
    resource refs are config, not prose — dropped entirely. CDATA bodies
    (ac:plain-text-body) ARE authored text and may legitimately contain
    angle brackets (code!), so they are stashed behind \\x00N\\x00
    placeholders BEFORE the generic tag strip — which would otherwise eat
    `if (x < 10 && y > 5)` down to `if (x 5)` — and restored verbatim
    after. Restoring after html.unescape also keeps CDATA literal (XML
    semantics: &amp; inside CDATA is four characters, not an entity)."""
    out = s or ""
    out = _AC_PARAM.sub(" ", out)
    stash: list[str] = []

    def _stash(m: re.Match) -> str:
        stash.append(m.group(1))
        return f"\x00{len(stash) - 1}\x00"

    out = _CDATA.sub(_stash, out)
    out = _RI_TAG.sub(" ", out)
    out = _TAG.sub(" ", out)
    out = html.unescape(out)
    out = _CDATA_SLOT.sub(lambda m: stash[int(m.group(1))], out)
    return _WS.sub(" ", out).strip()


# An ac:parameter in either form: open/close (group `content`) or self-closing.
_AC_PARAM = re.compile(
    r'<ac:parameter\b(?P<attrs>[^>]*?)(?:/>|>(?P<content>.*?)</ac:parameter>)',
    re.S)
_AC_NAME = re.compile(r'\bac:name="([^"]*)"')
# Target-identifying resource attributes of <ri:*> refs: the page TITLE and
# attachment FILENAME a macro/link points at. Deliberately EXCLUDES user
# identity (ri:username/userkey/account-id — the SAME person serialized
# differently per instance, which CCMA rewrites on a faithful migration), the
# space key (re-keyed during some migrations), and version counters — all of
# which are migration-volatile and would mass-false-positive.
_RI_ATTR = re.compile(r'\bri:(content-title|filename)="([^"]*)"')


def _param_value(val: str) -> str:
    """Canonical text of an ac:parameter body. CDATA bodies are LITERAL authored
    text (may contain `<`), so keep them; strip tags from the rest and unescape
    so `&amp;`/`&quot;` serialization differences don't false-mismatch."""
    if not val:
        return ""
    cdata = " ".join(_CDATA.findall(val))
    rest = _TAG.sub(" ", _CDATA.sub(" ", val))
    return canon(html.unescape(rest) + " " + cdata)


def macro_signature(s: str | None) -> str:
    """Fingerprint of a page's MACRO TARGET config — the ac:parameter values and
    page/attachment ri:* references that storage_text() strips as 'config, not
    prose'. Two pages with identical prose but a macro pointing at a different
    JQL / included page / attachment fingerprint EQUAL in the body sha (a false
    clean: the macro renders the wrong content or breaks after migration); this
    catches it. Migration-volatile content (user identity, space re-keys,
    version counters, macro instance ids) is deliberately excluded so a faithful
    migration never false-mismatches. Each (name,value) is hashed to a token
    BEFORE joining, so the final fingerprint can't dissolve the param boundaries
    (canon would otherwise collapse `ab=cd` and `abc=d`). Both Confluence
    deployments author storage XHTML, so it is cross-instance comparable. No
    macros/refs -> the stable empty fingerprint."""
    body = s or ""
    tokens: list[str] = []
    for m in _AC_PARAM.finditer(body):
        nm = _AC_NAME.search(m.group("attrs") or "")
        name = canon(nm.group(1)) if nm else ""
        tokens.append(h16(f"p\x1f{name}\x1f{_param_value(m.group('content') or '')}"))
    for attr, val in _RI_ATTR.findall(body):
        tokens.append(h16(f"r\x1f{attr}\x1f{canon(html.unescape(val))}"))
    return h16("|".join(sorted(tokens)))


def canon(s: str | None) -> str:
    """Lowercase alphanumerics only. Unicode isalnum, NOT ascii-restricted:
    prose in any language must survive canonicalization."""
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def content_fp(text: str | None) -> str:
    return h16(canon(text))


_TS_FORMATS = ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z")


def norm_ts(s: str | None) -> str | None:
    """ISO-8601 spellings of one instant → one epoch string. DC emits +0000,
    Cloud +00:00 or Z, millis vary by endpoint; comparing raw strings would
    flag every timestamp as drift. Date-only and unparseable inputs pass
    through unchanged — this must never raise mid-extraction."""
    if not s or "T" not in s:
        return s
    t = s.strip()
    for fmt in _TS_FORMATS:
        try:
            return str(int(datetime.strptime(t, fmt).timestamp()))
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return s
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return str(int(dt.timestamp()))
