"""AI environment assessment (spec R5). Metadata-only outbound boundary."""
from __future__ import annotations
import json
import math
import os
import re

from auditor.envaudit._pool import map_results

# A real 187-project instance (1028 components, 3883 versions, ~1600 findings)
# sent so much per-OBJECT metadata that the AI prompt argv exceeded the OS limit
# and an OpenAI-compatible claude-bridge CLI failed with `spawnSync claude E2BIG`.
# The fix keeps the metadata-only privacy boundary but BOUNDS the payload: large
# per-object lists become AGGREGATE counts, every forwarded `names` list is capped
# (the full `count` already conveys scale), and the finding sample is capped.
#
# We send COMPLETE by-kind / by-severity finding counts (cheap, full signal of
# scale) plus a bounded, severity-prioritised SAMPLE of at most this many findings
# so the most important findings are always represented.
#
# NOTE: the original 40 caps were defensive against an E2BIG-broken proxy (argv
# limit). The local `claude -p` path pipes on STDIN (no argv limit), and measured
# latency is ~independent of prompt size (12KB→93s vs 45KB→80s) — so starving the
# model of context only made the analysis SHALLOW for no speed gain. The caps are
# now generous; scale is still always conveyed by the full `count`.
_AI_FINDING_CAP = 150

# Cap for any forwarded `names` list (statuses, custom_fields, screens, groups,
# boards, issuetype_schemes, projects_using, principal/operation type tokens…).
# The full `count` is always forwarded alongside, so scale is never lost.
_AI_NAME_CAP = 80

# Per-section per-kind example cap (sectioned/map-reduce path): each section gets
# the COMPLETE per-kind finding counts for its areas plus this many example names.
_AI_SECTION_EXAMPLE_CAP = 30

# permission_scheme_grants caps: forward at most this many schemes, and at most
# this many grants per scheme. Permission-check overhead and scheme bloat are
# fully conveyed by the count + a representative slice; the full list is bloat.
_AI_SCHEME_CAP = 30
_AI_GRANT_CAP = 50

# Severity rank for sample prioritisation (lower = more important, sampled first):
# high, then the attention-demanding 'warning' (area_error fetch failures), then
# medium, then low/info. Unknown/None severities sort last so they never crowd
# out a real high in the bounded sample.
_SEVERITY_ORDER = {"high": 0, "warning": 1, "medium": 2, "low": 3, "info": 4}


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Atlassian Cloud accountIds — a direct user identifier, never a legitimate
# object name, so masking can only help. Three forms in the wild:
#   classic   <numeric>:<uuid>          557058:f6c30b2e-...   (hex/dash suffix)
#   UUID      8-4-4-4-12 hex            f6c30b2e-5f1a-...
#   modern    24-char lowercase hex     5b10ac8d82e05b22cc7d4ef5  (post-GDPR; most common)
# The hex-suffix anchor on the classic form avoids masking a benign
# "Sprint 12345:retro". The modern 24-hex (longer than the 16-hex content shas)
# is the dominant live format and was the gap a colon-only regex missed.
_ACCOUNT_ID_RE = re.compile(
    r"\b\d{5,}:[0-9a-fA-F][0-9a-fA-F-]{7,}\b"
    r"|\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
    r"|\b[0-9a-f]{24}\b")


def _redact_pii(s):
    """Mask high-confidence PII embedded in object NAMES before they leave the
    machine in the AI payload — an admin may name a group/filter/field after a
    person ('john.smith@acme.com', 'Marketing - Priya'), and finding samples can
    carry an accountId. Emails and Atlassian accountIds are masked; the LOCAL
    report still shows the real name, only the OUTBOUND copy is masked."""
    if not isinstance(s, str):
        return s
    s = _EMAIL_RE.sub("<redacted-email>", s)
    return _ACCOUNT_ID_RE.sub("<redacted-account-id>", s)


def _safe_str_list(lst, cap=_AI_NAME_CAP):
    """Return first `cap` items from a list, only including strings, with emails
    redacted (the outbound AI payload must not carry PII embedded in names)."""
    if not isinstance(lst, list): return []
    return [_redact_pii(x) for x in lst[:cap] if isinstance(x, str)]


