"""Web-sourced solution discovery for a single finding (spec R1-R4).

Privacy boundary (R4): only finding METADATA — defect kind, object names,
keys, counts, product/deployment — is ever assembled into the query. Body
text, fingerprint heads/shas, and captured values are NEVER read here."""
from __future__ import annotations

import hashlib
import json
import os

_PRODUCT = {"jira": "Jira", "confluence": "Confluence"}
_KIND_PHRASE = {
    "missing_in_tgt": "is missing on the target after migration",
    "missing_in_src": "exists on the target but not the source",
    "type_mismatch": "has a different type on the target",
    "option_mismatch": "is missing select options on the target",
    "structure_mismatch": "has a different structure on the target",
    "field_mismatch": "has fields missing on the target",
    "content_mismatch": "has differing content on the target",
    "count_mismatch": "has a lower count on the target",
    "key_collision": "has a key collision (same key, different item)",
    "user_gap": "references a user not resolvable on the target",
    "area_error": "could not be read on one side",
}

# Irregular-plural area names that rstrip('s') would mangle.
# Regular plurals (e.g. 'issues' -> 'issue', 'fields' -> 'field') fall through
# to the rstrip('s') fallback.
_AREA_NOUN = {
    "macros": "macro",
    "statuses": "status",
    "priorities": "priority",
}


def build_query(finding: dict) -> str:
    """Assemble a web-search query for a single finding.

    ``product`` and ``deployment_from`` are run-context fields that individual
    findings (from compare.py, confluence/compare.py, config_audit.py) do NOT
    carry.  The caller is responsible for injecting them before calling this
    function, e.g.::

        enriched = {**finding, "product": migration["product"],
                    "deployment_from": migration["deployment_from"]}
        query = build_query(enriched)

    Without injection, product defaults to 'Atlassian' and direction defaults
    to 'Cloud to Cloud', which is still a valid (if generic) query.
    """
    product = _PRODUCT.get(finding.get("product"), "Atlassian")
    article = "an" if product[0].lower() in "aeiou" else "a"
    dep = finding.get("deployment_from")
    if dep == "dc":
        direction = f"{product} Data Center to Cloud"
    elif dep == "cloud" or dep is None:
        direction = f"{product} Cloud to Cloud"
    else:
        raise ValueError(f"Unknown deployment_from value: {dep!r}; expected 'cloud' or 'dc'")
    obj = (finding.get("name") or finding.get("src_key") or finding.get("tgt_key")
           or finding.get("project") or "object")
    area = finding.get("area") or ("issue" if finding.get("src_key") else "object")
    noun = _AREA_NOUN.get(area, area.rstrip("s").replace("_", " "))
    phrase = _KIND_PHRASE.get(finding.get("kind"), "differs after migration")
    field = finding.get("field")
    field_bit = f" (field '{field}')" if field else ""
    return (f"In {article} {direction} migration, the {noun} '{obj}'{field_bit} {phrase}. "
            f"What are the known solutions, workarounds, and root causes? "
            f"Search Atlassian's documentation, community, support knowledge base, "
            f"and marketplace.")


def finding_signature(finding: dict) -> str:
    """Return a 16-hex-char content hash for de-duplication.

    Tolerates None values in any field (emitted as empty string), matching
    the explicit-None shape that compare.py and confluence/compare.py produce
    for presence findings (``src_key: None``, ``tgt_key: None``, ``field: None``).
    """
    parts = [finding.get("kind") or "", finding.get("area") or "",
             finding.get("project") or "", finding.get("name") or "",
             finding.get("src_key") or "", finding.get("tgt_key") or "",
             finding.get("field") or "",
             # product + direction scope the query, so they scope the cache key
             # too (defence in depth — findings are already run-scoped).
             finding.get("product") or "", finding.get("deployment_from") or ""]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


_DOMAINS = ["support.atlassian.com", "community.atlassian.com",
            "confluence.atlassian.com", "developer.atlassian.com",
            "marketplace.atlassian.com", "jira.atlassian.com", "atlassian.com"]
_SYSTEM = (
    "You are an Atlassian migration expert. Use the web_search tool against "
    "Atlassian's documentation, community, support KB and marketplace to find "
    "EVERY credible solution, workaround, and root cause for the described "
    "migration defect. Then reply with ONLY a JSON object of this exact shape, "
    "no prose around it: {\"solutions\": [{\"title\": str, \"summary\": str, "
    "\"steps\": [str], \"applicability\": str, \"sources\": [{\"title\": str, "
    "\"url\": str}], \"confidence\": \"high\"|\"medium\"|\"low\"}]}. Include the "
    "real source URLs you found in each solution's sources. If you find nothing "
    "credible, return {\"solutions\": []}.")


