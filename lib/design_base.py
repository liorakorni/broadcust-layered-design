"""The request for the Design Base extraction (layered-v2).

Built from the layer plan in the scene, so what the provider is asked to remove is exactly
what the layout will draw, quoted in the pixel space of the image the provider is looking
at (lib/coords.py) rather than in canvas pixels. Everything else the prompt could say about
the design is left out on purpose: the brief is not reintroduced, only the ownership lines
that apply are included, and an effect the layout draws in CSS (a gradient) is resolved
here, before the request exists, into a "do not draw this" line rather than left for the
provider to reproduce.

The Background Image extraction does not use this prompt.
"""

from __future__ import annotations

from lib import coords, prompts, template_policy, validate_assets

WINDOWS_OVERLAY = (
    "Keep photo windows transparent and preserve partial transparency where overlays "
    "must reveal the photograph beneath them."
)
WINDOWS_FULL = (
    "No photograph shows through this design: fills may be opaque everywhere they were."
)


def build_prompt(scene, concept_bytes, tolerance=None, defect=None):
    """`(prompt, info)`. `info` is what a diagnostic needs to reproduce the request."""
    canvas = scene["canvas"]
    size = coords.image_size(concept_bytes)
    lines = ownership_lines(scene, canvas, size)
    plan = scene.get("layerPlan") or {}
    representation = (plan.get("template") or {}).get("representation", "overlay")
    prompt = prompts.render(
        "design-base",
        windows=WINDOWS_FULL if representation == "full" else WINDOWS_OVERLAY,
        image_width=size[0], image_height=size[1],
        ownership=prompts.bullet_list(lines) if lines else "- (nothing is assigned elsewhere)",
        tolerance=template_policy.prompt_clause(tolerance),
    )
    if defect:
        prompt += "\n" + prompts.render(
            "design-base-repair", defects=prompts.bullet_list([defect]))
    info = {
        "representation": representation,
        "imageSize": {"width": size[0], "height": size[1]},
        "canvas": {"width": canvas["width"], "height": canvas["height"]},
        "ownershipLines": len(lines),
        "tolerance": (tolerance or {}).get("profile"),
        "repair": bool(defect),
    }
    return prompt, info


def ownership_lines(scene, canvas, size):
    """What is assigned to independent layers, with boxes in the image's pixel space."""
    lines = []
    for component in validate_assets.editable_component_boxes(scene):
        box = coords.canvas_to_image(component["box"], canvas, size)
        lines.append(
            f"the {_describe(component)} at x={box['x']}, y={box['y']}, "
            f"{box['width']}x{box['height']} px"
        )
    for effect in scene.get("effects") or []:
        if effect.get("kind") != "readabilityGradient":
            continue
        said = (effect.get("note") or "").strip().rstrip(".")
        if effect.get("owner") == "layout":
            lines.append(
                "the readability gradient behind the copy"
                + (f" ({said})" if said else "")
                + " - drawn in code as an editable gradient; leave it out of this image"
            )
        elif effect.get("owner") == "photo":
            lines.append(
                "the soft dark shading over the photograph behind the copy"
                + (f" ({said})" if said else "")
                + " - shading that fades into the photograph, not a panel"
            )
    return lines


def _describe(component):
    if component["kind"] == "rasterIcon":
        return "pictogram (the symbol itself, not the circle behind it)"
    colour = f" {component['color']}" if component.get("color") else ""
    return f"{component.get('shape') or 'shape'}{colour}"