def summarize_for_ai(snap: dict, findings: list, product: str = "jira") -> dict:
    areas_out = {}
    for area, a in (snap.get("areas") or {}).items():
        if not isinstance(a, dict): continue
        # `names`: capped at _AI_NAME_CAP (examples for the AI); the full `count`
        # is always forwarded alongside, so the cap never hides the true scale.
        entry = {"count": a.get("count"),
                 "names": _safe_str_list(a.get("names") or [])}
        if a.get("skipped"): entry["skipped"] = True
        if a.get("capped") is not None:
            entry["capped"] = a["capped"]
        if a.get("by_type"):
            # Allowlist: forward only string type labels (field name -> type
            # string, which is all gather ever sets). A non-string value — a
            # future area stuffing structured/secret data into a by_type key —
            # is DROPPED, never stringified through, so the outbound surface
            # cannot silently widen.
            #
            # This map scales with instance size (one entry per custom field —
            # ~500+ on a large instance, a big chunk of the old E2BIG payload).
            # We forward a complete `by_type_counts` AGGREGATE (type -> how many
            # fields of that type; bounded by the ~few-dozen distinct field
            # types) for the full type distribution, plus a capped `by_type`
            # name->type SAMPLE for concrete examples.
            pairs = [(_redact_pii(str(k)), v) for k, v in a["by_type"].items()
                     if isinstance(v, str)]
            type_counts: dict = {}
            for _, t in pairs:
                type_counts[t] = type_counts.get(t, 0) + 1
            entry["by_type_counts"] = type_counts
            entry["by_type"] = dict(pairs[:_AI_NAME_CAP])
        if a.get("structure_checked") is not None:
            entry["structure_checked"] = a["structure_checked"]
        # workflow status/transition NAMES only (no values/bodies)
        if a.get("detail") and area == "workflows":
            entry["workflows"] = {k: {"statuses": v.get("statuses"),
                                      "transitions": v.get("transitions")}
                                  for k, v in a["detail"].items()}

        # --- Phase-A new areas (spec R4 / invariant I1) ---

        # groups: member_counts (integers — not identities) and capped flag
        if area == "groups":
            raw_mc = a.get("member_counts")
            if isinstance(raw_mc, dict):
                # Forward group-name → member-count integer pairs only.
                # The integer values are aggregate stats, NOT identities.
                entry["member_counts"] = {_redact_pii(str(k)): v
                                          for k, v in raw_mc.items()
                                          if isinstance(v, int)}
            # "members" key is intentionally NOT forwarded.

        # components: AGGREGATE counts only — the per-component list (1028 objects
        # on the real instance) was the bloat that blew the argv limit. We forward
        # {total, leaderless, unassigned_default, projects_with_components}
        # computed across all projects. Individual component NAMES are NOT
        # forwarded (the findings carry the specific names via the finding sample;
        # the AI needs scale, not the full list). NEVER: lead name, leadAccountId,
        # or any identity field.
        elif area == "components":
            raw_bp = a.get("by_project")
            if isinstance(raw_bp, dict):
                total = leaderless = unassigned = projects_with = 0
                for comps in raw_bp.values():
                    if not isinstance(comps, list):
                        continue
                    objs = [c for c in comps if isinstance(c, dict)]
                    if objs:
                        projects_with += 1
                    for comp in objs:
                        total += 1
                        if not bool(comp.get("has_lead")):
                            leaderless += 1
                        if str(comp.get("assignee_type", "")) == "UNASSIGNED":
                            unassigned += 1
                entry["aggregate"] = {
                    "total": total, "leaderless": leaderless,
                    "unassigned_default": unassigned,
                    "projects_with_components": projects_with}
            # The default-shape `names` placeholder is empty for components; drop
            # it so the AI sees only the aggregate (and the real `count`).
            entry.pop("names", None)

        # versions: AGGREGATE counts only — the per-version list (3883 objects on
        # the real instance) was the other half of the argv bloat. We forward
        # {total, overdue, archived_unreleased, released}. `overdue` counts
        # unreleased+overdue versions (matching the checks.py finding logic);
        # `archived_unreleased` counts archived+unreleased. No per-version NAMES.
        # NEVER: createdBy, releasedBy, releaser, releaseDate, or any identity.
        elif area == "versions":
            raw_bp = a.get("by_project")
            if isinstance(raw_bp, dict):
                total = overdue = archived_unrel = released = 0
                for vers in raw_bp.values():
                    if not isinstance(vers, list):
                        continue
                    for ver in vers:
                        if not isinstance(ver, dict):
                            continue
                        total += 1
                        is_released = bool(ver.get("released"))
                        if is_released:
                            released += 1
                        if bool(ver.get("overdue")) and not is_released:
                            overdue += 1
                        if bool(ver.get("archived")) and not is_released:
                            archived_unrel += 1
                entry["aggregate"] = {
                    "total": total, "overdue": overdue,
                    "archived_unreleased": archived_unrel, "released": released}
            entry.pop("names", None)

        # permission_scheme_grants: per-scheme list of {permission, holder_type}.
        # NEVER: holder value, holder parameter, or any identity/group ID.
        elif area == "permission_scheme_grants":
            raw_bs = a.get("by_scheme")
            if isinstance(raw_bs, dict):
                entry["by_scheme"] = {
                    str(scheme): [
                        {"permission": str(g.get("permission", "")),
                         "holder_type": str(g.get("holder_type", ""))}
                        for g in grants[:_AI_GRANT_CAP] if isinstance(g, dict)
                    ]
                    for scheme, grants in list(raw_bs.items())[:_AI_SCHEME_CAP]
                    if isinstance(grants, list)
                }
            # "holder" key with value/parameter is intentionally NOT forwarded.

        # custom_field_options: per-field {contexts:int, options:int} only.
        # Option text values are NOT forwarded.
        elif area == "custom_field_options":
            raw_bf = a.get("by_field")
            if isinstance(raw_bf, dict):
                entry["by_field"] = {
                    str(field): {"contexts": int(info.get("contexts", 0)),
                                 "options": int(info.get("options", 0))}
                    for field, info in raw_bf.items() if isinstance(info, dict)
                }

        # issuetype_schemes / issuetype_screen_schemes:
        # forward names (already in entry), count, and projects_using mapping.
        # Project IDs are not PII (they are config keys, visible to any user).
        elif area in ("issuetype_schemes", "issuetype_screen_schemes"):
            raw_pu = a.get("projects_using")
            if isinstance(raw_pu, dict):
                entry["projects_using"] = {
                    str(scheme): _safe_str_list(proj_ids)
                    for scheme, proj_ids in raw_pu.items()
                }

        # --- Section-2 new areas (project activity + shared-object ownership) ---

        # projects activity: per-project {issue_count:int|None, stale:bool}
        # keyed on the project KEY (config metadata, not PII). NEVER a project
        # lead name, accountId, or the raw lastIssueUpdateTime timestamp.
        elif area == "projects":
            raw_bp = a.get("by_project")
            if isinstance(raw_bp, dict):
                forwarded = {}
                for pkey, info in raw_bp.items():
                    if not isinstance(info, dict):
                        continue
                    ic = info.get("issue_count")
                    forwarded[str(pkey)] = {
                        "issue_count": ic if isinstance(ic, int) else None,
                        "stale": bool(info.get("stale")),
                    }
                entry["by_project"] = forwarded

        # filters / dashboards: forward count + capped (already set above) plus
        # AGGREGATE shared-object-ownership counts derived from the per-object
        # `items` booleans. The raw items list is NOT forwarded — only the two
        # integers. NEVER an owner name/accountId/email (gather never stored one,
        # and any stray identity key in an item is dropped by reading only the
        # two boolean fields).
        elif area in ("filters", "dashboards"):
            raw_items = a.get("items")
            if isinstance(raw_items, list):
                entry["inactive_owned"] = sum(
                    1 for it in raw_items
                    if isinstance(it, dict) and it.get("owner_active") is False)
                entry["public"] = sum(
                    1 for it in raw_items
                    if isinstance(it, dict) and it.get("public") is True)
            # On DC (no items) neither aggregate is set — count-only contract.

        # --- Section-3 issue-level / data-quality area (invariant I1) ---
        # Forward ONLY the documented integer count metrics (they are aggregate
        # counts, safe). Each metric value must be an int OR None; ANY other
        # shape (a dict/list/string trying to smuggle issue content or keys
        # through this area) is DROPPED. NEVER an issue key, summary,
        # description, comment, field value, or reporter/assignee identity —
        # the gather only ever stored integers, and this allowlist re-enforces
        # that so the outbound surface cannot silently widen.
        elif area == "issue_quality":
            _IQ_METRICS = ("done_unresolved", "stale_open",
                           "unassigned_unresolved", "resolved_but_open",
                           "total_unresolved")
            for m in _IQ_METRICS:
                v = a.get(m)
                # bool is an int subclass — exclude it; only true ints or None.
                if isinstance(v, bool):
                    entry[m] = None
                elif isinstance(v, int) or v is None:
                    entry[m] = v
                else:
                    # Hostile/structured value -> drop to None, never forward.
                    entry[m] = None
            # The error string (if any) is metadata, not issue content.
            if a.get("error") is not None:
                entry["error"] = str(a["error"])
            # issue_quality has no count/names concept — drop the default-shape
            # placeholders so the AI sees only the integer metrics.
            entry.pop("count", None)
            entry.pop("names", None)
            # No other key (e.g. an injected leaked_summary / issue_keys) is
            # ever copied into the forwarded entry.

        # --- Confluence areas (spec R4 / invariant I1) -----------------------
        # Forward METADATA ONLY: counts, structural booleans, principal /
        # operation / space TYPES, and GLOBAL-space keys (config identifiers).
        # NEVER a page title/body, a space-admin name/accountId, a personal-
        # space key, an email, or a group-member identity. Each reader copies
        # exactly the allowlisted primitive and discards the source object, so a
        # stray identity/content key in the snapshot can never widen the surface.

        # spaces: aggregate counts + per-GLOBAL-space {type,status,
        # has_homepage,page_count}. Personal-space records (and any stray key
        # like admin_name / homepage_title) are DROPPED — only type=="global"
        # rows are forwarded, and only the four allowlisted fields of each.
        elif area == "spaces":
            entry["personal_count"] = a.get("personal_count")
            entry["archived_count"] = a.get("archived_count")
            raw_bs = a.get("by_space")
            if isinstance(raw_bs, dict):
                fwd = {}
                for key, s in raw_bs.items():
                    if not isinstance(s, dict):
                        continue
                    # Only GLOBAL spaces are named — personal-space keys embed
                    # usernames and are forwarded as a COUNT only.
                    if s.get("type") != "global":
                        continue
                    pc = s.get("page_count")
                    fwd[str(key)] = {
                        "type": str(s.get("type", "")),
                        "status": str(s.get("status", "")),
                        "has_homepage": bool(s.get("has_homepage")),
                        "page_count": pc if isinstance(pc, int)
                        and not isinstance(pc, bool) else None,
                    }
                entry["by_space"] = fwd
            # spaces carries no names list; keep count, drop the empty names.
            entry.pop("names", None)

        # space_permissions: per-GLOBAL-space {principal_types, operations,
        # has_admin, anonymous} — TYPES and booleans only. NEVER a principal
        # value/name/id; only the string type tokens and two booleans are read.
        elif area == "space_permissions":
            raw_bs = a.get("by_space")
            if isinstance(raw_bs, dict):
                fwd = {}
                for key, p in raw_bs.items():
                    if not isinstance(p, dict):
                        continue
                    fwd[str(key)] = {
                        "principal_types": _safe_str_list(
                            p.get("principal_types")),
                        "operations": _safe_str_list(p.get("operations")),
                        "has_admin": bool(p.get("has_admin")),
                        "anonymous": bool(p.get("anonymous")),
                    }
                entry["by_space"] = fwd
            entry.pop("count", None)
            entry.pop("names", None)

        # content_quality: the documented integer metrics only. ANY non-int
        # value (a dict/list/string trying to smuggle a page title through) is
        # dropped to None — same discipline as issue_quality.
        elif area == "content_quality":
            for m in ("pages_total", "stale_pages", "drafts", "orphaned_pages"):
                v = a.get(m)
                if isinstance(v, bool):
                    entry[m] = None
                elif isinstance(v, int) or v is None:
                    entry[m] = v
                else:
                    entry[m] = None
            entry.pop("count", None)
            entry.pop("names", None)

        # templates: global page-template + blueprint counts (int or None).
        elif area == "templates":
            for m in ("global_count", "blueprint_count"):
                v = a.get(m)
                entry[m] = v if (isinstance(v, int)
                                 and not isinstance(v, bool)) or v is None \
                    else None
            entry.pop("count", None)
            entry.pop("names", None)

        # labels: global-label count (int or None).
        elif area == "labels":
            v = a.get("global_count")
            entry["global_count"] = v if (isinstance(v, int)
                                          and not isinstance(v, bool)) \
                or v is None else None
            entry.pop("count", None)
            entry.pop("names", None)

        areas_out[area] = entry

    # findings: a BOUNDED summary. Each forwarded entry is metadata only —
    # {area, name, kind, severity} + fix_tier from finding["detail"]["fix"]
    # ["tier"] when present. Raw detail, secret_data, and all other detail
    # fields are NEVER forwarded (same privacy boundary as before).
    #
    # The full per-finding list is NOT sent (a large instance can produce
    # ~1600 findings → an oversized AI payload). Instead we send:
    #   - finding_total: the complete count,
    #   - finding_counts_by_kind / finding_counts_by_severity: complete,
    #     cheap signal of scale computed over ALL findings,
    #   - finding_sample: at most _AI_FINDING_CAP findings, severity-prioritised
    #     (high first) so the most important findings are always represented and
    #     low/info findings never crowd out a high.
    def _finding_entry(f):
        entry = {"area": f.get("area"), "name": _redact_pii(f.get("name")),
                 "kind": f.get("kind"), "severity": f.get("severity")}
        try:
            tier = f["detail"]["fix"]["tier"]
            entry["fix_tier"] = tier if isinstance(tier, str) else None
        except (KeyError, TypeError):
            entry["fix_tier"] = None
        return entry

    findings = findings or []
    counts_by_kind: dict = {}
    counts_by_severity: dict = {}
    for f in findings:
        k = f.get("kind")
        s = f.get("severity")
        counts_by_kind[k] = counts_by_kind.get(k, 0) + 1
        counts_by_severity[s] = counts_by_severity.get(s, 0) + 1

    # Stable, severity-prioritised sample: sort by severity rank (high first),
    # keeping original order within a severity, then take the first N. This
    # guarantees every high-severity finding is present before any lower one,
    # and that the sample is capped at _AI_FINDING_CAP.
    ordered = sorted(
        enumerate(findings),
        key=lambda pair: (_SEVERITY_ORDER.get(pair[1].get("severity"), 99),
                          pair[0]))
    finding_sample = [_finding_entry(f) for _, f in ordered[:_AI_FINDING_CAP]]

    return {"deployment": snap.get("deployment"),
            "projects": snap.get("projects"), "areas": areas_out,
            "finding_total": len(findings),
            "finding_counts_by_kind": counts_by_kind,
            "finding_counts_by_severity": counts_by_severity,
            "finding_sample": finding_sample}


