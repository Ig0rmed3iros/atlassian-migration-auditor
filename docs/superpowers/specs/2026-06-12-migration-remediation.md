# Migration Remediation: capability-honest fixing of detected defects

**Status:** approved for build (user directives captured below)
**Date:** 2026-06-12
**Builds on:** the multi-product connector engine (`2026-06-11-multi-product-connectors.md`). The audit side — scope, extract, compare, config parity, findings, verdict, Store, RunEngine, analysis API, Dark Console UI — is the input to this feature and stays behaviourally unchanged except for the additive payload capture in §4.1–§4.2.

## 1. Problem

The auditor *detects* migration defects but cannot *act* on them. The operator
reads the verdict, then fixes everything by hand in two consoles. The goal is
to let the app fix the defects it can fix faithfully — via the target's REST
API — and, for the defects no API can fix faithfully, hand the operator the
exact re-migration work list instead of a vague "gap found."

The scope is **any migration defect**, not just custom fields: missing config
objects (fields, options, statuses, priorities, resolutions, issue types, link
types, screens), missing field *values*, mis-wired config (a field not on a
screen, a status not in a workflow), missing **tickets**, and missing
**users**. Each defect class is handled at the highest level of honesty the
platform allows — never by fabricating a record.

## 2. Guiding decisions (from brainstorming)

- **Single scan, not double.** The audit gathers the full remediation payload
  for every fixable finding *during the audit run*, persists it on the
  finding, and the fixer consumes it. The audit UI keeps showing exactly the
  slim report it shows today. No second scan of the environment at fix time.
- **Capability honesty is the product.** Every defect is classified into a
  fix **tier**; the engine auto-fixes only what it can recreate faithfully
  (zero provenance loss), and for the rest it produces a precise
  detect-and-guide artifact. Missing tickets and missing users are
  detect-and-guide — never API-recreated — because created-date, reporter,
  comment authorship and history are immutable via REST and Cloud users live
  on a separate identity plane.
- **Granular, escalating consent.** A dedicated **Fix options** screen. Every
  fix is an independent checkbox carrying its own disclaimer. A fix decomposes
  into up to three separately-consented tiers of rising risk: **create**
  (safe, recreates a definition), **wire** (changes target behaviour — link
  field→screen/issue-type, link status+transitions→workflow), **populate**
  (rewrites issue metadata — sets field values, moves the Updated date).
- **Target-only, forward-only, proven.** Writes touch the target connection
  only, ever (asserted in code). v1 never auto-deletes created config. Closure
  is proven by re-auditing the touched scope, never assumed from a 200.

## 3. Requirements

### Audit-side (additive capture)

- **R1 — single-scan payload capture.** During the audit, for every finding
  the registry marks fixable, the audit captures the **full source-object
  definition** needed to recreate it (e.g. a missing custom field's type,
  searcher, contexts, options and screen placements) and persists it as
  `fix_payload` on the finding. Bounded to *findings only* — never the whole
  instance. Never surfaced in the audit UI.
- **R2 — bounded value capture (Option B).** Field *values* cannot be captured
  in the `extract` phase because the set of missing fields is unknown until
  `config` parity runs. After parity identifies the missing custom fields, a
  gated final audit step captures those specific fields' per-issue source
  values (keyed by issue key) into the workspace. Gated by a run param
  (`capture_remediation`, default on); a lean pure-audit run can disable it and
  store no values.
- **R3 — user-gap detection (new, bounded).** The audit surfaces distinct user
  identities referenced by audited issues (reporter, assignee) that are present
  on the source but do not resolve on the target, as Tier-2 `user_gap`
  findings. Detection only — no auto-fix (R8 tier).

### Remediation engine

- **R4 — fix registry / capability tiers.** A registry maps
  `(product, finding kind, object type)` → a **Fix descriptor**:
  `fix_id`, `tier ∈ {create, wire, populate}` (or `guide` for Tier-2),
  `risk ∈ {low, medium, high}`, `label`, `disclaimer`, an `applies_to`
  predicate, a `plan(finding, payload)` producing ordered target API-call
  descriptors, and an `apply(client, finding, payload, log)`. Tier-2 defects
  carry a `guidance(finding)` instead of plan/apply. The catalog is §4.4.
- **R5 — fix planner.** Given the operator's selected `fix_id`s plus the audit
  run's findings and payloads, build a `FixPlan`: an ordered list of
  `FixAction`s with dependencies resolved (create field → wire to screen →
  populate values; create status → wire into workflow). The plan is fully
  dry-run-renderable (every object it will create, every call it will make,
  with counts) before any write.
