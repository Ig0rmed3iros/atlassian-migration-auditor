# Multi-Product Connectors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Jira DC→Cloud and Confluence (Cloud→Cloud & DC→Cloud) migration audits behind a (product × deployment) connector abstraction, keeping the entire fidelity engine, store, run engine, analysis API and UI shell shared.

**Architecture:** Product axis = connector registry (`auditor/connectors.py`) consulted only by `webapp/stages.py` and `webapp/main.py`. Deployment axis = inside the clients (auth header, API prefix, pagination, content dialect). The fingerprint firewall is preserved: every backend emits the same "slim" dict shape, so `compare`/`findings`/`aggregate`/UI work unchanged. Spec: `docs/superpowers/specs/2026-06-11-multi-product-connectors.md` — READ IT FIRST.

**Tech Stack:** Python 3.11+, httpx (+MockTransport for tests), FastAPI, SQLite, vanilla JS. Tests: pytest, synthetic data only (Acme/Globex/Igor Medeiros — NEVER real customer names).

**Working conventions (every task):**
- Branch: `feat/multi-product-connectors` (created in Task 0).
- TDD: write failing tests → run them (expect FAIL) → implement minimally → run module tests → run FULL suite (`python3 -m pytest tests -q`) → commit.
- Run tests from the repo root `/mnt/d/Atlassian-Products/Migration-auditor`.
- Commit messages: conventional (`feat:`/`refactor:`/`test:`), end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Match the codebase voice: dense module docstrings explaining WHY, minimal inline comments, same naming style.
- Test factories: tests have NO conftest.py; copy the local `mk_*` factory pattern of the module you extend.

---

### Task 0: Branch + baseline

- [ ] `cd /mnt/d/Atlassian-Products/Migration-auditor && git checkout -b feat/multi-product-connectors`
- [ ] `python3 -m pytest tests -q` — record the baseline count (expect 156 passed). If baseline is red, STOP and report.

---

### Task 1: Store schema — product + deployment columns

**Files:**
- Modify: `webapp/store.py`
- Test: `tests/test_store.py` (extend)

**Behavior:**
- `_SCHEMA`'s `migrations` gains `product TEXT NOT NULL DEFAULT 'jira'`; `connections` gains `deployment TEXT NOT NULL DEFAULT 'cloud'` (in the CREATE TABLE for fresh DBs).
- New `Store._migrate()` called at the end of `__init__` right after `executescript(_SCHEMA)`:

```python
def _migrate(self) -> None:
    """Idempotent in-place upgrades for pre-existing DB files. SQLite ALTER
    TABLE ADD COLUMN with a constant default backfills old rows atomically."""
    cols = {r[1] for r in self.db.execute("PRAGMA table_info(migrations)")}
    if "product" not in cols:
        self.db.execute("ALTER TABLE migrations ADD COLUMN product TEXT "
                        "NOT NULL DEFAULT 'jira'")
    cols = {r[1] for r in self.db.execute("PRAGMA table_info(connections)")}
    if "deployment" not in cols:
        self.db.execute("ALTER TABLE connections ADD COLUMN deployment TEXT "
                        "NOT NULL DEFAULT 'cloud'")
    self.db.commit()
```

- `create_migration(self, name: str, product: str = "jira") -> int` inserts product; validate `product in ("jira", "confluence")` else `ValueError`.
- `save_connection(..., deployment: str = "cloud")` persists deployment (add to the INSERT and the upsert SET list); validate `deployment in ("cloud", "dc")` else `ValueError`.

**Tests (TDD, write first):**
- `test_migration_product_default_and_explicit` — create_migration("A") → row product "jira"; create_migration("B", product="confluence") → "confluence"; invalid product raises ValueError.
- `test_connection_deployment_persisted` — save_connection with deployment="dc" → get_connection row has deployment "dc"; default is "cloud"; invalid deployment raises ValueError.
- `test_schema_upgrade_in_place` — build a v1 DB by hand: `sqlite3.connect(path)` + execute the OLD migrations/connections CREATE TABLE statements (copy them into the test without the new columns) + insert one migration row + close. Then `Store(db_path=path, key_path=...)` → `get_migration(1)["product"] == "jira"`; reopening a second time is a no-op (idempotent).

- [ ] Failing tests → implement → module green → full suite green → commit `feat(store): product/deployment columns with in-place schema upgrade`

---

### Task 2: textnorm — dialect extractors + canonical fingerprint

**Files:**
- Create: `auditor/textnorm.py`
- Test: `tests/test_textnorm.py` (new)
- Modify: `auditor/client.py` (re-export `adf_text` from textnorm for back-compat), `auditor/extract.py` imports unchanged for now (Task 5 rewires).

**Behavior:** module docstring explains the fingerprint firewall (spec §4.3). Public API:

```python
def adf_text(node, *, for_canon: bool = False) -> str
def wiki_text(s: str | None) -> str
def storage_text(s: str | None) -> str
def canon(s: str | None) -> str
def content_fp(text: str | None) -> str       # h16(canon(text))
def norm_ts(s: str | None) -> str | None
```

- `adf_text` is MOVED here from client.py verbatim, plus `for_canon=True` skips `mention`/`emoji`/`inlineCard` nodes (spec: not authored prose). `client.py` keeps `from .textnorm import adf_text` so existing imports work; `h16` stays in client.py and textnorm imports it (no cycle: textnorm must NOT import client — move `h16` into textnorm and re-export from client instead).
- `wiki_text`: regex passes, in order: remove `{code(:...)?}`, `{noformat}`, `{quote}`, `{panel(:...)?}`, `{color(:...)?}` TOKENS (keep inner text); remove `!...!` image refs; `[~user]` → ``; `[text|url]` → `text`; `[url]` → `url`; strip leading `h[1-6]\.\s`, `bq\.\s`, list markers `^[\*\#\-]+\s`; unwrap `*b*`, `_i_`, `+u+`, `~sub~`, `^sup^`, `??cite??`, `{{mono}}` → inner text. Keep it conservative — residue is acceptable, canon() kills punctuation anyway.
- `storage_text`: drop `<ac:parameter ...>...</ac:parameter>` and `<ri:[^>]*>` entirely; replace remaining tags with a space; `html.unescape`; collapse whitespace. CDATA content inside `ac:plain-text-body` is KEPT (it is authored text).
- `canon`: `"".join(ch for ch in (s or "").lower() if ch.isalnum())` — restricted further to ASCII? NO: keep unicode alnum (prose in any language must survive).
- `content_fp(text)` = `h16(canon(text))`.
- `norm_ts`: accept `2024-01-02T03:04:05.000+0000`, `...+00:00`, `...Z`, second-precision variants → `str(int(epoch))`; date-only `2024-01-02` returned unchanged; unparseable input returned unchanged (never raise). Use `datetime.strptime` attempts then `datetime.fromisoformat` fallback (Python 3.11 fromisoformat handles most).