_SYSTEM = (
    "You are a senior Atlassian Jira administrator auditing a single "
    "environment's configuration health. You are given ONLY configuration "
    "metadata — object names, counts, structural booleans, holder types, "
    "workflow structure, fix tiers, and aggregate issue-quality COUNTS — never "
    "any issue content, field values, issue keys, member identities, emails, or "
    "account IDs. "
    "\n\n"
    "You are a SECOND, complementary auditor working ALONGSIDE a deterministic "
    "rule engine. That engine has already run; its results are given to you as "
    "finding_total, finding_counts_by_kind, finding_counts_by_severity, and a "
    "prioritised finding_sample. Do NOT merely restate or re-count those "
    "rule-based findings — that is the engine's job. Your distinct job is to "
    "independently INSPECT the configuration metadata (object counts, name "
    "samples, scheme usage and projects_using mappings, permission grant "
    "holder-types, workflow status/transition structure, by_type distributions, "
    "aggregate component/version/issue-quality/content counts, etc.) and report, "
    "in the ai_findings array, ADDITIONAL problems the rules plausibly MISSED: "
    "cross-object inconsistencies (a scheme used by no project; a screen scheme "
    "with no issue-type scheme; mismatched counts that should track together), "
    "governance and security-posture gaps, naming / standardisation smells "
    "(inconsistent casing, '(migrated)' / '- copy' artifacts, ad-hoc "
    "conventions), risky combinations (anonymous access plus a broad admin "
    "grant), scale / complexity concerns, and anything else that warrants admin "
    "attention. Each ai_findings item should be GENUINELY ADDITIONAL: do NOT "
    "duplicate a problem kind that is already heavily represented in "
    "finding_counts_by_kind — focus on what the deterministic checks did not "
    "surface. If you spot nothing beyond the rule findings, return an empty "
    "ai_findings array. "
    "\n\n"
    "The payload is bounded for size: for every area you receive the COMPLETE "
    "object count, plus a capped SAMPLE of object names (a few dozen examples — "
    "not the full list when the count is larger). For large per-object areas "
    "(components, versions) you receive AGGREGATE counts instead of a per-object "
    "list (e.g. components.aggregate = total / leaderless / unassigned_default / "
    "projects_with_components; versions.aggregate = total / overdue / "
    "archived_unreleased / released). Use the counts and aggregates to judge "
    "scale; the specific offending object names are in the finding sample below. "
    "\n\n"
    "The findings are summarised, not listed in full: finding_total is the total "
    "number of findings, finding_counts_by_kind and finding_counts_by_severity "
    "are COMPLETE counts over all findings, and finding_sample is a "
    "severity-prioritised sample (high first) of up to a few dozen findings. "
    "Use the counts to gauge scale and the sample for concrete examples; do not "
    "assume the sample is exhaustive when a count is larger than it. "
    "\n\n"
    "Analyse across four lenses and prioritise the highest-leverage cleanups:\n"
    "1. PERFORMANCE — identify custom-field/workflow/status/screen sprawl and "
    "large select-option sets (many options slow JQL indexing and UI rendering); "
    "flag permission-scheme bloat (many schemes or large grant lists increase "
    "permission-check overhead). Highlight counts that cross known Atlassian "
    "instance-health thresholds.\n"
    "2. CONFIGURATION HYGIENE — identify unused or orphaned objects "
    "(schemes used by no project, empty groups, boards with no apparent use), "
    "leaderless components, overdue or archived-but-unreleased versions, "
    "and migration artifacts (objects with '(migrated)' or '- copy' suffixes). "
    "Where a finding carries fix_tier='app', note that the tool can "
    "automatically remediate it; where fix_tier='human', flag it for operator "
    "review; where fix_tier='unfixable', recommend re-migration.\n"
    "3. SECURITY — flag overly-broad permission grants (sensitive permissions "
    "such as ADMINISTER or ADMINISTER_PROJECTS granted to holder_type 'anyone'), "
    "large groups with broad scheme memberships, and any pattern suggesting "
    "privilege creep across schemes.\n"
    "4. DATA QUALITY — read the issue_quality aggregate counts (these are "
    "instance-wide totals, never issue content): done_unresolved (Done-category "
    "issues with no resolution, which break Unresolved filters, release "
    "warnings, velocity, and burndown), resolved_but_open (the mirror defect), "
    "stale_open (unresolved issues untouched for a year), and "
    "unassigned_unresolved (open work with no owner), against total_unresolved "
    "as the denominator. A None metric means the probe could not run; treat it "
    "as unknown, not clean. Flag a large done_unresolved count as a top data "
    "defect.\n"
    "\n"
    "Produce the health_score / grade / summary / themes / top_risks / "
    "quick_wins as your SYNTHESIS of the overall environment (the rule findings "
    "plus your own observations). Produce ai_findings as your independent, "
    "ADDITIONAL audit on top of the rule engine. "
    "In themes and quick_wins, surface the highest-impact items first. "
    "In top_risks, name the two or three findings most likely to cause "
    "production incidents or compliance failures.\n"
    "\n"
    "Reply with ONLY a JSON object — no prose before or after:\n"
    "{\"health_score\": 0-100, \"grade\": \"A\"-\"F\", \"summary\": str, "
    "\"themes\": [{\"title\": str, \"why\": str, \"severity\": "
    "\"high\"|\"medium\"|\"low\", \"recommendation\": str, \"related\": "
    "[\"area/name\"]}], \"top_risks\": [str], \"quick_wins\": [str], "
    "\"ai_findings\": [{\"title\": str, \"area\": str, \"severity\": "
    "\"high\"|\"medium\"|\"low\", \"observation\": str, "
    "\"recommendation\": str}]}."
)


