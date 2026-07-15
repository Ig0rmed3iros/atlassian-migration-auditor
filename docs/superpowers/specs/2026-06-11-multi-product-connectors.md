# Multi-Product Connectors: Jira DC→Cloud + Confluence (Cloud→Cloud & DC→Cloud)

**Status:** approved for build (user directive: build all discussed features, autonomous)
**Date:** 2026-06-11
**Prior art:** feasibility analysis recovered from session transcript (connector = product × deployment, injected at `webapp/stages.py`; ~65-70% of the codebase is already product-agnostic; coupling lives in `client.py`, `extract.py`, `compare.py`, `config_audit.py`).

## 1. Problem

The auditor supports exactly one migration shape: Jira Cloud → Jira Cloud. Real
migrations Igor audits are frequently Jira **Data Center** → Cloud and
**Confluence** (both Cloud→Cloud and DC→Cloud). The fidelity engine (compare,
verdict ladder, systematic-gap/core-fidelity derivation, store, run engine,
analysis API, Dark Console UI) is product-agnostic by construction — only the
four files above know Jira Cloud.

## 2. Requirements

- **R1 — product per migration.** A migration carries `product ∈ {jira, confluence}`,
  chosen at creation, immutable, default `jira`. Both sides always share product.
- **R2 — deployment per connection.** Each connection carries
  `deployment ∈ {cloud, dc}`, default `cloud`. DC auth is PAT-as-Bearer
  (token only, no email). Cloud PAT stays Basic email:token. OAuth remains
  Jira-Cloud-only.
- **R3 — Jira DC issue fidelity.** A DC side is audited with the full engine:
  presence/holes/tails/collisions, all SPECS fields, content/comment/attachment
  fingerprints. Cross-dialect content (DC wiki-markup vs Cloud ADF) is
  canonicalized so a faithful migration of prose does not emit false
  `content_mismatch` findings (see §5 textnorm).
- **R4 — Jira DC config parity, honest.** Config areas with a DC API are audited
  via `/rest/api/2`. Areas with no DC API are reported explicitly as
  `skipped` with a reason in the area summary (never silently absent, never
  false findings). The select-option deep check runs only when BOTH sides are
  cloud. Workflow structural diff runs only when both sides expose
  transitions/statuses.
- **R5 — Confluence page fidelity.** Spaces are matched by key (reusing
  `scope.match_projects`). Pages are matched by exact title within a space
  (current pages only). Compared per page: parent title, created timestamp
  (normalized), creator displayName, version number (low sev), labels,
  body fingerprint (storage-XHTML → canon text sha), attachment set (when
  fully inline), comment count. Inline caps overflow → advisory
  `attachment_uncheckable` / `comment_uncheckable`, never a mismatch.
