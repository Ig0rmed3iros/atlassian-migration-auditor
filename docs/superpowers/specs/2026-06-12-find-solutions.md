# Find Solutions: web-sourced fixes per finding

**Status:** approved for build (user: "just do it")
**Date:** 2026-06-12
**Builds on:** the multi-product + remediation engine. This is additive — a third tier alongside auto-fix and detect-and-guide: for ANY finding, search the web for known solutions and surface them with sources.

## 1. Problem

The auditor detects defects, fixes the faithfully-fixable ones, and guides on the rest — but its guidance is canned. The operator wants the app to **search the web (Atlassian docs / community / support KB) for known solutions and workarounds to a specific finding**, returning **all** credible candidates with **source links**, on demand, inside the app.

## 2. Decisions (from brainstorming)

- **Engine:** Claude API with its **web-search server tool**. Claude searches and returns a synthesized list of *every* credible solution it finds, each with steps + cited source links. (User: "all possible solutions".)
- **Scope:** a **Find solutions** button on **every** finding (issue findings, config findings, Tier-2 guidance). Runs only on click. (User: "Any finding, on demand".)
- **Privacy boundary (hard):** only finding **metadata** leaves the machine — defect kind, object names (status/field/macro/workflow names), counts, product/deployment. **Never** issue/page bodies, comments, or customer content. Enforced in the query builder, stated in UI + README.
- **Caching:** results cache per (run, finding signature) so re-opening is instant and free; a Refresh re-queries.
- **Key:** an Anthropic API key in Settings, Fernet-encrypted like the OAuth secret. No key → the button shows an actionable "add a key in Settings" prompt.

## 3. Requirements

- **R1 — solutions engine.** `auditor/solutions.py::find_solutions(finding, client, *, max_solutions=8)` builds a metadata-only query from the finding, calls Claude with the `web_search_20260209` tool (allowed-domains scoped to Atlassian: `support.atlassian.com`, `community.atlassian.com`, `confluence.atlassian.com`, `developer.atlassian.com`, `marketplace.atlassian.com`, `atlassian.com`, `jira.atlassian.com`), and returns `{query, solutions: [...], searched_at, model, error?}`. Each solution: `{title, summary, steps: [..], applicability, sources: [{title, url}], confidence}`. Returns *all* credible candidates, not one.
- **R2 — Claude SDK usage.** Use the official `anthropic` Python SDK (added to deps), not raw HTTP. Default model `claude-opus-4-8` (configurable via `MA_SOLUTIONS_MODEL` env / Settings; the value is pinnable). Adaptive thinking, `effort` configurable (default `medium` for interactive latency). The Anthropic client is **injected** into `find_solutions` so tests pass a fake (no live calls in the suite). Output is requested as a JSON object **via the prompt** (not `output_config.format`, which 400s alongside web-search citations); parsed defensively — a non-JSON or partial reply degrades to a single advisory entry, never a crash.
- **R3 — robustness.** Handle `stop_reason == "pause_turn"` (server web-search loop hit its cap) by re-sending up to 4 continuations; `stop_reason == "refusal"` → return `{solutions: [], error: "model declined"}`; map `anthropic.AuthenticationError` → "invalid/missing API key", `RateLimitError`/`APIStatusError`/`APIConnectionError` → surfaced, never silent. Never raise into the request thread — return an `error` field.
- **R4 — privacy query builder.** `build_query(finding)` emits a metadata-only natural-language question: product, deployment direction (e.g. "Jira Data Center to Cloud"), defect kind, object names, counts. It MUST NOT include description/body/comment text, `head`/`sha` fingerprints, or any captured value. A unit test asserts a finding carrying a body `head` never leaks it into the query.
- **R5 — key in Settings.** Settings gains an Anthropic API key field, stored Fernet-encrypted in the `settings` table (key `anthropic_api_key_enc`), mirroring `oauth_client_secret_enc`. A helper `anthropic_client(store)` builds an `anthropic.Anthropic` from the stored key (or returns None when unset).
- **R6 — caching.** New `finding_solutions(run_id, finding_sig, payload_json, created_at)` table + store methods `get_solutions(run_id, sig)` / `save_solutions(run_id, sig, payload)`. `finding_sig` = stable hash of (kind, area|project, name|keys). First click queries + caches; later clicks read cache; Refresh forces a re-query.
- **R7 — route.** `POST /runs/{run_id}/solutions` (form: the finding signature + enough finding fields to rebuild the query, or a finding-id lookup) → returns JSON `{solutions, sources, searched_at, cached, error?}`. No key → 400 with the actionable message. Reconstruct the finding from the stored finding row by signature; never trust client-sent content beyond identifiers.
- **R8 — UI.** A **Find solutions** button on each finding (issue findings table rows, config findings, Tier-2 guidance cards). Click → POST → render a results panel beneath that finding: one card per solution (title, summary, steps, applicability, source links opening in new tabs), a "searched N ago / Refresh" line, loading + error + no-key + empty states. A one-line privacy note ("only finding metadata is sent to Anthropic; never issue/page content").
- **R9 — back-compat & tests.** Existing `auditor.db` upgrades in place (new table via `_migrate` CREATE TABLE IF NOT EXISTS). The existing suite stays green. New tests cover query-building privacy, the SDK call shape (injected fake client returning a canned response with web-search blocks + a JSON answer), pause_turn continuation, refusal, JSON-parse fallback, caching, the route (no-key + cached + fresh), and the Settings key round-trip. No live API in the suite.