_SYSTEM_CONFLUENCE = (
    "You are a senior Atlassian Confluence administrator auditing a single "
    "instance's configuration and content health. You are given ONLY "
    "configuration metadata — space keys, names, counts, structural booleans, "
    "principal and operation TYPES, space type/status, and aggregate content "
    "COUNTS — never any page title or body, user or space-admin identity, "
    "group member identity, personal-space key, email, or account ID. "
    "\n\n"
    "You are a SECOND, complementary auditor working ALONGSIDE a deterministic "
    "rule engine. That engine has already run; its results are given to you as "
    "finding_total, finding_counts_by_kind, finding_counts_by_severity, and a "
    "prioritised finding_sample. Do NOT merely restate or re-count those "
    "rule-based findings — that is the engine's job. Your distinct job is to "
    "independently INSPECT the configuration metadata (per-space type / status / "
    "homepage / page_count, space-permission principal and operation TYPES, "
    "has_admin and anonymous booleans, aggregate content-quality counts, "
    "template / blueprint / label counts, group member-count integers, etc.) and "
    "report, in the ai_findings array, ADDITIONAL problems the rules plausibly "
    "MISSED: cross-object inconsistencies, governance and security-posture gaps "
    "(a global space with anonymous access AND no admin grant), naming / "
    "standardisation smells, risky combinations, scale / complexity concerns, "
    "and anything else that warrants admin attention. Each ai_findings item "
    "should be GENUINELY ADDITIONAL: do NOT duplicate a problem kind already "
    "heavily represented in finding_counts_by_kind — focus on what the "
    "deterministic checks did not surface. If you spot nothing beyond the rule "
    "findings, return an empty ai_findings array. "
    "\n\n"
    "The payload is bounded for size: for every area you receive the COMPLETE "
    "object count, plus a capped SAMPLE of object names (a few dozen examples — "
    "not the full list when the count is larger), and AGGREGATE counts where a "
    "per-object list would be large. Use the counts and aggregates to judge "
    "scale; the specific offending object names are in the finding sample below. "
    "\n\n"
    "The findings are summarised, not listed in full: finding_total is the total "
    "number of findings, finding_counts_by_kind and finding_counts_by_severity "
    "are COMPLETE counts over all findings, and finding_sample is a "
    "severity-prioritised sample (high first) of up to a few dozen findings. "
    "Use the counts to gauge scale and the sample for concrete examples; do not "
    "assume the sample is exhaustive when a count is larger than it. "
    "\n\n"
    "Analyse across four lenses and prioritise the highest-leverage cleanups:\n"
    "1. SPACE HYGIENE — identify empty spaces (zero pages), archived-space "
    "clutter (archiving does not free storage), oversized spaces whose page "
    "trees degrade navigation, personal-space sprawl (personal spaces count "
    "toward the instance space guardrail), and spaces missing a homepage. "
    "Flag a space count approaching Atlassian's ~10,000-space guardrail as a "
    "performance risk.\n"
    "2. PERMISSIONS & SECURITY — flag spaces with NO space-admin grant "
    "(orphaned governance), anonymous access (especially an anonymous write "
    "or create grant, a defacement/spam vector), and overly broad grants to "
    "an all-logged-in / all-users / access-class principal. Treat a None or "
    "unknowable permission signal as unknown, never clean.\n"
    "3. CONTENT & DATA QUALITY — read the aggregate content counts (instance "
    "totals, never page content): a high stale-page ratio (pages untouched "
    "for a year or more) against pages_total erodes trust and clutters "
    "search, and a draft pileup is unpublished clutter. A None metric means "
    "the probe could not run; treat it as unknown, not clean.\n"
    "4. CONFIGURATION — identify label sprawl (a very large global-label set "
    "fragments discoverability) and template/blueprint sprawl. "
    "Where a finding carries fix_tier='app', note that the tool can "
    "automatically remediate it; where fix_tier='human', flag it for operator "
    "review; where fix_tier='unfixable', recommend manual re-work.\n"
    "\n"
    "Produce the health_score / grade / summary / themes / top_risks / "
    "quick_wins as your SYNTHESIS of the overall instance (the rule findings "
    "plus your own observations). Produce ai_findings as your independent, "
    "ADDITIONAL audit on top of the rule engine. "
    "In themes and quick_wins, surface the highest-impact items first. "
    "In top_risks, name the two or three findings most likely to cause a "
    "security incident, data-loss, or compliance failure.\n"
    "\n"
    "Reply with ONLY a JSON object — no prose before or after:\n"
    "{\"health_score\": 0-100, \"grade\": \"A\"-\"F\", \"summary\": str, "
    "\"themes\": [{\"title\": str, \"why\": str, \"severity\": "
    "\"high\"|\"medium\"|\"low\", \"recommendation\": str, \"related\": "
    "[\"area/name\"]}], \"top_risks\": [str], \"quick_wins\": [str], "
    "\"ai_findings\": [{\"title\": str, \"area\": str, \"severity\": "
    "\"high\"|\"medium\"|\"low\", \"observation\": str, "
    "\"recommendation\": str}]}."
)


