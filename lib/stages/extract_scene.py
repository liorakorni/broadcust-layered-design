"""EXTRACT_SCENE: turn the concept image into a strict JSON layout.

The model returns geometry, never HTML (SPEC section 4). Assembly is done by
code in lib/assemble_html.py so the output is deterministic and escapable.

The single most dangerous detail here is `detail="original"` on the image part.
Any other detail level makes the API resize the image before the model sees it,
so every returned coordinate describes the resized frame rather than the canvas -
and nothing errors. The design simply comes out with every layer misplaced. See
docs/PROBE_RESULTS.md for the verification that detail="original" preserves the
true 1024x1024 frame.
"""

from __future__ import annotations

import logging

import conf
from lib import alignment, artifacts, concept_sample, jobs, prompts, schemas
from lib.errors import InvalidScene, Stage
from lib.stages import independent
from lib.providers import vision

log = logging.getLogger("layered-design.extract-scene")

_BOX = {
    "x": {"type": "integer"},
    "y": {"type": "integer"},
    "width": {"type": "integer"},
    "height": {"type": "integer"},
}

SCENE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "canvas", "textLayers", "imageLayers", "overlaySamples", "shapeLayers",
        "effects", "components",
    ],
    "properties": {
        "canvas": {
            "type": "object",
            "additionalProperties": False,
            "required": ["width", "height"],
            "properties": {
                "width": {"type": "integer"},
                "height": {"type": "integer"},
            },
        },
        "textLayers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "role",
                    "contentKey",
                    "text",
                    "x",
                    "y",
                    "width",
                    "height",
                    "zIndex",
                    "fontFamily",
                    "fontSize",
                    "fontWeight",
                    "color",
                    "align",
                    "direction",
                    "lineHeight",
                    "letterSpacing",
                    "fill",
                    "strokeColor",
                    "strokeWidth",
                    "wrap",
                    "overflow",
                ],
                "properties": {
                    "id": {"type": "string"},
                    # Constrained here rather than only in the validator: an open
                    # string lets the model invent a role, which is then rejected
                    # after both this call and the image generation before it have
                    # already been paid for.
                    "role": {"type": "string", "enum": list(schemas.ALLOWED_ROLES)},
                    "contentKey": {"type": "string"},
                    "text": {"type": "string"},
                    **_BOX,
                    "zIndex": {"type": "integer"},
                    "fontFamily": {"type": "string"},
                    "fontSize": {"type": "integer"},
                    "fontWeight": {"type": "integer"},
                    "color": {"type": "string"},
                    "align": {"type": "string", "enum": ["left", "right", "center"]},
                    "direction": {"type": "string", "enum": ["rtl", "ltr"]},
                    "lineHeight": {"type": "number"},
                    # Typography the concept can show beyond size and colour.
                    # Every one is required in strict mode; the neutral value
                    # (0, "solid", null, 0, "wrap", "shrink") means "plain text".
                    "letterSpacing": {"type": "number"},
                    "fill": {"type": "string", "enum": ["solid", "transparent"]},
                    "strokeColor": {"type": ["string", "null"]},
                    "strokeWidth": {"type": "number"},
                    "wrap": {"type": "string", "enum": ["wrap", "nowrap"]},
                    "overflow": {"type": "string", "enum": ["shrink", "visible"]},
                },
            },
        },
        "imageLayers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "type",
                    "assetId",
                    "x",
                    "y",
                    "width",
                    "height",
                    "zIndex",
                    "objectFit",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": ["background", "decoration", "image"],
                    },
                    # Nullable rather than optional: strict mode requires every
                    # property to be listed in `required`.
                    "assetId": {"type": ["string", "null"]},
                    **_BOX,
                    "zIndex": {"type": "integer"},
                    "objectFit": {"type": "string", "enum": ["cover", "contain"]},
                },
            },
        },
        "overlaySamples": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "x", "y", "width", "height"],
                "properties": {
                    "kind": {"type": "string", "enum": ["transparent", "opaque"]},
                    **_BOX,
                },
            },
        },
        # The simple elements the layout draws itself rather than leaving in the
        # artwork: CTA backgrounds, contact circles, panels, gradients, dividers.
        # Nullable rather than optional throughout: strict mode requires every
        # property to be listed in `required`, so a value that does not apply to
        # a kind is sent as null.
        "shapeLayers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id", "kind", "effect", "x", "y", "width", "height", "zIndex",
                    "color", "opacity", "radius", "angle", "stops", "borderColor",
                    "borderWidth", "rotation",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string", "enum": list(schemas.SHAPE_KINDS)},
                    "effect": {"type": "string"},
                    **_BOX,
                    "zIndex": {"type": "integer"},
                    "color": {"type": ["string", "null"]},
                    "opacity": {"type": "number"},
                    "radius": {"type": "integer"},
                    "angle": {"type": "integer"},
                    "stops": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["color", "opacity", "position"],
                            "properties": {
                                "color": {"type": "string"},
                                "opacity": {"type": "number"},
                                "position": {"type": "number"},
                            },
                        },
                    },
                    "borderColor": {"type": ["string", "null"]},
                    "borderWidth": {"type": "number"},
                    "rotation": {"type": "integer"},
                },
            },
        },
        "effects": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "kind", "owner", "note"],
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string", "enum": list(schemas.EFFECT_KINDS)},
                    "owner": {"type": "string", "enum": list(schemas.EFFECT_OWNERS)},
                    "note": {"type": ["string", "null"]},
                },
            },
        },
        "components": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "kind", "layerId", "pairsWith", "note"],
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string", "enum": list(schemas.COMPONENT_KINDS)},
                    "layerId": {"type": "string"},
                    "pairsWith": {"type": ["string", "null"]},
                    "note": {"type": ["string", "null"]},
                },
            },
        },
    },
}