**Tests:** same prose authored in ADF vs wiki vs storage → identical `content_fp`:
- `test_cross_dialect_prose_equal_fp` — ADF doc for "Hello World, line two" (paragraph + hardBreak) vs wiki `"Hello *World*,\nline two"` vs storage `"<p>Hello <strong>World</strong>,</p><p>line two</p>"` → one fp.
- `test_mentions_excluded_from_canon` — ADF with mention `@Igor Medeiros` vs wiki with `[~imedeiros]` in same sentence → equal fp; but `adf_text` WITHOUT for_canon still includes `@Igor Medeiros` (display text unchanged).
- `test_wiki_markup_stripped` — code blocks, links `[Acme|https://acme.example]` → text keeps "Acme", loses URL? NO — keep the link TEXT only; bare `[https://acme.example]` keeps the URL text. Assert exact `wiki_text` output for a composite sample.
- `test_storage_macros_dropped_but_cdata_kept` — `<ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA[x = 1]]></ac:plain-text-body></ac:structured-macro>` → contains "x = 1".
- `test_norm_ts_variants_equal` — the four timestamp spellings of the same instant → same value; `"2024-01-02"` unchanged; `"garbage"` unchanged.
- `test_h16_and_adf_text_reexported_from_client` — `from auditor.client import adf_text, h16` still works and is the same object.

- [ ] Failing tests → implement → green → full suite → commit `feat(textnorm): cross-dialect text canonicalization + fingerprint`

---

### Task 3: Client — BaseClient split + Jira DC deployment support

**Files:**
- Modify: `auditor/client.py`
- Test: `tests/test_client.py` (extend; keep ALL existing tests passing unmodified)