def _text(resp):
    return "".join(b.text for b in resp.content
                   if getattr(b, "type", None) == "text" and getattr(b, "text", None))


def analyze(snap: dict, findings: list, provider, *, model=None,
            effort="medium", product="jira") -> dict:
    """Run the AI environment assessment through a PROVIDER (with a .complete
    method). The provider is built from Settings (Anthropic OR an
    OpenAI-compatible endpoint); a None provider means AI is skipped and never
    raises, so a missing/unconfigured provider never blocks the run.

    `product` selects the system prompt and user message: "confluence" uses the
    Confluence-administrator prompt; anything else (Jira and any future product
    routed here) uses the Jira prompt. The provider abstraction and return shape
    are unchanged.

    The metadata-only privacy boundary is unchanged and provider-agnostic:
    summarize_for_ai runs BEFORE the provider call, so the same allowlisted
    metadata is sent to whichever provider is configured."""
    if provider is None:
        return {"skipped": True, "error": None, "health_score": None,
                "grade": None,
                "summary": "AI analysis skipped (no AI provider configured).",
                "themes": [], "top_risks": [], "quick_wins": [],
                "ai_findings": [], "model": None}
    summary = summarize_for_ai(snap, findings, product=product)
    if product == "confluence":
        system, lead = _SYSTEM_CONFLUENCE, \
            "Audit this Confluence environment configuration:\n"
    else:
        system, lead = _SYSTEM, \
            "Audit this Jira environment configuration:\n"
    r = provider.complete(
        system, lead + json.dumps(summary, default=str),
        model=model, effort=effort)
    rmodel = r.get("model")
    if r.get("refused"):
        return {"skipped": False, "error": "the model declined this request",
                "health_score": None, "grade": None, "summary": "",
                "themes": [], "top_risks": [], "quick_wins": [],
                "ai_findings": [], "model": rmodel}
    if r.get("error"):
        return {"skipped": False, "error": r["error"],
                "health_score": None, "grade": None, "summary": "",
                "themes": [], "top_risks": [], "quick_wins": [],
                "ai_findings": [], "model": rmodel}
    parsed = _parse(r.get("text"), rmodel)
    # Ground the AI's independent findings against the areas actually audited so
    # a hallucinated area can't reach the report as a real finding.
    audited_areas = [f.get("area") for f in (findings or []) if isinstance(f, dict)]
    parsed["ai_findings"] = _ground_ai_findings(parsed.get("ai_findings"), snap,
                                                audited_areas)
    return parsed