def run(job_id, attempt, asset_id=None):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.EXTRACT_SCENE)

    copy_key = job["artifacts"][Stage.PREPARE_COPY]["copy"]
    copy_items = artifacts.get_json(job_id, copy_key)["items"]
    approved = jobs.approved_concept(job)
    concept_key = approved["key"]
    concept_bytes = artifacts.get_bytes(job_id, concept_key)

    canvas = {"width": int(job["canvasWidth"]), "height": int(job["canvasHeight"])}

    approved = {i["contentKey"]: i["text"] for i in copy_items}

    def describe(correction=""):
        prompt = prompts.render(
            "extract-scene",
            width=canvas["width"],
            height=canvas["height"],
            fonts=", ".join(conf.APPROVED_FONT_FAMILIES),
            copy_block=prompts.copy_block(copy_items),
            correction_block=correction,
        )
        raw = vision.analyze(
            prompt,
            (concept_bytes,),
            schema=SCENE_SCHEMA,
            schema_name="scene_v1",
            model=conf.SCENE_MODEL,
            operation_id=f"{job_id}:extract_scene:{attempt}"
                         + (":retry" if correction else ""),
            detail="original",  # Do not change. See the module docstring.
        )
        scene = schemas.parse_scene(
            raw,
            canvas["width"],
            canvas["height"],
            allowed_content_keys=set(approved),
            allowed_fonts=set(conf.APPROVED_FONT_FAMILIES),
        )
        check_copy_is_verbatim(scene, approved)
        check_required_layers(scene)
        return scene

    try:
        scene = describe()
    except InvalidScene as first:
        # One re-ask, carrying the exact complaint. The per-kind shape rules are
        # the part a describing model gets wrong most often (a fifth gradient
        # stop, a chevron with no border), and they are rules the model can obey
        # once it is told which field broke. A second failure is still the job's
        # failure: the original message is what the caller sees, because the
        # retry is an attempt to recover from it rather than a separate fault.
        log.info(
            "scene rejected, re-asking once jobId=%s problem=%s", job_id, first.message)
        try:
            scene = describe(
                "CORRECTION: your previous answer was rejected: "
                f"{first.message}\nReturn the whole document again, fixed. If an "
                "element cannot satisfy the rule for its kind, leave it out of "
                "shapeLayers and account for it as a retainedRaster component."
            )
        except InvalidScene:
            raise first from None

    document = scene.model_dump(mode="json")

    # The shapes are about to be drawn in CSS from these colours, so they are
    # measured off the approved concept rather than taken on trust. Geometry is
    # left as reported: see lib/concept_sample.py.
    measured = concept_sample.measure_shape_colors(concept_bytes, document)
    corrections, ignored = measured["corrections"], measured["ignored"]
    if corrections or ignored:
        log.info(
            "shape colours measured jobId=%s corrected=%d ignored=%d",
            job_id, len(corrections), len(ignored),
        )

    # Relationships the concept shows, which a model reading each box on its own
    # does not carry: text that shares an edge, shapes that share a fill. Both are
    # measured from pixels and recorded; ASSEMBLE turns them into positions.
    document["alignmentGroups"] = alignment.detect_groups(concept_bytes, document)
    document["fillGroups"] = alignment.fill_groups(document)
    document = schemas.parse_scene(          # nothing unvalidated is stored
        document,
        canvas["width"],
        canvas["height"],
        allowed_content_keys=set(approved),
        allowed_fonts=set(conf.APPROVED_FONT_FAMILIES),
    ).model_dump(mode="json")

    key = artifacts.artifact_key(job_id, Stage.EXTRACT_SCENE, attempt, "scene.json")
    stored = artifacts.put_json(key, document)
    recorded = {
        # Which concept the scene describes, so FINALIZE can prove every stage
        # used the one that was selected.
        "scene": stored["key"],
        "conceptSha256": artifacts.sha256_hex(concept_bytes),
        "components": len(document.get("components") or []),
        "shapeLayers": len(document.get("shapeLayers") or []),
        "alignmentGroups": len(document.get("alignmentGroups") or []),
        "fillGroups": len(document.get("fillGroups") or []),
    }
    if corrections or ignored:
        # Kept beside the scene rather than inside it: the scene is the design,
        # and this is the evidence of how it was adjusted.
        colors_key = artifacts.artifact_key(
            job_id, Stage.EXTRACT_SCENE, attempt, "scene-colors.json")
        recorded["colorCorrections"] = artifacts.put_json(
            colors_key, {"corrections": corrections, "ignored": ignored})["key"]
    jobs.record_artifacts(job_id, Stage.EXTRACT_SCENE, attempt, recorded)
    return {
        "scene": stored["key"],
        "textLayers": len(scene.textLayers),
        "imageLayers": len(scene.imageLayers),
        # The workflow's Map iterates this directly, so the bound is applied
        # here rather than in ASL, where exceeding it would be a cryptic
        # States.* error instead of a clear refusal.
        "independentAssetIds": independent.plan_from_scene(document, job_id),
    }


def check_copy_is_verbatim(scene, approved):
    """Every text layer must quote the copy manifest exactly.

    parse_scene already confirms the contentKey exists; this confirms the text
    was not paraphrased on the way into the layout, which would quietly undo
    PREPARE_COPY's grounding guarantees.
    """
    for layer in scene.textLayers:
        if layer.text != approved[layer.contentKey]:
            raise InvalidScene(
                f"Text layer {layer.id!r} altered the approved copy for "
                f"{layer.contentKey!r}."
            )


def check_required_layers(scene):
    """A background and a decoration layer are what the separation produces.

    A scene missing either would assemble into HTML that renders, but with no
    photograph or no artwork - a silent content failure rather than an error.
    """
    types = [layer.type for layer in scene.imageLayers]
    for required in ("background", "decoration"):
        if types.count(required) != 1:
            raise InvalidScene(
                f"The scene must contain exactly one {required} layer; "
                f"found {types.count(required)}."
            )
