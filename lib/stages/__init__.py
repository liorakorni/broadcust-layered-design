"""Stage implementations.

`build_registry()` is the honest statement of what the pipeline can currently
do. A stage absent from the returned mapping raises NotImplementedStage rather
than returning a fake success (invariant 1).

The mapping is built by a function rather than at import time so this package
never imports handler_stage, which imports it.
"""

from lib.errors import Stage


def build_registry():
    from lib.stages import (
        analyze_reference,
        assemble,
        concept,
        extract_scene,
        independent,
        plan_layout,
        preflight,
        prepare_copy,
        review,
        review_concept,
        reuse_concept,
        separation,
        validate,
    )

    return {
        Stage.PREFLIGHT: preflight.run,
        Stage.PREPARE_COPY: prepare_copy.run,
        Stage.ANALYZE_REFERENCE: analyze_reference.run,
        Stage.CONCEPT: concept.run,
        Stage.REVIEW_CONCEPT: review_concept.run,
        Stage.REUSE_CONCEPT: reuse_concept.run,
        Stage.PLAN_LAYOUT: plan_layout.run,
        Stage.GENERATE_PHOTO: plan_layout.generate_photo,
        Stage.EXTRACT_SCENE: extract_scene.run,
        Stage.CLEAN_PHOTO: separation.clean_photo,
        Stage.DECORATION_OVERLAY: separation.decoration_overlay,
        Stage.INDEPENDENT_ASSETS: independent.run,
        Stage.VALIDATE_ASSETS: validate.run,
        Stage.ASSEMBLE: assemble.assemble,
        Stage.RENDER: assemble.render,
        Stage.REVIEW: review.run,
        Stage.FINALIZE: review.finalize,
    }