_AREA_SENTINELS = frozenset(("", "multiple", "general", "cross-area",
                             "cross_area", "overall", "global", "n/a"))


def _ground_token(s):
    return str(s or "").strip().lower().replace(" ", "_")


def _ground_ai_findings(findings, snap, audited_areas=()):
    """Drop ai_findings whose `area` was never audited — the model must not
    invent an area/object the snapshot never contained and have it render as a
    real, admin-facing finding (prompt instructions are not a control). Grounds
    against the snapshot's areas PLUS the areas that produced deterministic
    findings (finer-grained finding categories are not top-level snapshot keys).
    Cross-area sentinels (e.g. 'multiple') are kept; kept items are flagged
    grounded=True. Fail-open: with nothing audited to ground against, keep all."""
    findings = [f for f in (findings or []) if isinstance(f, dict)]
    valid = {_ground_token(a) for a in (snap.get("areas") or {})}
    valid |= {_ground_token(a) for a in audited_areas}
    if not valid:
        return findings        # no audited reference -> cannot verify, don't drop
    kept = []
    for f in findings:
        tok = _ground_token(f.get("area"))
        if tok in valid or tok in _AREA_SENTINELS:
            f["grounded"] = True
            kept.append(f)
    return kept


def _clamp_score(v):
    """Coerce an AI-supplied health score to an int in [0, 100], or None if it
    is not a finite number. The model is not trusted to stay in range/type:
    bool (an int subclass) and non-finite floats (Infinity/NaN, which
    json.loads accepts by default and which crash int(round(...))) are rejected."""
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return max(0, min(100, int(round(f))))


def _valid_grade(v):
    """Normalize an AI-supplied grade to a clean A-F (uppercased), else None —
    a stray 'A+' / 'Excellent' / number must not reach the report as a grade."""
    if not isinstance(v, str):
        return None
    g = v.strip().upper()
    return g if g in ("A", "B", "C", "D", "F") else None


# Keys that mark a model object as the actual assessment (vs a decoy/preamble
# object the model emitted before its answer). area_findings covers the
# sectioned/map-reduce per-area replies.
_ASSESSMENT_KEYS = frozenset((
    "health_score", "grade", "summary", "themes", "top_risks",
    "quick_wins", "ai_findings", "roadmap", "gaps", "area_findings"))


def _json_objects(text):
    """Every top-level balanced {...} substring that parses as a JSON dict, in
    order, in a SINGLE left-to-right O(n) pass (string/escape aware). Tolerates
    decoy/prose objects, ```json fences and trailing text without going
    quadratic: an unbalanced tail simply ends the scan."""
    out = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        start, depth, in_str, esc, closed = i, 0, False, False, False
        while i < n:
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    i += 1
                    closed = True
                    break
            i += 1
        if not closed:
            break       # unbalanced from here on -> no more complete objects
        try:
            d = json.loads(text[start:i])
        except (ValueError, json.JSONDecodeError):
            d = None
        if isinstance(d, dict):
            out.append(d)
    return out


def _extract_json(text):
    """Best-effort dict extraction from a model reply: try the whole string,
    then — among ALL balanced top-level objects — pick the one carrying
    assessment keys, so a decoy/preamble object can't win. Falls back to the
    last object, then None. Tolerates ```json fences, prose, and trailing text."""
    if not text:
        return None
    for candidate in (text.strip(), text):
        try:
            d = json.loads(candidate)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(d, dict):
            return d
    objs = _json_objects(text)
    if not objs:
        return None
    for d in objs:
        if _ASSESSMENT_KEYS & d.keys():
            return d
    return objs[-1]


def _parse(text, model):
    # ai_findings rounds-trips here: it is the AI's complementary second-auditor
    # output (issues the deterministic rules did not catch). It defaults to []
    # when absent/unparseable, and any non-list value is coerced to [] so the
    # renderer always receives a list.
    base = {"skipped": False, "error": None, "model": model, "themes": [],
            "top_risks": [], "quick_wins": [], "ai_findings": [],
            "roadmap": [], "gaps": [],
            "health_score": None, "grade": None, "summary": ""}
    text = text or ""   # a provider may return None content; treat as empty.
    d = _extract_json(text)
    if d is not None:
        base.update({k: d.get(k, base[k]) for k in
                     ("health_score", "grade", "summary", "themes",
                      "top_risks", "quick_wins", "ai_findings", "roadmap",
                      "gaps")})
        for k in ("ai_findings", "roadmap", "gaps"):
            if not isinstance(base[k], list):
                base[k] = []
        # The model is not trusted for the headline numbers: validate + clamp.
        base["health_score"] = _clamp_score(base["health_score"])
        base["grade"] = _valid_grade(base["grade"])
    else:
        base["summary"] = text[:1500].strip() or "No structured assessment returned."
    return base


# ===========================================================================
# Map-reduce ("sectioned") analysis — parallel per-area passes + a synthesis
# pass that re-correlates across areas. Trades cost + latency for DEPTH.
# ===========================================================================

# Ordered (label, area-name keywords). An area is placed in the FIRST section
# whose any keyword is a substring of the lowercased area name; unmatched areas
# fall to the catch-all. Security is FIRST so "permission_scheme_grants" lands in
# access (its "scheme" substring must not steal it into the schemes section).
_SECTIONS_JIRA = [
    ("Security & access", ("permission", "group")),
    ("Workflows, statuses & schemes",
     ("workflow", "status", "scheme", "resolution", "priority",
      "issue_type", "issuetype", "link_type", "linktype")),
    ("Fields & screens", ("field", "screen")),
    ("Projects, components & versions",
     ("project", "component", "version", "board", "filter", "dashboard")),
    ("Issue quality", ("issue_quality", "quality")),
]
_SECTIONS_CONFLUENCE = [
    ("Permissions & groups", ("permission", "group")),
    ("Spaces", ("space",)),
    ("Content & templates", ("template", "label", "content")),
]
_CATCHALL = "Other configuration"

