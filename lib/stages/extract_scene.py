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

import conf
from lib import artifacts, jobs, prompts, schemas
from lib.errors import InvalidScene, Stage
from lib.stages import independent
from lib.providers import vision

_BOX = {
    "x": {"type": "integer"},
    "y": {"type": "integer"},
    "width": {"type": "integer"},
    "height": {"type": "integer"},
}

SCENE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["canvas", "textLayers", "imageLayers", "overlaySamples"],
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
    },
}


def run(job_id, attempt, asset_id=None):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.EXTRACT_SCENE)

    copy_key = job["artifacts"][Stage.PREPARE_COPY]["copy"]
    copy_items = artifacts.get_json(job_id, copy_key)["items"]
    concept_key = jobs.approved_concept(job)["key"]
    concept_bytes = artifacts.get_bytes(job_id, concept_key)

    canvas = {"width": int(job["canvasWidth"]), "height": int(job["canvasHeight"])}

    prompt = prompts.render(
        "extract-scene",
        width=canvas["width"],
        height=canvas["height"],
        fonts=", ".join(conf.APPROVED_FONT_FAMILIES),
        copy_block=prompts.copy_block(copy_items),
    )

    raw = vision.analyze(
        prompt,
        (concept_bytes,),
        schema=SCENE_SCHEMA,
        schema_name="scene_v1",
        model=conf.SCENE_MODEL,
        operation_id=f"{job_id}:extract_scene:{attempt}",
        detail="original",  # Do not change. See the module docstring.
    )

    approved = {i["contentKey"]: i["text"] for i in copy_items}
    scene = schemas.parse_scene(
        raw,
        canvas["width"],
        canvas["height"],
        allowed_content_keys=set(approved),
        allowed_fonts=set(conf.APPROVED_FONT_FAMILIES),
    )
    check_copy_is_verbatim(scene, approved)
    check_required_layers(scene)

    document = scene.model_dump(mode="json")
    key = artifacts.artifact_key(job_id, Stage.EXTRACT_SCENE, attempt, "scene.json")
    stored = artifacts.put_json(key, document)
    jobs.record_artifacts(
        job_id, Stage.EXTRACT_SCENE, attempt, {"scene": stored["key"]}
    )
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
