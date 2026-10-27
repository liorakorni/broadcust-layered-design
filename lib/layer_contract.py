"""Does the delivered candidate keep the layer contract?

The contract (lib/layer_plan.py) says which layer owns what: the Design Base holds
structural artwork only, text and icons and contact circles are editable layers, and no
element has two owners. This reads the facts already recorded for ONE candidate (its
scene's plan and its validation record) and reports compliance. It makes no model call.

A violation names the affected ids, and the result built from it is `needsReview` and not
`publishReady`: an owner conflict means the editor may not behave as promised, and that is
for a person to look at - it is not hidden, and it does not by itself fail the job.
"""

from __future__ import annotations

from lib import layer_plan

#: A major piece of artwork the approved concept does not have, still in the Design Base.
CODE_TEMPLATE_ARTWORK = "TEMPLATE_MAJOR_ARTWORK_REMAINS"
CODE_PLAN_INVALID = "LAYER_PLAN_INVALID"


def assess(scene, validation):
    """`{checked, version, compliant, violations, needsReview}` for one candidate."""
    plan = (scene or {}).get("layerPlan")
    if not plan:
        return {"checked": False, "version": None, "compliant": None, "violations": [],
                "needsReview": False, "reason": "The candidate has no layer plan."}
    violations = []
    try:
        layer_plan.check(plan)
    except layer_plan.PlanError as exc:
        violations.append({"code": CODE_PLAN_INVALID, "affected": [], "message": str(exc)})
    validation = validation or {}
    for entry in validation.get("violations") or []:
        violations.append({
            "code": entry.get("code") or "OWNERSHIP_VIOLATION",
            "affected": list(entry.get("affected") or entry.get("elementIds") or []),
            "message": entry.get("message") or "An element has two owners.",
        })
    template = validation.get("template") or {}
    if template.get("present") and template.get("major"):
        decoration = next((e["id"] for e in plan["elements"] if e["owner"] == "template"), None)
        violations.append({
            "code": CODE_TEMPLATE_ARTWORK,
            "affected": [decoration] if decoration else [],
            "message": "The Design Base contains major artwork that the approved concept "
                       "does not have.",
        })
    return {
        "checked": True, "version": plan.get("version", layer_plan.VERSION),
        "compliant": not violations, "violations": violations,
        "needsReview": bool(violations),
    }
