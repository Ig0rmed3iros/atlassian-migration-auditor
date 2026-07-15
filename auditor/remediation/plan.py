"""Turn selected fixes + findings into an ordered, dry-run-able plan.

Pure: no client calls. Dependency rule — a wire/populate action for an object
is ordered after that object's create action. Tier rank create<wire<populate
encodes both the safety order and the dependency order, so a stable sort on
(object, tier_rank) is sufficient."""
from __future__ import annotations

from dataclasses import dataclass, field

from .registry import get_fix, fixes_for

_TIER_RANK = {"create": 0, "wire": 1, "populate": 2}


@dataclass
class FixAction:
    fix_id: str
    tier: str
    risk: str
    object_name: str
    area: str
    finding_ref: str
    payload: dict
    side: str = "target"          # invariant: never "source" (apply asserts)


@dataclass
class FixPlan:
    actions: list = field(default_factory=list)
    skipped: list = field(default_factory=list)


def _ref(finding: dict) -> str:
    return f"{finding.get('area')}/{finding.get('name')}"


def build_plan(findings: list, selected_fix_ids: list,
               product: str = "jira") -> FixPlan:
    selected = set(selected_fix_ids)
    plan = FixPlan()
    for finding in findings:
        applicable = [f for f in fixes_for(product, finding)
                      if f.fix_id in selected]
        if not applicable:
            continue
        if finding.get("fix_payload") is None:
            plan.skipped.append({"finding": _ref(finding),
                                 "reason": "no fix payload captured"})
            continue
        for fx in applicable:
            plan.actions.append(FixAction(
                fix_id=fx.fix_id, tier=fx.tier, risk=fx.risk,
                object_name=finding.get("name"), area=finding.get("area"),
                finding_ref=_ref(finding), payload=finding["fix_payload"]))
    plan.actions.sort(key=lambda a: (a.object_name or "",
                                     _TIER_RANK.get(a.tier, 9)))
    return plan


def dry_run_preview(plan: FixPlan) -> dict:
    objects = {a.object_name for a in plan.actions if a.tier == "create"}
    issues = sum(a.payload.get("values_count", 0)
                 for a in plan.actions if a.tier == "populate")
    return {"objects": len(objects), "issues_to_touch": issues,
            "calls": len(plan.actions), "skipped": len(plan.skipped),
            "high_risk": sorted({a.fix_id for a in plan.actions
                                 if a.risk == "high"})}