- **R6 — Confluence tails by timestamp.** Cutover line = max(created) over
  common pages. A side-only page created after the line is
  `tail_post_cutover`; otherwise `missing_in_tgt`/`missing_in_src`. With no
  common pages there is no cutover evidence: everything is genuine
  missing/extra (mirrors Jira's `has_overlap` rule).
- **R7 — Confluence config audit = macro inventory.** Per-macro occurrence
  counts (`<ac:structured-macro ac:name="X">`) aggregated from extracts on
  each side. Macro present on source, absent on target → `missing_in_tgt`
  config finding (area `macros`); target count lower than source →
  `count_mismatch`. This is the #1 real-world Confluence migration failure
  (DC macros with no Cloud equivalent).
- **R8 — shared engine untouched in behavior.** Verdict ladder, fidelity math,
  systematic-gap derivation, store, run engine, analysis API and the UI shell
  work unchanged for all products. Labels adapt (`project→space`,
  `issue→page`) by product.
- **R9 — capability honesty.** Blind-spot detection and elevation are
  Jira-Cloud-only. Any other combo emits one explicit warn event per side:
  blind-spot detection not supported, counts unverified. The Elevate UI is
  hidden/guarded for those combos.
- **R10 — backward compatibility.** Existing `auditor.db` files upgrade in
  place (ALTER TABLE with defaults `jira`/`cloud`). The existing test suite
  stays green (with assertion updates only where shapes legitimately gained
  fields).
- **R11 — extraction count verification everywhere.** Jira Cloud:
  approximate-count (existing). Jira DC: search `total` with `maxResults=0`.
  Confluence: CQL search `totalSize` (`type=page and space=KEY`).
- **R12 — UI.** Create-migration form gains a product select. Connection form
  gains a deployment select; email input hidden/optional for DC. Dashboard
  shows a product chip. Analysis + run pages use product vocabulary
  (data-product attribute → JS label map).

## 3. Non-goals (v1)

- OAuth for DC or Confluence (PAT only — matches the PAT-first product direction).
- Confluence space permissions/templates/settings parity (macro inventory only).
- Blog posts, archived pages, drafts (current pages only — documented).
- Full pagination of per-page comments/attachments past the inline expansion
  cap (the `*_uncheckable` advisory pattern covers it, as it does for Jira).
- Cloud→DC / DC→DC support claims (not blocked by design, but untested).
- Confluence body comparison across *representations* other than storage
  (both Cloud and DC serve `body.storage` XHTML — same dialect on both sides).

## 4. Architecture

### 4.1 Connector registry — `auditor/connectors.py`

A frozen dataclass per product; `get_connector(product)` returns it. The
connector is the ONLY thing `webapp/stages.py` and `webapp/main.py` consult
for product-specific behavior:

```python
@dataclass(frozen=True)
class Connector:
    product: str                 # "jira" | "confluence"
    container_label: str         # "project" | "space"
    item_label: str              # "issue" | "page"
    supports_blind_spots: bool   # jira+cloud only (checked per side w/ deployment)
    supports_elevation: bool
    make_client: Callable        # (Connection, http) -> client
    verify: Callable             # (client) -> {"display_name","email","account_id"}
    list_containers: Callable    # (client) -> (rows [{key,name,id}], err)
    count_items: Callable        # (client, key) -> int | "ERR..."
    extract: Callable            # (client, key, out_path, progress) -> {"extracted","approx","verified"}
    compare: Callable            # (key, src_path, tgt_path, cross_dialect=False) -> {"stats","findings"}
    audit_config: Callable       # (src_client, tgt_client, containers, workspace, progress) -> {"areas","findings"}
    browse_url: Callable         # (conn_row_site_url, container_key, item_key) -> str
```

The jira connector wraps the existing modules verbatim. Deployment-specific
behavior lives INSIDE the client (api paths, auth, pagination, dialect), not
in the connector — the connector is the product axis only.

### 4.2 Client split — `auditor/client.py`

`BaseClient` keeps `req()` (retry/429/5xx/refresh), `_auth_header()`, helpers.
`Connection` gains `deployment: str = "cloud"`. Auth rule: `oauth` → Bearer
gateway token (unchanged); `pat` + `cloud` → Basic email:token (unchanged);
`pat` + `dc` → `Bearer {api_token}`. `JiraClient(BaseClient)` gains
`api_prefix` (`/rest/api/3` cloud, `/rest/api/2` dc) and DC branches for
search (startAt/total loop), count (`maxResults=0` → total), project list
(plain array), myself. `paginate_start_at` terminates on `isLast` OR
computed `startAt+len >= total` (DC envelopes lack `isLast`).
`ConfluenceClient(BaseClient)` lives in `auditor/confluence/client.py`;
cloud base = `site_url + /wiki`, dc base = `site_url`.

### 4.3 Text canonicalization — `auditor/textnorm.py`

The fingerprint firewall depends on both sides producing the same sha for the
same authored content. Three dialect extractors → one canonical form:

- `adf_text(node)` — existing walker (moves here; client re-exports for
  back-compat); keeps mentions/emoji/cards for *display* text.
- `wiki_text(s)` — strips Jira wiki markup: `{code}/{noformat}/{quote}/{panel}/{color}`
  blocks' markers, `[~mentions]` removed, `[text|url]` → text, `!image.png!`
  removed, heading/list/emphasis markers stripped.
- `storage_text(s)` — Confluence storage XHTML → text: `ac:*`/`ri:*` elements
  dropped, tags stripped, entities unescaped.
- `canon(s)` — lowercase, keep `[a-z0-9]` only. **Mentions, emoji and inline
  cards are excluded from canon input on ALL dialects** (they render
  differently per platform and are not authored prose).
- `content_fp(text)` → `h16(canon(text))` — the stored sha.
- `norm_ts(s)` — ISO-8601 variants (`+0000`, `Z`, millis) → canonical epoch
  string; date-only strings pass through.

Same-dialect runs are unaffected in outcome (equal texts stay equal). Extracts
are per-run artifacts, so the fingerprint change is not a migration concern
(documented: do not `reuse_extracts_from` a pre-upgrade run).

### 4.4 Store schema — `webapp/store.py`

`migrations` + `product TEXT NOT NULL DEFAULT 'jira'`;
`connections` + `deployment TEXT NOT NULL DEFAULT 'cloud'`.
Upgrade path: after `executescript(_SCHEMA)`, a `_migrate()` checks
`PRAGMA table_info` and issues `ALTER TABLE ... ADD COLUMN` when missing
(idempotent; SQLite ADD COLUMN with constant default backfills existing rows).

### 4.5 Stages — `webapp/stages.py`

`build_clients` reads `migration.product` + each connection's `deployment`,
resolves the connector once, and returns `(src, tgt, connector)`. Each stage
calls through the connector. `stage_permissions` short-circuits with the R9
warn event when blind spots are unsupported for a side. `stage_compare`
passes `cross_dialect = (src.deployment != tgt.deployment and product == "jira")`.
`stage_config` calls `connector.audit_config` (jira → existing config_audit
with capability gates; confluence → macro audit over the workspace extracts).
Events use connector vocabulary (`"3 space(s) in scope"`).

### 4.6 Confluence module — `auditor/confluence/`

- `client.py` — `ConfluenceClient(BaseClient)`: space list, page count (CQL
  totalSize), page iterator with body.storage + history + ancestors + labels
  + children.comment/attachment expansion, `/user/current` verify. Exact
  endpoints per deployment locked in the plan (research-verified).
- `extract.py` — `slim_page(page)` → `{key: title, id, fields: {...}}` with the
  same structural conventions as Jira slim (sha/len/head for body; items+caps
  for attachments/comments; per-page macro Counter in `fields["macros"]`).
  `extract_space(client, space_key, out_path, progress)` mirrors
  `extract_project` (gzip JSONL, tmp+rename, CQL count verification).
- `compare.py` — `compare_space(space, src_path, tgt_path)` emits the SAME
  stats keys and finding kinds as Jira compare (so findings.py, aggregate.py,
  analysis.py, UI work unchanged): presence via title sets, timestamp cutover
  tails (R6), collisions (created differs AND creator differs), CONF_SPECS
  field list, content/attachment/comment handling with uncheckable advisories.
- `macros.py` — `audit_macros(workspace, spaces)` reads both sides' extracts,
  aggregates macro counters, emits area summary + findings (R7).

### 4.7 Webapp + UI

- `main.py`: `POST /migrations` gains `product` form field; connection POST
  gains `deployment` (email required only for cloud); verify uses
  `connector.verify`; `scope.json` uses connector list/count; elevate routes
  guarded (404→redirect with error for non-jira-cloud).
- `analysis.py` summary + projects responses gain `product`,
  `container_label`, `item_label` (from the run's migration row).
- Templates: product select (index), deployment selects + conditional email
  (migration.html), product chip (dashboard), `data-product` on
  analysis/run pages, phase label map (run.html).
- `app.js`: `VOCAB` map keyed by product: `{jira: {container:'Project',
  item:'issue', ...}, confluence: {container:'Space', item:'page', ...}}`,
  consumed by heat table headers, KPI labels, finding prose, drill-down.

### 4.8 Small shared-engine accommodations

- `auditor/aggregate.py`: add `"attachment_uncheckable"` to the
  presence/advisory skip list (new advisory kind from Confluence compare;
  Jira never emits it).
- `auditor/findings.py`: `build_run_summary(..., item_label="issue",
  container_label="project")` — headlines templated on the labels; stats keys
  unchanged (UI relabels).
- `compare.py` (jira): `compare_project(..., cross_dialect=False)` — when
  true, content/comment mismatch details carry `"cross_dialect": true` so the
  UI can badge them as representation-sensitive.

## 5. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Confluence Cloud v1 REST removal breaks the single-code-path client | Research agent verifies current status; client isolates ALL paths in one class; if v1 is dead on cloud, cloud uses v2 endpoints behind the same methods (decision recorded in plan) |
| Cross-dialect canon still mismatches on macro-heavy bodies | Honest: it IS structural drift; detail carries `cross_dialect: true`, docs explain; systematic-gap derivation catches per-field artifacts |
| DC endpoints differ across DC versions (9.x vs 10.x) | Capability probes degrade to explicit `skipped`/`area_error`, never silent |
| Title collisions / renamed pages on Confluence | Same-title+different-created+different-creator → `key_collision`; renames read as hole+extra (documented limitation, matches feasibility) |
| Schema upgrade on live DB | ALTER TABLE ADD COLUMN is atomic in SQLite; defaults preserve existing rows; test covers reopening a v1-schema DB |

## 6. Acceptance

1. Full pytest suite green (existing + new; target ≥ 220 tests).
2. A simulated Jira DC→Cloud run (MockTransport end-to-end via stages tests)
   produces correct findings incl. no false content mismatch for identical
   prose in wiki vs ADF.
3. A simulated Confluence Cloud→Cloud run end-to-end: scope→extract→compare→
   macro audit→finalize with correct verdict + UI labels.
4. Existing Jira Cloud→Cloud behavior byte-identical on stats keys (run #2
   data still renders).
5. UI: create Confluence migration, connect DC source (form accepts token
   without email), labels read Space/page across analysis views.