## 4. Architecture

### 4.1 `auditor/solutions.py` (pure-ish core)
`build_query(finding) -> str` (R4, metadata only). `find_solutions(finding, client, *, model, effort, max_solutions) -> dict` (R1–R3): assembles the Messages request (system: "You are an Atlassian migration expert; search the listed sources; return EVERY credible solution as JSON"; tools: web_search scoped; thinking adaptive; effort), runs the pause_turn continuation loop, extracts the assistant's final text, parses the JSON solutions array, attaches web-search source URLs, returns the shaped dict. `finding_signature(finding) -> str` (R6).

### 4.2 `webapp/anthropic_key.py`
`save_key(store, key)` / `anthropic_client(store) -> anthropic.Anthropic | None` (R5). Encryption reuses `store.encrypt`/`decrypt`.

### 4.3 Store (`webapp/store.py`)
`finding_solutions` table + `get_solutions`/`save_solutions`; `_migrate` adds it; settings already support arbitrary keys.

### 4.4 Webapp (`webapp/main.py` or new `webapp/solutions_routes.py`)
`POST /runs/{run_id}/solutions`; Settings GET/POST gain the key field.

### 4.5 UI
`webapp/templates/analysis.html` + `webapp/static/app.js` (or a small `solutions.js`): the button + results panel + states, reusing existing component classes so any future reskin styles it. `settings.html` gains the key input.

## 5. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Customer content leaks to Anthropic | Metadata-only query builder (R4) with a leak test; privacy note in UI/README |
| Structured output 400s with web-search citations | Request JSON via the prompt, parse defensively (R2) — never `output_config.format` with web search |
| Long latency (web-search loops + thinking) | `effort` default `medium`; on-demand only; loading state; pause_turn continuation capped at 4 |
| Model/cost drift | Model is a pinnable config value, default `claude-opus-4-8`; `max_uses` cap on the web-search tool |
| No/invalid key | Actionable UI prompt + 400; typed-exception mapping (R3) |
| Web search finds nothing | Empty state with a "no external solutions found — see built-in guidance" message |

## 6. Non-goals (v1)

- Auto-applying a web-sourced solution (read-only; the operator acts).
- Searching non-Atlassian sources by default (allowed-domains scoped; widenable later).
- Streaming the answer token-by-token to the UI (single JSON response; a spinner suffices).
- Per-finding background pre-fetch (on-demand only — cost control).

## 7. Acceptance

1. Full suite green (existing + new); no live API calls (injected fake client).
2. A finding with a body `head` produces a query that contains the object name but NOT the head text (privacy test).
3. `find_solutions` parses a canned multi-solution JSON answer + web-search source URLs into the shaped dict; a `pause_turn` response triggers exactly one continuation; a `refusal` returns `{solutions: [], error}`; malformed JSON degrades to one advisory entry.
4. `POST /runs/{id}/solutions` with no key → 400 + actionable message; with a key → solutions JSON; a second call → `cached: true`; Refresh → fresh.
5. Settings round-trips the encrypted Anthropic key; the analysis page renders the Find solutions button on a finding.