_SECTION_SYSTEM = (
    "You are a senior Atlassian Jira administrator doing a DEEP audit of ONE area "
    "of a single environment. You are given ONLY configuration metadata — object "
    "names, counts, structural booleans, holder types — never issue content, "
    "field values, issue keys, member identities, emails, or account IDs. The "
    "deterministic rule engine has ALREADY produced the finding COUNTS in this "
    "payload (rule_findings); do NOT merely restate them — that adds no value. "
    "Your value is the analysis the rules cannot do. For each real problem, give "
    "the ROOT CAUSE (WHY is this happening — unfinished migration, missing "
    "governance, config drift, copy-paste sprawl?), the concrete RISK (operational/"
    "security/governance/performance) and who it hurts, a PRIORITY (1=highest..5), "
    "and a specific ORDERED remediation. Group related objects into ONE finding "
    "rather than one-per-object. Think hard. Return ONLY a JSON object: "
    "{\"area_findings\": [{\"title\": short, \"area\": area name, \"severity\": "
    "\"high\"|\"medium\"|\"low\", \"priority\": 1-5, \"affected_count\": int, "
    "\"root_cause\": why, \"risk\": concrete impact, \"remediation_steps\": "
    "[ordered strings], \"effort\": \"S\"|\"M\"|\"L\"}]}. If the area is genuinely "
    "healthy, return an empty list. Never invent objects not present in the data.")
_SECTION_SYSTEM_CONFLUENCE = _SECTION_SYSTEM.replace("Jira", "Confluence")

_SYNTH_SYSTEM = (
    "You are the LEAD Atlassian Jira auditor writing the executive assessment. It "
    "MUST be clearly MORE valuable than the mechanical finding list a rule engine "
    "already produced. You are given your team's per-area findings plus the rule "
    "engine's counts — metadata only, never content or identities. Deliver: (1) a "
    "sharp executive SUMMARY of what is really going on and WHY (root causes, not "
    "symptoms); (2) CROSS-AREA findings the per-area passes could not see (e.g. a "
    "status that is both unreachable AND in an unreferenced workflow; "
    "field+screen+scheme sprawl compounding); (3) a PRIORITIZED REMEDIATION "
    "ROADMAP — an ORDERED sequence of concrete actions, highest-leverage first, "
    "each with its rationale, which problems it resolves, and rough effort; (4) "
    "what the rule engine likely MISSED (gaps, anti-patterns it does not encode). "
    "Think hard. Return ONLY a JSON object: {\"health_score\": 0-100, \"grade\": "
    "\"A\"|\"B\"|\"C\"|\"D\"|\"F\", \"summary\": \"3-5 sentence narrative\", "
    "\"themes\": [..], \"top_risks\": [..], \"quick_wins\": [..], \"roadmap\": "
    "[{\"step\": action, \"rationale\": why now, \"addresses\": [problems], "
    "\"effort\": \"S\"|\"M\"|\"L\"}], \"gaps\": [what the rules missed], "
    "\"ai_findings\": [{\"title\":.., \"area\": \"multiple\" or area, "
    "\"severity\":.., \"priority\": 1-5, \"root_cause\":.., \"risk\":.., "
    "\"remediation_steps\": [..]}]}. ai_findings = CROSS-AREA issues only, not "
    "per-area repeats.")
_SYNTH_SYSTEM_CONFLUENCE = _SYNTH_SYSTEM.replace("Jira", "Confluence")


def _section_workers() -> int:
    """Concurrency for the parallel per-area passes. Deliberately modest — each
    pass is a full provider call (a `claude -p` subprocess / an API request), so
    a wide pool multiplies cost + rate-limit pressure. Override with
    MA_AI_SECTION_WORKERS."""
    raw = os.environ.get("MA_AI_SECTION_WORKERS")
    if raw is None:
        return 4
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 4
    return n if n >= 1 else 1


def _split_sections(summary: dict, findings: list, product: str) -> list:
    """Split into per-area-group SECTIONS. Every area lands in exactly one section
    (first-keyword-match, else the catch-all). Crucially, each section carries the
    COMPLETE per-kind rollup of the FULL findings for its areas — every kind's
    true count plus a generous sample of example names — NOT a slice of a global
    40-cap. That is what gives each per-area pass real material to reason over.

    The area entries are the SAME objects summarize_for_ai built, and the example
    names are object names (already allowlisted) — sectioning only re-buckets
    already-allowlisted metadata, so the privacy boundary is unchanged."""
    specs = _SECTIONS_CONFLUENCE if product == "confluence" else _SECTIONS_JIRA
    labels = [lbl for lbl, _ in specs] + [_CATCHALL]

    def idx_for(area_name) -> int:
        an = (area_name or "").lower()
        for i, (_lbl, kws) in enumerate(specs):
            if any(kw in an for kw in kws):
                return i
        return len(specs)   # catch-all

    def bucket(i):
        return buckets.setdefault(i, {"areas": {}, "kinds": {}})

    buckets: dict[int, dict] = {}
    for an, entry in (summary.get("areas") or {}).items():
        bucket(idx_for(an))["areas"][an] = entry
    for f in (findings or []):
        kinds = bucket(idx_for(f.get("area")))["kinds"]
        k = f.get("kind") or "unknown"
        e = kinds.setdefault(k, {"kind": k, "count": 0,
                                 "severity": f.get("severity"), "examples": []})
        e["count"] += 1
        nm = f.get("name")
        if isinstance(nm, str) and len(e["examples"]) < _AI_SECTION_EXAMPLE_CAP:
            e["examples"].append(nm)

    out = []
    for i in sorted(buckets):
        b = buckets[i]
        if not b["areas"] and not b["kinds"]:
            continue
        out.append({"label": labels[i], "payload": {
            "deployment": summary.get("deployment"),
            "areas": b["areas"],
            "rule_findings": sorted(b["kinds"].values(),
                                    key=lambda x: -x["count"])}})
    return out


def _parse_section(text) -> list:
    """Pull the area_findings list out of a section response; tolerate noise.
    Uses the same hardened, decoy-resistant extractor as _parse (the old
    index/rindex span silently dropped per-area findings on any stray brace)."""
    d = _extract_json(text or "")
    af = d.get("area_findings") if isinstance(d, dict) else None
    return af if isinstance(af, list) else []


def _str_list(v, cap=8):
    return [str(s).strip() for s in (v or []) if str(s).strip()][:cap]