- **R6 — fix run.** Remediation executes as a RunEngine run of
  `kind="fix"` with phases `verify → apply → reaudit → finalize`, reusing the
  thread engine, event stream, SSE and Store. `apply` executes the FixPlan in
  dependency order and logs **every** API call (method, path, status, ok,
  created-id, finding-ref) to a `fix_actions` table — the elevation grant log
  generalized. Fail-loud per action; a failed action is recorded and the run
  continues (mirrors `apply_elevation`), never a silent skip.
- **R7 — proof by re-audit.** `reaudit` re-runs only the touched audit scope
  (the affected config areas; for value backfill, re-extract the touched
  fields/issues and re-compare) and the fix verdict is computed from it:
  `{closed, still_open, failed}`. A 200 on the write is never sufficient to
  call a finding closed.
- **R8 — Tier-2 detect-and-guide.** Missing tickets/pages (holes below the
  cutover line), missing users, key collisions, full-workflow rebuilds, and
  app-backed macros are **never** API-recreated. For each, the engine emits a
  precise guidance artifact: the exact missing keys / unmapped users, a
  copyable JQL/CQL selection, the recommended JCMA/CCMA re-migration scope, and
  a "then re-audit to confirm" instruction.
- **R9 — target-only & idempotent.** Writes only ever flow through the single
  target client passed by `fix_apply`. Two independent guards enforce this:
  (a) `apply_plan` checks `tgt_client.api_base == expected_api_base` at the
  call boundary so a mis-wired caller (wrong client object) raises before any
  HTTP; (b) the planner-level `side='target'` field on every `FixAction` guards
  against a future planner regression emitting a source-side action — it does
  NOT verify the client identity at runtime (that is guard (a)'s job). A write
  to the source side is a programming error that raises before any HTTP.
  Applying a fix whose object already exists on the target is a pre-checked
  no-op (mirrors elevation's already-member pre-check), so re-running a fix run
  is safe.
- **R10 — forward-only.** v1 does not auto-delete or roll back created config.
  The `fix_actions` log records every created object id so an operator can undo
  manually; this is documented, not automated (deleting config that may already
  hold data is more dangerous than the original gap).

### Confluence & cross-product

- **R11 — Confluence remediation.** Page-label backfill (`add label`) is the
  one faithful auto-fix (create tier). Missing macros (need the Cloud app),
  missing pages (re-migrate), and body/version differences are Tier-2
  detect-and-guide.

### Consent & UI

- **R12 — consent model.** No fix run starts without at least one explicitly
  selected `fix_id`. `create`, `wire` and `populate` are independent consents:
  ticking create never wires or populates. Workflow wiring (the only `high`
  risk fix that mutates live transition behaviour) requires an additional
  explicit confirmation beyond its checkbox.
- **R13 — UI.** Each completed audit run's analysis page gains a **Fix options**
  button. The Fix options screen lists fixable findings grouped by `fix_id`
  with per-fix checkboxes + disclaimers and a live **dry-run plan preview**
  (objects to create, calls to make, issues to touch). A separate read-only
  **Detect & re-migrate** section renders the Tier-2 guidance with copyable
  JQL/keys. Submitting starts a fix run; a fix-run results page shows the
  action log (with created ids and any failures) and the closure verdict, using
  the run's product vocabulary.
- **R14 — Store & back-compat.** Schema gains `runs.kind` (default `'audit'`),
  `runs.source_run_id` (the audit a fix remediates), `fix_payload` on the
  finding tables, and a `fix_actions` table. Existing `auditor.db` upgrades in
  place via `_migrate()` ALTER TABLE. The existing suite stays green. An audit
  run recorded before this feature has no payloads: its fixable findings render
  on the Fix screen as "re-run the audit to capture fix data," not as broken.

## 4. Architecture

### 4.1 Payload capture during audit — `auditor/remediation/payload.py`

`capture_config_payload(src_client, finding)` is called from `stage_config`
for each fixable config finding and returns the full source definition:

- custom field → `{type, searcher, contexts:[{name, options:[...], projectIds,
  issueTypeIds}], screens:[screen names the field sits on]}`
- status → `{name, statusCategory}`; priority/resolution → `{name, description,
  iconUrl?}`; issue type → `{name, description, hierarchyLevel, subtask}`;
  link type → `{name, inward, outward}`
- screen → `{name, description, tabs:[{name, fields:[field names]}]}`

The payload is attached to the finding dict (`finding["fix_payload"]`) and
persisted by `insert_findings_config`. This reuses the read helpers the audit
already has (`_field_options`, `_screen_fields`) plus a few new read calls;
it is gated by `capture_remediation` so a lean audit skips it.

### 4.2 Value capture — `auditor/remediation/values.py`

After `stage_config`, a new `stage_capture_values(ctx)` runs only when
`capture_remediation` is set and there is ≥1 missing custom-field finding. For
each missing field it re-reads that single field across the audited issues on
the source (a *targeted* read — one field over the issue population, not a
re-scan of the environment) and writes
`workspace/fix/values/{safe_field_name}.jsonl.gz` of `{issue_key, value}`.
Bounded to missing fields; absent when disabled.

### 4.3 Registry — `auditor/remediation/registry.py`

`FIXES: list[Fix]` and `fixes_for(product, finding)` → the applicable
descriptors. Each `Fix` is a frozen dataclass (R4 fields). The registry is the
only place that knows *which* defects are fixable and *how*; planner, applier,
guidance and UI all read it, so adding a fix is one entry.

### 4.4 Capability catalog (v1)

**Tier 1 — auto-fix (Jira):**

| Finding | create | wire (opt-in) | populate (opt-in) | risk |
|---|---|---|---|---|
| custom field `missing_in_tgt` | field + contexts + options | link to screens; link to issue types / field configs | populate field values | create=low, wire=med, populate=med |
| field `option_mismatch` | add missing options | — | — | low |
| status `missing_in_tgt` | create status (+category) | link status + transitions into workflow | — | create=low, wire=**high** |
| priority / resolution `missing_in_tgt` | create | — | — | low |
| issue type `missing_in_tgt` | create | add to project issue-type scheme | — | create=low, wire=med |
| link type `missing_in_tgt` | create | — | — | low |
| screen `missing_in_tgt` | create + tabs/fields | wire into screen scheme / issue-type screen scheme | — | create=low, wire=med |
| screen `field_mismatch` | — | add missing fields to the screen | — | med |

**Tier 1 — auto-fix (Confluence):** page label `missing_in_tgt` → add label (create, low).

**Tier 2 — detect & guide (no API recreation):** missing issues/pages below
cutover; `user_gap`; key collisions; workflow `structure_mismatch` where the
workflow must be *built* (only wiring into an existing workflow is Tier-1
wire); app-backed macros (`missing_in_tgt` in area `macros`);
created-date/reporter/authorship.

### 4.5 Planner — `auditor/remediation/plan.py`

`build_plan(findings, selected_fix_ids, payloads)` → `FixPlan` of ordered
`FixAction`s. Dependency rules: a `wire`/`populate` action for an object is
ordered after that object's `create` action (whether the create is in this plan
or already satisfied on target). `dry_run_preview(plan)` → the structure the UI
renders before consent. No client calls — pure.

### 4.6 Applier — `auditor/remediation/apply.py`

`apply_plan(tgt_client, plan, log_sink)` executes each action, asserting
`tgt_client` is the target. Per action: pre-check existence (no-op + log if
present), else perform the write, log `{finding_ref, fix_id, object, method,
path, status, ok, created_id, error?}`. New client write methods land on
`JiraClient` / `ConfluenceClient`: `create_field`, `create_field_context`,
`add_field_options`, `add_field_to_screen`, `create_status`, `create_priority`,
`create_resolution`, `create_issue_type`, `create_link_type`, `create_screen`,
`add_screen_tab`, `add_screen_field`, `add_status_to_workflow`,
`set_issue_fields`, `add_page_label`.

### 4.7 Guidance — `auditor/remediation/guidance.py`

`guidance_for(finding)` → `{summary, why_unfixable, missing:[...],
selection_query, remigration_scope, next_step}` for each Tier-2 kind. Pure;
consumed by the UI's Detect & re-migrate section and copyable as text.

### 4.8 Re-audit & fix run — `webapp/runs.py`, `webapp/fix_stages.py`

`RunEngine` gains a `kind`-parameterized phase list and finalize builder.
`kind="fix"` runs `verify → apply → reaudit → finalize` with stages injected
like the audit stages:
- `fix_verify` — re-auth both sides; assert target write capability.
- `fix_apply` — build the plan from the run's selected fix_ids + the source
  audit run's findings/payloads; populate actions read the captured values from
  the **source audit run's** workspace (`source_run_id` → its `fix/values/`);
  `apply_plan`; persist `fix_actions`.
- `fix_reaudit` — re-run the touched audit scope (affected config areas; for
  populate, re-extract touched fields/issues and re-compare); compute closure.
- `finalize` — fix verdict `{closed, still_open, failed}` + headlines; persist.

### 4.9 Store — `webapp/store.py`

`runs` + `kind TEXT NOT NULL DEFAULT 'audit'`, `source_run_id INTEGER NULL`.
`findings_config` / `findings_issue` + `fix_payload TEXT NULL`. New
`fix_actions(id, run_id, finding_ref, fix_id, object_name, method, path,
status, ok, created_id, error)`. `_migrate()` adds each via idempotent ALTER
TABLE (constant defaults backfill existing rows).

### 4.10 Webapp & UI — `webapp/remediate.py`, templates, `fix.js`

- `GET /runs/{run_id}/fix` — the Fix options screen (Tier-1 checkboxes grouped
  by fix with disclaimers + dry-run preview; Tier-2 Detect & re-migrate
  section). Findings without payloads → "re-run audit to capture fix data."
- `POST /runs/{run_id}/fix` — validate ≥1 fix_id and the workflow-wire confirm
  flag; start a `kind="fix"` run with `source_run_id = run_id`; redirect.
- `GET /fix-runs/{id}` — action log + closure verdict; SSE reuse.
- Analysis page gains the **Fix options** button. `fix.js` renders tier
  checkboxes, escalating disclaimers, the live plan preview, and copy buttons
  for Tier-2 artifacts.

## 5. Risks & mitigations

| Risk | Mitigation |
|---|---|
| A fix writes to the source by mistake | `apply_plan` asserts `client is tgt`; every write method is only ever handed the target client; unit test proves a source-targeted apply raises |
| Workflow wiring breaks a live workflow | Highest-risk fix; separate `high` risk class, loudest disclaimer, extra confirm flag (R12); pre-check that the status/transition is genuinely absent before editing; documented as the one fix to apply in a maintenance window |
| Operator thinks a 200 = fixed | Closure is computed only by `fix_reaudit`; the verdict surfaces `still_open` and `failed` explicitly |
| Re-running a fix double-creates objects | Idempotent pre-check per action (R9); existing object → logged no-op |
| Captured payload is stale vs current target | Audit and fix are usually minutes apart and the source is frozen; pre-check reconciles against the live target at apply time; re-audit catches any drift |
| Value capture stores full customer values at rest | Bounded to missing fields, gated by `capture_remediation`, lives under the gitignored `MA_DATA_DIR` workspace (same class as existing extracts), documented |
| Old audit runs have no payloads | Fix screen degrades gracefully: those fixable findings prompt a re-audit, never error |
| Missing-users expectation (operator expects auto-fix) | UI states the API limit plainly in the Detect & re-migrate section; invite-via-org-admin is a documented fast-follow needing a separate org-admin token |

## 6. Non-goals (v1)

- Automated rollback / deletion of created config (forward-only; manual undo
  via the action log).
- Creating Cloud **users** (org-admin/SCIM; documented fast-follow, separate
  token scope).
- Recreating missing **issues/pages** via API (forbidden by design —
  re-migrate the delta).
- Building a workflow that does not exist on the target (only wiring missing
  statuses/transitions into an *existing* workflow is attempted).
- Reassigning historical authorship / created-date (immutable via REST).
- Scheme creation where the source side is DC (those areas are audit-skipped —
  nothing to remediate).
- Confluence content re-push / version-bump fixes (Tier-2 guide only).

## 7. Acceptance

1. Full pytest suite green (existing + new): registry, payload capture, value
   capture bounding, planner dependency ordering, applier logging +
   target-only assertion + idempotent no-op, re-audit closure, Tier-2 guidance,
   Store upgrade, and the fix-screen / fix-run routes.
2. Simulated Jira Cloud→Cloud fix run (MockTransport end-to-end through
   RunEngine): a missing custom field is created with its context + options;
   with the wire box ticked it is linked to a screen; with populate ticked the
   captured values are set; `fix_reaudit` reports the finding **closed**; the
   action log is complete; **nothing** is written to the source.
3. Consent enforcement: a POST with no selected fix_id is rejected; create
   without wire leaves the object unwired; workflow-wire without the confirm
   flag is rejected.
4. Tier-2: a run with holes below the cutover line yields a missing-keys list +
   JQL + re-migration scope and creates **zero** issues; a `user_gap` finding
   renders invite/re-migrate guidance.
5. Idempotency: re-running the same fix run performs only logged no-ops.
6. Back-compat: a pre-remediation `auditor.db` opens, existing audit runs
   render unchanged, and a pre-feature audit run's fixable findings show the
   "re-run audit to capture fix data" prompt.
