"""The single source of truth for WHICH defects are fixable and HOW.

Each Fix is one consent checkbox. Tiers rise in risk: create (recreate a
definition, safe) -> wire (change target behaviour) -> populate (rewrite
issue metadata). Tier-2 defects are absent here entirely — guidance.py owns
them. Planner, applier and UI all read this registry, so adding a fix is one
FIXES entry plus its apply function."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Fix:
    fix_id: str
    product: str
    area: str
    kinds: tuple             # finding kinds this fix applies to
    tier: str                # "create" | "wire" | "populate"
    risk: str                # "low" | "medium" | "high"
    label: str
    disclaimer: str
    requires_confirm: bool = False   # extra gate beyond the checkbox

    def applies_to(self, finding: dict) -> bool:
        return (finding.get("area") == self.area
                and finding.get("kind") in self.kinds)


FIXES: list[Fix] = [
    Fix("jira.custom_field.create", "jira", "custom_fields", ("missing_in_tgt",),
        "create", "low", "Create missing custom fields",
        "Creates the field definition, its context(s) and select options. "
        "Fields are created WITHOUT values."),
    Fix("jira.custom_field.wire_screen", "jira", "custom_fields",
        ("missing_in_tgt",), "wire", "medium",
        "Add created fields to their screens",
        "Adds the field to the screens it occupies on the source. Changes "
        "which fields appear on the target's create/edit views."),
    Fix("jira.custom_field.populate", "jira", "custom_fields", ("missing_in_tgt",),
        "populate", "medium", "Populate field values",
        "Sets each issue's source value on the target. The issue's Updated "
        "date becomes today and target automation may fire — pause it first."),
    Fix("jira.custom_field.add_options", "jira", "custom_fields",
        ("option_mismatch",), "create", "low", "Add missing select options",
        "Adds the missing options to the existing target field."),
    Fix("jira.status.create", "jira", "statuses", ("missing_in_tgt",),
        "create", "low", "Create missing statuses",
        "Creates the status with its category. Not wired into any workflow."),
    Fix("jira.priority.create", "jira", "priorities", ("missing_in_tgt",),
        "create", "low", "Create missing priorities",
        "Creates the priority definition."),
    Fix("jira.resolution.create", "jira", "resolutions", ("missing_in_tgt",),
        "create", "low", "Create missing resolutions",
        "Creates the resolution definition."),
    Fix("jira.issue_type.create", "jira", "issue_types", ("missing_in_tgt",),
        "create", "low", "Create missing issue types",
        "Creates the issue type. Not added to any project's scheme."),
    Fix("jira.link_type.create", "jira", "link_types", ("missing_in_tgt",),
        "create", "low", "Create missing issue link types",
        "Creates the link type with its inward/outward labels."),
    # jira.screen.create removed (I5/I7): screens have no payload capture in v1
    #   and the disclaimer overclaimed. Screens are detect-and-guide; listed as
    #   future work.
    # jira.status.wire_workflow removed (C3/I4): live-workflow editing is
    #   Tier-2; the apply branch always returned ok=False. Replaced by
    #   guidance_for('workflow_wire', ...) in guidance.py.
    # confluence.label.create removed (C4): the 'labels' area has no apply
    #   branch and no Confluence payload pipeline in v1. Confluence remediation
    #   is detect-and-guide.
]

_BY_ID = {f.fix_id: f for f in FIXES}


def get_fix(fix_id: str) -> Fix:
    return _BY_ID[fix_id]


def fixes_for(product: str, finding: dict) -> list[Fix]:
    return [f for f in FIXES if f.product == product and f.applies_to(finding)]