def _normalize_findings(items, default_area) -> list:
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        out.append({
            "title": str(it.get("title") or "").strip() or "Untitled finding",
            "area": str(it.get("area") or default_area or "").strip(),
            "severity": (it.get("severity") if it.get("severity") in
                         ("high", "medium", "low") else "low"),
            "priority": (it.get("priority")
                         if isinstance(it.get("priority"), int) else None),
            "affected_count": (it.get("affected_count")
                               if isinstance(it.get("affected_count"), int)
                               else None),
            "root_cause": str(it.get("root_cause") or "").strip(),
            "risk": str(it.get("risk") or "").strip(),
            "remediation_steps": _str_list(it.get("remediation_steps")),
            "effort": (it.get("effort") if it.get("effort") in ("S", "M", "L")
                       else None),
            # legacy fields kept so a renderer can fall back gracefully
            "observation": str(it.get("observation") or it.get("risk") or "").strip(),
            "recommendation": str(it.get("recommendation") or "").strip()})
    return out


def _dedupe_findings(findings) -> list:
    """Stable dedupe by (title, area), first occurrence wins (synthesis cross-area
    findings are merged ahead of the per-area ones, so they take precedence)."""
    seen = set()
    out = []
    for f in findings or []:
        key = ((f.get("title") or "").lower().strip(),
               (f.get("area") or "").lower().strip())
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


_EFFORT_LADDER = ["low", "medium", "high", "xhigh", "max"]


def _bump_effort(e: str) -> str:
    """One notch up the effort ladder (synthesis reasons harder than the per-area
    passes). Unknown values jump straight to max."""
    try:
        i = _EFFORT_LADDER.index(e)
    except ValueError:
        return "max"
    return _EFFORT_LADDER[min(i + 1, len(_EFFORT_LADDER) - 1)]


def _skipped_result():
    return {"skipped": True, "error": None, "health_score": None, "grade": None,
            "summary": "AI analysis skipped (no AI provider configured).",
            "themes": [], "top_risks": [], "quick_wins": [], "ai_findings": [],
            "roadmap": [], "gaps": [], "model": None}


def analyze_sectioned(snap: dict, findings: list, provider, *, model=None,
                      effort="high", product="jira", workers=None) -> dict:
    """Map-reduce env assessment: a focused analysis per area-group runs in
    PARALLEL (map), then a synthesis pass re-correlates across areas (reduce).

    Same metadata-only boundary as analyze(): summarize_for_ai runs first and
    every section payload is a slice of its allowlisted output. A None provider
    skips (never blocks). A failing section is isolated (its depth is lost but
    siblings + synthesis proceed). If synthesis fails, the per-area findings are
    still returned (degraded: no overall grade) so the work is never wholly lost.
    Return shape matches analyze()/_parse so the renderer is unchanged."""
    if provider is None:
        return _skipped_result()

    summary = summarize_for_ai(snap, findings, product=product)
    sections = _split_sections(summary, findings, product)
    sec_sys = (_SECTION_SYSTEM_CONFLUENCE if product == "confluence"
               else _SECTION_SYSTEM)

    # --- MAP: one focused, independent provider call per section, in parallel ---
    def _one(section):
        user = (f"Section: {section['label']}\n"
                + json.dumps(section["payload"], default=str))
        return section["label"], provider.complete(
            sec_sys, user, model=model, effort=effort)

    results = map_results(sections, _one, workers or _section_workers())

    section_findings: list = []
    section_summaries: list = []
    last_model = None
    for sec, res in zip(sections, results):
        if isinstance(res, Exception):
            section_summaries.append({"section": sec["label"], "error": str(res)})
            continue
        label, r = res
        last_model = r.get("model") or last_model
        if r.get("error") or r.get("refused"):
            section_summaries.append(
                {"section": label, "error": r.get("error") or "declined"})
            continue
        af = _normalize_findings(_parse_section(r.get("text")), label)
        section_findings.extend(af)
        section_summaries.append({"section": label, "findings": af})

    # If EVERY per-area pass failed (provider error / refusal / crash), there is
    # nothing for synthesis to reason over — running it anyway lets the model
    # invent a confident grade from the bare finding counts. Degrade explicitly
    # (null grade + error) instead of shipping a green-looking result built on
    # nothing. A section that legitimately found ZERO issues is NOT a failure
    # (it carries "findings", not "error").
    if sections and all("error" in s for s in section_summaries):
        first_err = next((s["error"] for s in section_summaries
                          if s.get("error")), "unknown")
        return {"skipped": False,
                "error": f"all {len(sections)} per-area analyses failed "
                         f"({first_err})",
                "health_score": None, "grade": None,
                "summary": "AI analysis could not complete — every per-area "
                           "pass failed.",
                "themes": [], "top_risks": [], "quick_wins": [],
                "roadmap": [], "gaps": [], "ai_findings": [], "model": last_model}

    # --- REDUCE: synthesis correlates across the per-area results ---
    synth_sys = (_SYNTH_SYSTEM_CONFLUENCE if product == "confluence"
                 else _SYNTH_SYSTEM)
    synth_payload = {
        "deployment": summary.get("deployment"),
        "finding_total": summary.get("finding_total"),
        "finding_counts_by_kind": summary.get("finding_counts_by_kind"),
        "finding_counts_by_severity": summary.get("finding_counts_by_severity"),
        "per_area_findings": section_summaries}
    # Synthesis is the high-value cross-area reasoning — give it one notch MORE
    # effort than the per-area passes.
    r = provider.complete(
        synth_sys,
        "PER-AREA FINDINGS (synthesize overall health):\n"
        + json.dumps(synth_payload, default=str),
        model=model, effort=_bump_effort(effort))
    smodel = r.get("model") or last_model

    if r.get("refused") or r.get("error"):
        # Degraded: keep the per-area depth, just no overall grade/score.
        return {"skipped": False,
                "error": r.get("error") or "the model declined this request",
                "health_score": None, "grade": None,
                "summary": "Per-area analysis completed; overall synthesis "
                           "unavailable.",
                "themes": [], "top_risks": [], "quick_wins": [],
                "roadmap": [], "gaps": [],
                "ai_findings": _dedupe_findings(section_findings),
                "model": smodel}

    parsed = _parse(r.get("text"), smodel)
    # Merge: synthesis CROSS-AREA findings first (they win dedupe), then the
    # per-area depth findings.
    parsed["ai_findings"] = _dedupe_findings(
        list(parsed.get("ai_findings") or []) + section_findings)
    return parsed