def _final_text(resp) -> str:
    return "".join(b.text for b in resp.content
                   if getattr(b, "type", None) == "text" and getattr(b, "text", None))


def _collect_source_urls(resp) -> list:
    """Best-effort harvest of web_search result URLs from the response blocks,
    used as a fallback if a solution omitted its sources."""
    urls = []
    for b in resp.content:
        if getattr(b, "type", None) == "web_search_tool_result":
            for r in (b.content if isinstance(getattr(b, "content", None), list) else []):
                u = getattr(r, "url", None)
                if u:
                    urls.append({"title": getattr(r, "title", u), "url": u})
    return urls


def _parse(text, fallback_sources):
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        data = json.loads(text[start:end])
        sols = data.get("solutions", [])
        if isinstance(sols, list):
            for s in sols:
                if not s.get("sources") and fallback_sources:
                    s["sources"] = fallback_sources[:5]
            return sols
    except (ValueError, json.JSONDecodeError, AttributeError):
        pass
    # Degrade: one advisory entry carrying the model's prose + any found sources.
    return [{"title": "Search summary", "summary": text[:1500].strip() or
             "No structured solutions were returned.", "steps": [],
             "applicability": "review", "sources": fallback_sources[:5],
             "confidence": "low"}]


def find_solutions(finding: dict, client, *, model: str | None = None,
                   effort: str = "medium", max_solutions: int = 8) -> dict:
    """Search the web (via Claude's web_search server tool) for credible
    solutions to a single migration finding.

    ``searched_at`` is intentionally left ``None`` here — the caller (store/
    route) stamps it, keeping this function clock-free and testable."""
    import anthropic
    # Pin a specific snapshot so a silent server-side model update can't change
    # behaviour overnight. For the Claude 4.6+ generation the DATELESS id is the
    # pinned snapshot (no dated variant exists), so "claude-opus-4-8" is the pin
    # — matches webapp.ai_provider's _ANTHROPIC_DEFAULT_MODEL. Operator override:
    # MA_SOLUTIONS_MODEL.
    model = model or os.environ.get("MA_SOLUTIONS_MODEL",
                                    "claude-opus-4-8")
    # build_query validates run-context fields and raises ValueError on a bad
    # deployment_from. Build it INSIDE the guard so that never escapes as an
    # unhandled 500 in the route — it returns the standard error-dict instead.
    query = ""
    tools = [{"type": "web_search_20260209", "name": "web_search",
              "allowed_domains": _DOMAINS, "max_uses": 6}]
    sources = []
    try:
        query = build_query(finding)
        messages = [{"role": "user", "content": query}]
        for _ in range(5):   # original + up to 4 pause_turn continuations
            resp = client.messages.create(
                model=model, max_tokens=6000, system=_SYSTEM,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                tools=tools, messages=messages)
            if getattr(resp, "stop_reason", None) == "refusal":
                return {"query": query, "solutions": [],
                        "error": "the model declined this request",
                        "searched_at": None, "model": model}
            for s in _collect_source_urls(resp):   # accumulate across hops
                if s not in sources:
                    sources.append(s)
            if getattr(resp, "stop_reason", None) == "pause_turn":
                messages = messages + [{"role": "assistant", "content": resp.content}]
                continue
            break
        sols = _parse(_final_text(resp), sources)[:max_solutions]
        return {"query": query, "solutions": sols, "error": None,
                "searched_at": None, "model": model}
    except ValueError as exc:
        # build_query rejected the run-context (e.g. an unknown deployment_from).
        return {"query": query, "solutions": [], "error": str(exc),
                "searched_at": None, "model": model}
    except anthropic.AuthenticationError:
        return {"query": query, "solutions": [],
                "error": "invalid or missing Anthropic API key",
                "searched_at": None, "model": model}
    except (anthropic.RateLimitError, anthropic.APIConnectionError,
            anthropic.APIStatusError) as exc:
        return {"query": query, "solutions": [],
                "error": f"solution search failed: {exc}",
                "searched_at": None, "model": model}
    except anthropic.APIError as exc:
        # Catch-all for any other SDK error (e.g. APIResponseValidationError) so
        # nothing ever raises into the request thread (spec R3).
        return {"query": query, "solutions": [],
                "error": f"solution search failed: {exc}",
                "searched_at": None, "model": model}