**Behavior:**
- `Connection` gains `deployment: str = "cloud"` (dataclass field after `site_url`). `api_base` unchanged (DC base is the bare site_url, same as cloud PAT).
- `_auth_header`: `oauth` → Bearer access_token (unchanged); `pat` + `deployment == "dc"` → `f"Bearer {c.api_token}"`; `pat` cloud → Basic (unchanged).
- Split: `class BaseClient` owns `__init__`, `_refresh`, `_refresh_safe`, `_auth_header`, `req`, `paginate_start_at`, `sleep` — moved verbatim. `class JiraClient(BaseClient)` keeps the Jira-specific methods. Public surface unchanged (`JiraClient(conn, http=..., sleeper=...)`).
- `JiraClient.api_prefix` property → `"/rest/api/2"` if `self.conn.deployment == "dc"` else `"/rest/api/3"`.
- `paginate_start_at` termination (research-verified against DC + Cloud envelopes):
  1. `isLast` present → use it (existing behavior).
  2. else `total` present → stop when `len(out) >= total` (DC PageBean without isLast).
  3. else (NEITHER isLast nor total: an unpaginated wrapper like `/permissionscheme`'s
     `{permissionSchemes:[...]}` or DC `/issuetypescheme`'s `{schemes:[...]}`) →
     **single page, stop immediately.** This also fixes a latent bug: today the loop
     re-requests unpaginated wrappers until the 20k cap, accumulating duplicates that
     only survive because downstream name-dedup hides them. Add a regression test:
     `test_paginate_wrapper_without_islast_is_single_request` (handler counts requests; must be 1).
- `search_jql` when dc → **keyset pagination by id** (NOT startAt): DC 10/11 instances
  with the OpenSearch backend enforce a 10,000-result window — `startAt` past it fails
  with HTTP 500 "Search limit exceeded" (Atlassian KB workaround: `AND id > lastId`).
  Implementation: strip a trailing `ORDER BY ...` clause from the incoming jql
  (case-insensitive rsplit) → `bare`; first request `GET {api_prefix}/search` params
  `{"jql": f"({bare}) ORDER BY id ASC", "startAt": 0, "maxResults": page,
  "fields": ",".join(fields)}`; subsequent requests use
  `({bare}) AND id > {max_id_seen} ORDER BY id ASC` with `startAt` always 0; stop on
  empty page. `max_id_seen = max(int(i["id"]) for i in page_issues)`. Yield every issue.
  Cloud path unchanged. (Compare loads extracts into dicts, so emission order is
  irrelevant to correctness.)
- `approx_count`: dc → `GET {api_prefix}/search` params `{"jql": jql, "maxResults": 0}` → `d.get("total")` (live-verified exact under Lucene; document that OpenSearch-backed instances MAY cap counts above 10k — acceptable: a capped count fails the extraction-verification gate loudly rather than silently). Cloud unchanged.
- `all_projects`: dc → `GET /rest/api/2/project` params `{"expand": "description,lead"}` returns a plain LIST → `(list, None)`; on non-200 → `([], f"ERR{st}:...")`. Cloud unchanged.
- `myself`: path = `f"{self.api_prefix}/myself"` (works on both; DC returns no accountId — callers already use `.get`).
- `sd_list`: ADD header support — `req()` gains optional `headers: dict | None = None` merged over the defaults, and `sd_list` ALWAYS passes `{"X-ExperimentalApi": "true"}` (research-verified: JSM DC 5.x queue endpoints 403 without it, value is the string "true"; graduated/Cloud endpoints ignore it — always sending is the safe cross-version behavior). Note: JSM paging has `isLastPage` (existing sd_list handles it) and no total.

**Tests (MockTransport, follow existing `mk_pat` style; add `mk_dc`):**

```python
def mk_dc(handler):
    conn = Connection(auth_type="pat", site_url="https://jira.acme.example",
                      deployment="dc", api_token="tok-123")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)
```

- `test_dc_auth_header_is_bearer` — handler asserts `Authorization == "Bearer tok-123"` and no email needed.
- `test_dc_search_keyset_paginates_by_id` — handler serves 3 pages keyed off the `id > N` clause in the jql param (issues carry ids 1..120); collect 120 issues; assert path `/rest/api/2/search`, `startAt` is always 0, the FIRST request has no `id >` clause, and the incoming `ORDER BY key ASC` was replaced by `ORDER BY id ASC`.
- `test_dc_approx_count_uses_total` — maxResults=0 → returns 120.
- `test_dc_all_projects_plain_array` — returns `([{...}], None)`.
- `test_paginate_start_at_total_termination` — DC-shaped envelope `{startAt, maxResults, total, values}` WITHOUT isLast terminates correctly (2 pages) and does not loop forever.
- `test_paginate_wrapper_without_islast_is_single_request` — `{permissionSchemes:[...]}` with neither isLast nor total → exactly ONE request, no duplicate rows.
- `test_sd_list_sends_experimental_header` — handler asserts `X-ExperimentalApi: true` on servicedeskapi calls.
- `test_cloud_paths_unchanged` — a cloud client still hits `/rest/api/3/search/jql` (guard against regression; may already exist — extend if so).

- [ ] Failing tests → implement → green → full suite → commit `feat(client): BaseClient split + Jira Data Center deployment support`

---

### Task 4: Extract — dialect-aware slimming

**Files:**
- Modify: `auditor/extract.py`
- Test: `tests/test_extract.py` (extend)

**Behavior:**
- `slim(issue: dict, dialect: str = "adf") -> dict`. Body handling:
  - `dialect == "adf"` (cloud): display text via `adf_text(body)`, canon fp via `adf_text(body, for_canon=True)`.
  - `dialect == "wiki"` (dc): body fields are plain strings → display text = the raw string, canon input via `wiki_text(s)`.
  - description/environment/comments store `{"len": len(display), "sha": content_fp(canon_input), "head": display[:200]}` — sha now ALWAYS `content_fp(...)` (canonical) for every dialect, len/head stay readable.
- `created`/`updated`/`resolutiondate`/`statuscategorychangedate` values pass through `norm_ts` in slim (so DC `+0000` and any cloud variant compare equal). `duedate` is date-only — unchanged by norm_ts.
- `extract_project(client, project_key, out_path, ...)`: derive `dialect = "wiki" if client.conn.deployment == "dc" else "adf"`. CORE_FIELDS: when dc, drop `"statuscategorychangedate"` (Cloud-only field — research-verified; DC rejects/ignores unknown fields inconsistently across versions, so don't request it).
- comment items sha uses the same dialect path as description.

**Tests:**
- `test_slim_wiki_dialect_string_bodies` — issue with string description `"h1. Title\nBody *bold* [~imedeiros]"` and string comment bodies → sha equals `content_fp(wiki_text(...))`, head is the raw readable string prefix, len > 0.
- `test_slim_adf_and_wiki_same_prose_same_sha` — the cross-dialect invariant at the slim level.
- `test_slim_normalizes_timestamps` — created `"2024-01-02T03:04:05.000+0000"` (slim) == created `"2024-01-02T03:04:05+00:00"` (slim) after norm.
- `test_extract_project_dc_drops_cloud_only_field` — with a dc client, the search request's fields do NOT include statuscategorychangedate.
- Existing extract tests must stay green (the adf display path is unchanged; shas change value — update any test asserting a LITERAL sha to compute it via content_fp).

- [ ] Failing tests → implement → green → full suite → commit `feat(extract): dialect-aware content fingerprints + timestamp normalization`

---

### Task 5: Jira compare — cross-dialect badge (small)

**Files:**
- Modify: `auditor/compare.py`
- Test: `tests/test_compare.py` (extend)

**Behavior:**
- `compare_project(project, src_path, tgt_path, cross_dialect: bool = False)`.
- When `cross_dialect` and a `content_mismatch` or `comment_mismatch` finding is emitted, its detail dict gains `"cross_dialect": True` (UI/readers know representation drift may contribute).
- NO other behavior change.

**Tests:**
- `test_cross_dialect_flag_on_content_findings` — build two tiny gz extracts (copy the existing test helper pattern) differing in description sha; with cross_dialect=True the content_mismatch detail has cross_dialect True; with default False the key is absent.

- [ ] Failing → implement → green → full suite → commit `feat(compare): cross-dialect badge on content findings`

---

### Task 6: Config audit — DC capability gating

**Files:**
- Modify: `auditor/config_audit.py`
- Test: `tests/test_config_audit.py` (extend)

**Behavior:**
- All paths derive from each client's `api_prefix` (e.g. `f"{src.api_prefix}/status"`). SIMPLE table holds path SUFFIXES (`"/status"`, ...) joined per client per call (src and tgt may have different prefixes!).
- Capability gates, derived from `client.conn.deployment` (`dc_side = "dc" in (src.conn.deployment, tgt.conn.deployment)`):
  - Cloud-only areas, SKIPPED when either side is dc (research-verified against the
    complete DC 11.3 OpenAPI path inventory): `issuetype_screen_schemes`,
    `screen_schemes`, `field_configurations`, `field_config_schemes`,
    `workflow_schemes` (DC has list-all REST for NONE of these; `/workflowscheme` on DC
    is per-id/per-project only). For each skipped area:
    `areas[area] = {"label": area, "skipped": True, "reason": "no Data Center API — verify manually"}`
    and NO findings.
  - `issuetype_schemes` is NOT skipped — it exists on DC as `GET /rest/api/2/issuetypescheme`
    returning the wrapper `{schemes:[...]}` (vs Cloud's paginated `{values,isLast}`).
    Implement a per-side fetch: `key="schemes"` for a dc side, default for cloud.
    SIMPLE-table mechanics: add an optional per-deployment override map
    `DC_KEYS = {"issuetype_schemes": "schemes"}` consulted when a side is dc.
  - `workflows`: when either side is dc, the structural diff (transitions/statuses) is
    skipped — name-presence diff only. DC side fetches `GET /rest/api/2/workflow`
    (plain array of `{name, description, steps, ...}` — steps is an int COUNT, no
    transition detail exists on DC); cloud sides keep `/workflow/search`. Area summary
    gains `"structure_checked": False` when skipped.
  - custom_fields: presence + type diff works on both (`/rest/api/2/field` is a plain
    array on DC). The select-OPTION deep check requires `/field/{id}/context`
    (Cloud-only; DC's `/customFields/{id}/options` is experimental with an unverified
    item shape — documented fast-follow, NOT v1): run it only when BOTH sides are
    cloud; otherwise `summ["options_checked"] = False`.
  - `screens` area: DC `GET /rest/api/2/screens` honors startAt/maxResults but returns
    PLAIN ARRAY SLICES (no envelope, no isLast) — `paginate_start_at`'s list branch
    would silently return only the first page. Add a module-local
    `_dc_list_sliced(client, path)`: loop `startAt += len(chunk)`, accumulate, stop
    when a chunk is shorter than maxResults OR adds no new ids (guard against
    endpoints that ignore startAt and replay the full list). Use it for the dc side of
    screens. Tabs/fields deep-check endpoints exist on DC → deep check stays, paths
    via api_prefix.
  - permission/notification schemes exist on DC (`/rest/api/2/permissionscheme`
    wrapper; `/rest/api/2/notificationscheme` paginated WITH isLast) → keep, paths via
    api_prefix.
  - JSM: unchanged (sd_list already sends the experimental header from Task 3; JSM DC
    shapes match Cloud's start/limit/isLastPage).
- `audit_config` signature unchanged (clients carry their own deployment).

**Tests (extend the `make_pair`/BASE mock pattern):**
- `test_dc_source_skips_cloud_only_areas` — src dc + tgt cloud → `areas["workflow_schemes"]["skipped"] is True`, no findings for that area, and the audit still emits statuses/priorities findings from `/rest/api/2/...` on the src side and `/rest/api/3/...` on the tgt side (assert both path prefixes were actually requested via the handler's seen-URLs set).
- `test_dc_issuetype_schemes_uses_schemes_key` — dc side served `{"schemes":[...]}` → area diff works, not skipped.
- `test_dc_workflows_name_presence_only` — workflow on src(dc array shape) missing on tgt → missing_in_tgt finding; no structure_mismatch findings; `areas["workflows"]["structure_checked"] is False`.
- `test_dc_screens_sliced_array_pagination` — dc /screens serves 50+20 as two array slices → all 70 seen; an endpoint replaying the same array → loop stops (no-new-ids guard).
- `test_options_check_requires_both_cloud` — dc+cloud pair: no `/context` requests issued; `options_checked is False`. cloud+cloud pair: context requests issued (existing behavior).

- [ ] Failing → implement → green → full suite → commit `feat(config): Data Center capability gating with explicit skipped areas`

---

### Task 7: Connector registry

**Files:**
- Create: `auditor/connectors.py`
- Test: `tests/test_connectors.py` (new)

**Behavior:** spec §4.1 dataclass, plus:

```python
def get_connector(product: str) -> Connector:
    try:
        return _REGISTRY[product]
    except KeyError:
        raise ValueError(f"unknown product {product!r}") from None

def supports_blind_spots(connector: Connector, deployment: str) -> bool:
    return connector.supports_blind_spots and deployment == "cloud"
```

- Jira connector wires existing modules: `make_client=lambda conn, http: JiraClient(conn, http=http)`, `verify` calls `client.myself()` → `{"display_name": me.get("displayName"), "email": me.get("emailAddress"), "account_id": me.get("accountId")}`, `list_containers=lambda c: c.all_projects()` normalized to `[{key,name,id}]`, `count_items=lambda c, k: c.approx_count(f'project = "{k}"')`, `extract=extract_mod.extract_project`, `compare=compare_mod.compare_project`, `audit_config` adapter (`lambda src, tgt, containers, workspace, progress: config_mod.audit_config(src, tgt, jsm_projects=containers, progress=progress)`), `detect_blind_spots=perm_mod.detect_blind_spots`, `browse_url=lambda site, container, item: f"{site.rstrip('/')}/browse/{item}"`.
- Registry starts `{"jira": JIRA}`; Task 11 adds confluence.

**Tests:**
- `test_get_connector_jira_and_unknown` — jira returns Connector with labels project/issue; "confluence" raises ValueError UNTIL Task 11 (write the test as: unknown product "bamboo" raises).
- `test_jira_connector_verify_shapes_identity` — fake client with stubbed myself() → dict with the three keys.
- `test_supports_blind_spots_matrix` — jira+cloud True; jira+dc False.

- [ ] Failing → implement → green → full suite → commit `feat(connectors): product registry with jira connector`

---

### Task 8: Stages through the connector

**Files:**
- Modify: `webapp/stages.py`
- Test: `tests/test_stages.py`, `tests/test_stages_pipeline.py` (extend; existing tests stay green)

**Behavior:**
- `build_clients(store, migration_id, http=None, require_both=True)` → returns `(src, tgt, connector)`. Reads `store.get_migration(migration_id)["product"]` → `connector = get_connector(product)`; per side builds `Connection(..., deployment=row["deployment"] or "cloud")` and `connector.make_client(conn, http)`. OAuth branch only valid for jira (assert/raise RuntimeError "oauth is only supported for Jira Cloud" if product != jira and auth_type == oauth).
- `ctx["connector"] = connector` set in `stage_verify`.
- `stage_verify`: `me = connector.verify(cl)`; event text uses `me["display_name"]`; `mark_connection_verified(row["id"], me.get("email") or "")`; `ctx[f"{role}_account_id"] = me.get("account_id")`.
- `stage_scope`: `connector.list_containers` + `connector.count_items`; event vocabulary via `connector.container_label` (`f"{n} {connector.container_label}(s) in scope"`).
- `stage_permissions`: per side, `if not supports_blind_spots(connector, cl.conn.deployment): _say(ctx, "permissions", f"blind-spot detection not supported for {connector.product} on {cl.conn.deployment} — {side} counts unverified", "warn"); continue`.
- `stage_extract`: `connector.extract(cl, m["key"], path, progress=...)` (same verification gate).
- `stage_compare`: `cross = (connector.product == "jira" and ctx["src"].conn.deployment != ctx["tgt"].conn.deployment)`; call `connector.compare(m["key"], src_path, tgt_path, cross_dialect=cross)`.
- `stage_config`: `connector.audit_config(ctx["src"], ctx["tgt"], containers=[m["key"] for m in ctx["selected"]], workspace=ctx["workspace"], progress=...)`. Jira adapter keeps `params["jsm_projects"]` override: if `ctx["params"].get("jsm_projects")` pass those as containers for jira.
- `undo_migration_elevations` + `build_stages` signatures unchanged. All callers of `build_clients` updated (main.py does in Task 9 — for THIS task update the call sites minimally to unpack 3 values and ignore the connector where unused).

**Tests:**
- Existing stage tests updated mechanically for the 3-tuple (keep semantics identical — jira/cloud defaults flow through migration rows automatically via Task 1 defaults).
- `test_stage_permissions_dc_side_skips_with_warning` — fake connector/clients where src deployment dc → no detect_blind_spots call for src, one warn event mentioning "not supported", tgt (cloud) still checked.
- `test_stage_compare_passes_cross_dialect_for_mixed_deployments` — spy connector.compare records kwargs; src dc + tgt cloud → cross_dialect True; cloud+cloud → False.
- `test_build_clients_returns_connector_for_product` — migration with product jira → connector.product == "jira"; oauth + (hypothetical) non-jira product raises RuntimeError.

- [ ] Failing → implement → green → full suite → commit `refactor(stages): dispatch every stage through the product connector`

---

### Task 9: Webapp — forms, verify, scope, elevation guards

**Files:**
- Modify: `webapp/main.py`
- Test: `tests/test_main.py` (extend)

**Behavior:**
- `POST /migrations`: `product: str = Form("jira")`; invalid product → redirect `/?error=...` (303), no creation.
- `POST /migrations/{mid}/connections`: new `deployment: str = Form("cloud")`; `email: str = Form("")` becomes optional — required only when deployment == "cloud" (if missing → redirect with error "email is required for cloud connections"). Secret dict for dc: `{"token": api_token.strip()}` (no email key). Verification: `connector = get_connector(store.get_migration(mid)["product"])`; `cl = connector.make_client(conn, app.state.http)`; `me = connector.verify(cl)` (wrap ClientError as today). `save_connection(..., deployment=deployment)`.
- `build_clients` 3-tuple unpacked at call sites (`scope_preview`, `_side_client`).
- `scope_preview`: list/count via connector (counts label-agnostic); response unchanged in shape plus `"product"` and labels: `{"matched": ..., "source_only": ..., "target_only": ..., "product": product, "container_label": ..., "item_label": ...}`.
- Elevate routes (`GET/POST /runs/{run_id}/elevate*`): resolve the run's migration product + the side's deployment; when not (jira and cloud) → redirect to `/runs/{run_id}` with error event logged ("elevation is only supported for Jira Cloud"). `oauth_start`: if migration product != jira → redirect with error.
- Dashboard (`/`): each migration dict gains `m["product"]` (already on the row after Task 1 — just ensure template receives it).
- `migration_page` + `analysis_page` template ctx gain `product` (from mig row) and each connection's deployment.

**Tests (TestClient + mock transport, existing factory style):**
- `test_create_migration_with_product` — POST product=confluence → migration row product confluence; invalid → no new migration.
- `test_dc_connection_no_email_required` — POST deployment=dc with empty email, handler answers `/rest/api/2/myself` with Bearer auth → connection saved with deployment dc; secret has no email.
- `test_cloud_connection_still_requires_email` — POST deployment=cloud, empty email → redirect with error, nothing saved.
- `test_elevate_blocked_for_dc` — run on a migration whose source is dc → GET elevate redirects to run page.
- `test_scope_preview_includes_product_labels`.

- [ ] Failing → implement → green → full suite → commit `feat(webapp): product/deployment aware forms, verify and guards`

---

### Task 10: Confluence client

**Files:**
- Create: `auditor/confluence/__init__.py` (empty), `auditor/confluence/client.py`
- Test: `tests/test_confluence_client.py` (new)

**Behavior:** `ConfluenceClient(BaseClient)` — same constructor contract as JiraClient.

```python
class ConfluenceClient(BaseClient):
    @property
    def api_base(self) -> str:           # overrides Connection-derived base use
        base = self.conn.site_url.rstrip("/")
        return base + "/wiki" if self.conn.deployment == "cloud" else base
```

(Implementation note: BaseClient.req uses `self.conn.api_base`; refactor so req uses `self.api_base` property on the CLIENT, defaulting to `self.conn.api_base` in BaseClient — one-line override point, no Connection change.)

- **Research-verified facts (2026-06-11, from live Atlassian OpenAPI specs):** Cloud v1
  content/space enumeration (`GET /wiki/rest/api/content`, `GET /wiki/rest/api/space`,
  `GET .../content/{id}/child`) is REMOVED — returns 410 Gone. Survivors on Cloud:
  `GET /wiki/rest/api/content/search` (CQL + `expand` + cursor `_links.next`) and
  `GET /wiki/rest/api/search` (CQL, response carries required `totalSize`). DC keeps the
  classic v1 API (`/rest/api/content?spaceKey=...&expand=...`, `{results,start,limit,size,_links.next}`)
  and Bearer PAT (Confluence 7.9+). Cloud spaces must use v2 `GET /wiki/api/v2/spaces`
  (cursor, limit ≤ 250, rows carry `id,key,name`).
- `myself()` → `GET /rest/api/user/current` (works on DC; survived on Cloud as far as the
  spec shows). Resilience: on 404/410 from a CLOUD connection, fall back to
  `GET /api/v2/spaces` params `{"limit": 1}` purely as an auth check and return
  `{"display_name": "verified (identity API unavailable)", "email": None, "account_id": None}`.
  Any 401/403 still raises ClientError (auth failures stay loud).
- `all_spaces() -> tuple[list, str | None]` — deployment branch:
  - cloud: `GET /api/v2/spaces` params `{"limit": 250}`; follow `_links.next` (relative
    URL — see the next-link guard below) until absent; rows → `{"key","name","id"}`.
  - dc: `GET /rest/api/space` params `{"start": n, "limit": 50}`; advance until short
    page or `_links.next` absent; same normalized rows.
  - Non-200 → `([], f"ERR{st}:...")` like all_projects (enumeration failure is loud).
- `count_pages(space_key) -> int | str` — `GET /rest/api/search` params
  `{"cql": f'space="{space_key}" and type=page', "limit": 1}` → `d.get("totalSize")`;
  error → `f"ERR{st}"`. (limit=1 not 0: limit=0 is schema-valid but runtime-unverified.)
  Works identically on both deployments under their api_base. Do NOT use
  `content/search` for counting — its envelope has no totalSize.
- `pages(space_key, page_size=50) -> Iterator[dict]` — one v1-content-shaped iterator,
  deployment only changes the FIRST request:
  - cloud: `GET /rest/api/content/search` params `{"cql": f'space="{space_key}" and type=page',
    "expand": _EXPAND, "limit": page_size}`
  - dc: `GET /rest/api/content` params `{"spaceKey": space_key, "type": "page",
    "status": "current", "expand": _EXPAND, "limit": page_size}`
  - `_EXPAND = "body.storage,version,history,ancestors,metadata.labels,children.comment,children.attachment"`
  - Both return `{results, ..., _links}`: yield results, then follow `_links["next"]`
    (a RELATIVE url with query) until absent. **Next-link guard:** if the next link
    starts with `/wiki/` and `self.api_base` already ends with `/wiki`, strip the
    leading `/wiki` before requesting (defends against context-vs-site-relative links).
    Split the next link into path + params (`urllib.parse.urlsplit` + keep query as
    string via `params=None`, path=`path?query` is NOT how req works — pass
    `path=split.path` relative to api_base minus its origin, `params=dict(parse_qsl(split.query))`).
  - Raise ClientError on non-200 mid-iteration (NEVER silently truncate — extraction
    verification depends on it).

**Risk guard (Cloud expand behavior is spec-verified but not runtime-verified):** Task 11's
`extract_space` adds an empty-body tripwire — see Task 11.

**Tests (MockTransport):** `mk_conf(handler, deployment="cloud")` factory.
- `test_cloud_base_has_wiki_prefix_dc_does_not` — handler records URLs; myself() hits `https://acme.atlassian.net/wiki/rest/api/user/current` (cloud) vs `https://confluence.acme.example/rest/api/user/current` (dc).
- `test_cloud_myself_falls_back_to_v2_auth_check` — user/current → 410, /api/v2/spaces → 200 → identity dict with placeholder display_name; user/current → 401 raises ClientError.
- `test_all_spaces_cloud_v2_cursor` — /wiki/api/v2/spaces two cursor pages via `_links.next` → all rows normalized {key,name,id}.
- `test_all_spaces_dc_v1_start_limit` — /rest/api/space two pages → normalized rows.
- `test_count_pages_total_size` — /rest/api/search with cql + limit=1 → totalSize int on both deployments.
- `test_pages_cloud_uses_content_search_cql` — first request path is /wiki/rest/api/content/search with cql param; follows `_links.next` cursor; yields all results.
- `test_pages_dc_uses_content_spacekey` — first request is /rest/api/content with spaceKey param.
- `test_pages_next_link_wiki_dedup` — a `_links.next` of `/wiki/rest/api/content/search?cursor=x` on a cloud client does NOT produce `/wiki/wiki/...` in the requested URL.
- `test_pages_raises_mid_loop` — page 1 ok, page 2 → 500 (after retries) raises ClientError.
- `test_dc_pat_bearer_header`.

- [ ] Failing → implement → green → full suite → commit `feat(confluence): client with space/page enumeration on cloud and DC`

---

### Task 11: Confluence extract

**Files:**
- Create: `auditor/confluence/extract.py`
- Test: `tests/test_confluence_extract.py` (new)

**Behavior:** mirrors `auditor/extract.py` conventions exactly (gzip JSONL, tmp+atomic rename, count verification, progress callback).

```python
MACRO_RE = re.compile(r'<ac:structured-macro[^>]*\bac:name="([^"]+)"')

def slim_page(page: dict) -> dict:
    # identity: exact title (current pages are title-unique per space)
    body = ((page.get("body") or {}).get("storage") or {}).get("value") or ""
    display = storage_text(body)
    ancestors = page.get("ancestors") or []
    parent = (ancestors[-1].get("title") if ancestors else None)
    hist = page.get("history") or {}
    labels = sorted(l.get("name") for l in
                    ((page.get("metadata") or {}).get("labels") or {}).get("results", [])
                    if l.get("name"))
    catt = (page.get("children") or {}).get("attachment") or {}
    att_results = catt.get("results") or []
    att_capped = bool((catt.get("_links") or {}).get("next")) or \
        (catt.get("size") is not None and len(att_results) < catt.get("size", 0))
    ccom = (page.get("children") or {}).get("comment") or {}
    com_results = ccom.get("results") or []
    com_capped = bool((ccom.get("_links") or {}).get("next"))
    return {"key": page.get("title"), "id": page.get("id"), "fields": {
        "title": page.get("title"),
        "parent": parent,
        "created": norm_ts(hist.get("createdDate")),
        "creator": ((hist.get("createdBy") or {}).get("displayName")),
        "version": (page.get("version") or {}).get("number"),
        "labels": labels,
        "body": {"len": len(display), "sha": content_fp(display),
                 "head": display[:200]},
        "attachment": {"capped": att_capped, "items": [
            {"filename": a.get("title"),
             "size": ((a.get("extensions") or {}).get("fileSize"))}
            for a in att_results]},
        "comment": {"count": len(com_results), "capped": com_capped},
        "macros": dict(Counter(MACRO_RE.findall(body))),
    }}

def extract_space(client, space_key, out_path, progress=None) -> dict
```

`extract_space` writes one slim page per line keyed by title; counts; verifies against `client.count_pages(space_key)`; returns `{"extracted", "approx", "verified"}` (identical contract to extract_project).

**Empty-body tripwire (risk guard for Cloud expand behavior):** after extraction, if
`extracted >= 10` and EVERY page's body len is 0, raise
`RuntimeError(f"{space_key}: body.storage expansion returned no content for any of "
f"{n} pages — the content API's expand behavior has changed; refusing to fingerprint")`.
A silently body-less extract would otherwise compare as a mass false content_mismatch
(or worse, a false CLEAN against an equally empty side). Threshold 10 avoids
false-tripping tiny stub spaces.

**Tests:**
- `test_slim_page_full_shape` — a synthetic v1 page JSON (Acme space) → exact dict assert (title key, parent from last ancestor, normalized created, labels sorted, body sha == content_fp(storage_text(body)), macros counted `{"toc": 1, "jira": 2}`).
- `test_attachment_capped_detection` — `_links.next` present → capped True.
- `test_extract_space_gz_and_verification` — mock client yielding 3 pages, count_pages 3 → verified True; count 4 → verified False.
- `test_extract_space_empty_body_tripwire` — 12 pages all with empty storage bodies → RuntimeError mentioning "expand"; 12 pages where ONE has a body → no error; 3 empty-bodied pages (under threshold) → no error.

- [ ] Failing → implement → green → full suite → commit `feat(confluence): page extraction with storage fingerprints and macro inventory`

---

### Task 12: Confluence compare

**Files:**
- Create: `auditor/confluence/compare.py`
- Test: `tests/test_confluence_compare.py` (new)

**Behavior:** `compare_space(space: str, src_path: str, tgt_path: str, cross_dialect: bool = False) -> dict` — same return contract as `compare_project` (stats keys IDENTICAL: project/src/tgt/common/missing_in_tgt/missing_in_src/tails/collisions/issues_with_mismatches/comments_uncheckable/fidelity_pct/severity_totals/field_mismatch_counts/remap/unmapped_users/distinct_src_people — fill remap/unmapped_users with empty shapes `{}` / `[]`, distinct_src_people from creator names).

- Presence: title-key sets. Cutover line (R6): `cut = max(norm created over common pages on the SOURCE side)` as float epoch; src-only page with `created > cut` → `tail_post_cutover` (direction source); tgt-only with `created > cut` → tail (direction target); else missing_in_tgt / missing_in_src. No common pages → no tails (all genuine), mirroring jira's has_overlap.
- Collision: common title where `created` differs AND `creator` differs → `key_collision` (skip field diffs for it).
- CONF_SPECS:

```python
CONF_SPECS = [
    ("parent",  lambda f: f.get("parent"), "high"),
    ("created", lambda f: f.get("created"), "high"),
    ("creator", lambda f: f.get("creator"), "high"),
    ("version", lambda f: f.get("version"), "low"),
    ("labels",  lambda f: sorted(f.get("labels") or []), "med"),
]
```

- Body: sha diff → `content_mismatch` (field "body", sev high, src_len/tgt_len detail; cross_dialect flag passthrough like Task 5).
- Attachments: if EITHER side capped → one `attachment_uncheckable` advisory finding (kind `attachment_uncheckable`, does not touch mismatch sets, counted in stats as `attachments_uncheckable`); else set-compare filename|size → `attachment_mismatch` (sev high).
- Comments: counts equal → nothing; differ and neither capped → `comment_mismatch` (sev high, src_total/tgt_total detail); either capped → `comment_uncheckable` advisory (reuses the existing kind + stats key `comments_uncheckable`).
- Findings carry `"project": space` and `src_key`/`tgt_key` = title (the store columns are TEXT; the UI links via title — fine).

**Tests** (gz fixture helper local to the module, synthetic Acme/Globex):
- `test_presence_and_timestamp_tails` — common pages w/ max created T; src-only created T+10 → tail; src-only created T-10 → missing_in_tgt; no-overlap case → all missing.
- `test_collision_same_title_different_page`.
- `test_field_and_body_mismatches` — parent/labels/version diffs counted; body sha diff → content_mismatch; fidelity computed as (common - mismatched)/common.
- `test_uncheckable_advisories_do_not_dent_fidelity` — capped attachments + capped comments → fidelity 100, advisory kinds present, stats counters set.
- `test_stats_contract_keys_match_jira` — `set(stats) == set(jira_compare_stats_keys)` (import compare_project, run it on a 1-issue fixture, compare key sets — this is the firewall regression test).

- [ ] Failing → implement → green → full suite → commit `feat(confluence): page comparison with timestamp cutover and uncheckable advisories`

---

### Task 13: Confluence macro audit + connector registration

**Files:**
- Create: `auditor/confluence/macros.py`
- Modify: `auditor/connectors.py` (register confluence), `auditor/aggregate.py` (skip `attachment_uncheckable`)
- Test: `tests/test_macros.py` (new), `tests/test_connectors.py` + `tests/test_aggregate.py` (extend)

**Behavior:**
- `audit_macros(workspace: str, spaces: list[str], progress=None) -> {"areas", "findings"}` — reads `{workspace}/src/{space}.core.jsonl.gz` and tgt counterparts, sums per-page `fields["macros"]` counters per side. Area: `areas["macros"] = {"label": "macros", "src": total_distinct_src, "tgt": total_distinct_tgt, "in_both": n, "source_only": [names], "target_only_count": n, "target_only": [names], "by_macro": {name: {"src": n, "tgt": n}}}`. Findings: macro with src>0, tgt==0 → `{"area": "macros", "name": name, "kind": "missing_in_tgt", "detail": {"src_occurrences": n}}`; macro with 0 < tgt < src → `{"kind": "count_mismatch", "detail": {"src": n, "tgt": n}}`.
- Confluence connector: labels space/page; `supports_blind_spots=False`, `supports_elevation=False`; `make_client=ConfluenceClient`; verify → `/user/current` shaped to the same identity dict (displayName/email may be absent on DC → .get); `list_containers=all_spaces`; `count_items=count_pages`; `extract=extract_space`; `compare=compare_space`; `audit_config=lambda src, tgt, containers, workspace, progress: audit_macros(workspace, containers, progress)`; `browse_url=lambda site, space, title: f"{site.rstrip('/')}/wiki/display/{space}/{quote(title)}"` (dc: no /wiki — take deployment? browse_url receives the conn row's site_url only; ACCEPT the /display/ path on both: cloud redirects /display → /wiki/display? NO — implement `browse_url(site, container, item, deployment="cloud")` with /wiki prefix only for cloud).
- `aggregate.py`: the kind-skip tuple in `derive_fidelity` gains `"attachment_uncheckable"`. Add same to `webapp/analysis.py`'s advisory handling if any (check `_KIND_BREAKDOWN_KEYS` — leave as-is; it lists mismatch kinds only).

**Tests:**
- `test_audit_macros_missing_and_drop` — fixtures with src macros {jira: 5, toc: 2, vendor-macro: 3}, tgt {jira: 5, toc: 1} → vendor-macro missing_in_tgt, toc count_mismatch, jira clean.
- `test_confluence_connector_registered` — get_connector("confluence").item_label == "page".
- `test_attachment_uncheckable_skipped_in_fidelity` (test_aggregate.py) — a finding of that kind does not enter core mismatches.

- [ ] Failing → implement → green → full suite → commit `feat(confluence): macro inventory audit + connector registration`

---

### Task 14: Findings vocabulary + analysis product passthrough

**Files:**
- Modify: `auditor/findings.py`, `webapp/runs.py` (pass labels), `webapp/analysis.py`
- Test: `tests/test_findings.py`, `tests/test_analysis.py` (extend)

**Behavior:**
- `build_run_summary(project_results, config_result, blind_spots, item_label="issue", container_label="project")` — every headline string substitutes the labels (e.g. `f"{mismatched} migrated {item_label}(s) have at least one field or content difference."`). Stats KEYS unchanged.
- NEW advisory passthrough (carry-forward from Task 12/13): aggregate `attachments_uncheckable` from project stats (confluence emits it; jira stats lack the key — use `.get(..., 0)`), add it to the stats block, and give it an advisory headline mirroring comments_uncheckable: `f"{n} {item_label}(s) had more attachments than the API returns inline; their attachment sets could not be fully verified."` — appended in the advisory section (before the clean-migration fallback).
- `RunEngine._execute` finalize block: labels come from ctx — `summary = build_run_summary(..., item_label=ctx.get("item_label", "issue"), container_label=ctx.get("container_label", "project"))`; `stage_verify` sets `ctx["item_label"]/["container_label"]` from the connector.
- `analysis.py` summary endpoint: load the run's migration (`store.get_migration(run["migration_id"])`), add to the response: `"product"`, `"container_label"`, `"item_label"` (via get_connector). `/api/runs/{id}/projects` response rows unchanged (labels come from summary).

**Tests:**
- `test_headlines_use_item_label` — confluence labels → headline contains "page(s)" not "issue(s)".
- `test_summary_includes_product_and_labels` — API summary for a run whose migration is confluence → product confluence, labels space/page.
- Engine test: finalize uses ctx labels (extend an existing runs/pipeline test minimally).

- [ ] Failing → implement → green → full suite → commit `feat(analysis): product vocabulary through summary and headlines`

---

### Task 15: Templates + app.js vocabulary

**Files:**
- Modify: `webapp/templates/index.html`, `migration.html`, `run.html`, `analysis.html`, `webapp/static/app.js`
- Test: `tests/test_main.py` (template assertions)

**Behavior:**
- `index.html`: create-migration form gains `<select name="product">` (Jira / Confluence, default Jira); each migration card shows a product chip (`{{ m.product }}`; style like the existing site chips); the tagline drops the hardcoded "Jira Cloud → Cloud" for "Audit an Atlassian migration end to end — Jira or Confluence, Cloud or Data Center source."
- `migration.html`: connection form gains `<select name="deployment">` (Cloud / Data Center); email input is wrapped with a hint and JS toggle: when deployment=dc, hide email (and drop required); placeholder text adapts (e.g. `jira.acme.example` for DC). Keep PAT-only flow. Project picker copy says `{{ 'spaces' if mig.product == 'confluence' else 'projects' }}` where applicable.
- `run.html`: phase list labels via product (`Extract pages` for confluence) — template conditional on `mig.product`.
- `analysis.html`: add `data-product="{{ product }}"` (plus existing data attrs).
- `app.js`: add at top:

```javascript
const VOCAB = {
  jira:       {container: 'Project', containers: 'Projects', item: 'issue', items: 'issues', itemTitle: 'Issue'},
  confluence: {container: 'Space',   containers: 'Spaces',   item: 'page',  items: 'pages',  itemTitle: 'Page'},
};
let vocab = VOCAB.jira;   // reassigned at boot from data-product / summary.product
```

  — heat table headers, KPI tile labels, tab label, finding prose, drill-down strings switch to `vocab.*`. Initialize from `document.body.dataset.product || summary.product`. Keep diffs surgical; do not restyle.

**Tests:** (server-rendered assertions only — no JS runner exists)
- `test_index_has_product_select_and_chip`.
- `test_migration_page_has_deployment_select`.
- `test_analysis_page_carries_data_product` — create confluence migration + run row, GET analysis page → `data-product="confluence"` present.

- [ ] Failing → implement → green → full suite → commit `feat(ui): product/deployment forms and vocabulary`

---

### Task 16: Docs + end-to-end smoke

**Files:**
- Modify: `README.md` (supported matrix table, DC PAT instructions, Confluence notes, known limitations from spec §3/§5)
- Test: `tests/test_stages_pipeline.py` (extend with one full confluence pipeline test)

**Behavior:**
- README gains a "Supported migrations" matrix (Jira C2C ✅, Jira DC2C ✅ issue fidelity + partial config, Confluence C2C ✅, Confluence DC2C ✅), auth instructions per deployment, the reuse_extracts_from version caveat, and the Confluence current-pages/macro-audit scope notes.
- Pipeline test: MockTransport serving a tiny Confluence site pair (1 space, 2 pages, 1 macro gap) through `create_app` + real RunEngine → run completes, verdict GAPS_FOUND, run_projects row has the space, summary API returns product confluence labels. (Mirror the existing jira pipeline test structure.)

- [ ] Failing → implement → green → full suite → commit `docs+test: supported-migrations matrix and confluence e2e pipeline`

---

## Review gates (after Tasks 9 and 16)

Two parallel reviewers each gate:
1. **Spec compliance** — re-read the spec, diff the branch, verify every R# requirement maps to code+tests; hunt for silent capability lies (a skipped area rendered as clean).
2. **Code quality** — bugs, dead code, convention drift, test quality (assertions that can't fail, mocks that don't assert URLs).

Findings → fix tasks before proceeding. After Task 16: full stress audit (fresh-context adversarial subagents, non-overlapping concerns), then push branch + open PR on `Ig0rmed3iros/migration-auditor` (push via git HTTPS; PR via curl with the api.github.com --resolve workaround).

## Self-review checklist (author)

- Spec coverage: R1→T1/T9, R2→T1/T3/T9, R3→T2/T3/T4/T5/T8, R4→T6, R5→T10-12, R6→T12, R7→T13, R8→T12(stats contract)/T13/T14, R9→T7/T8/T9, R10→T1/T8, R11→T3/T10/T11, R12→T15. ✅
- Type consistency: `build_clients` 3-tuple (T8) matches T9 call sites; connector field names consistent across T7/T8/T13; `content_fp`/`norm_ts` used in T4/T11/T12 as defined in T2. ✅
- No placeholders: research-dependent items are confined to Task 10's bracketed contingency note (resolved before execution). ✅
