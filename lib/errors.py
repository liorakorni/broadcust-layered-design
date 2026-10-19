"""Stage names, error codes and the single error shape used across the service.

Every failure anywhere in the service — API validation, a stage, a provider — is
reported as the same object, because SPEC sections 2.3, 2.6 and 4 all consume it:

    {"stage": "...", "code": "...", "message": "...", "retryable": bool}

Nothing else may invent an error shape. Import from here.
"""

from __future__ import annotations


class Stage:
    """Workflow stage names, in execution order.

    These strings are reported to callers as real progress (SPEC section 2.4
    forbids fabricated percentages), used in artifact keys, and matched by the
    Step Functions definition. They are part of the public contract.
    """

    ACCEPT_REQUEST = "ACCEPT_REQUEST"
    PREFLIGHT = "PREFLIGHT"
    PREPARE_COPY = "PREPARE_COPY"
    ANALYZE_REFERENCE = "ANALYZE_REFERENCE"
    CONCEPT = "CONCEPT"
    REVIEW_CONCEPT = "REVIEW_CONCEPT"
    EXTRACT_SCENE = "EXTRACT_SCENE"
    CLEAN_PHOTO = "CLEAN_PHOTO"
    DECORATION_OVERLAY = "DECORATION_OVERLAY"
    INDEPENDENT_ASSETS = "INDEPENDENT_ASSETS"
    VALIDATE_ASSETS = "VALIDATE_ASSETS"
    ASSEMBLE = "ASSEMBLE"
    RENDER = "RENDER"
    REVIEW = "REVIEW"
    FINALIZE = "FINALIZE"

    #: Pipeline order. ACCEPT_REQUEST is excluded: it happens in the API Lambda
    #: before the workflow exists.
    PIPELINE = (
        PREFLIGHT,
        PREPARE_COPY,
        ANALYZE_REFERENCE,
        CONCEPT,
        REVIEW_CONCEPT,
        EXTRACT_SCENE,
        CLEAN_PHOTO,
        DECORATION_OVERLAY,
        INDEPENDENT_ASSETS,
        VALIDATE_ASSETS,
        ASSEMBLE,
        RENDER,
        REVIEW,
        FINALIZE,
    )

    ALL = (ACCEPT_REQUEST,) + PIPELINE


class Status:
    """Job statuses. SPEC section 2.4 allows exactly these four."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"

    ALL = (QUEUED, RUNNING, SUCCEEDED, FAILED)
    TERMINAL = (SUCCEEDED, FAILED)


class Code:
    """Error codes. Keep these stable; callers branch on them."""

    # Request acceptance
    INVALID_REQUEST = "INVALID_REQUEST"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    UNAUTHORIZED = "UNAUTHORIZED"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"

    # Configuration and capability
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    CONFIGURATION_INVALID = "CONFIGURATION_INVALID"
    CAPABILITY_MISSING = "CAPABILITY_MISSING"
    TRANSPARENCY_UNSUPPORTED = "TRANSPARENCY_UNSUPPORTED"

    # Providers
    PROVIDER_ERROR = "PROVIDER_ERROR"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_OUTCOME_UNKNOWN = "PROVIDER_OUTCOME_UNKNOWN"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    INVALID_MODEL_JSON = "INVALID_MODEL_JSON"

    # Content and assets
    IMPOSSIBLE_CONTENT_REQUIREMENT = "IMPOSSIBLE_CONTENT_REQUIREMENT"
    COPY_INVALID = "COPY_INVALID"
    INVALID_SCENE = "INVALID_SCENE"
    INVALID_TRANSPARENCY = "INVALID_TRANSPARENCY"
    ASSET_INVALID = "ASSET_INVALID"
    REFERENCE_FETCH_FAILED = "REFERENCE_FETCH_FAILED"
    REFERENCE_ADHERENCE_FAILED = "REFERENCE_ADHERENCE_FAILED"
    REFERENCE_CONSTRAINT_CONFLICT = "REFERENCE_CONSTRAINT_CONFLICT"

    # Assembly and delivery
    ASSEMBLY_FAILED = "ASSEMBLY_FAILED"
    RENDER_FAILED = "RENDER_FAILED"
    FONT_MISSING = "FONT_MISSING"
    REVIEW_BLOCKED = "REVIEW_BLOCKED"
    CORRECTION_EXHAUSTED = "CORRECTION_EXHAUSTED"
    MEDIA_DELIVERY_UNAVAILABLE = "MEDIA_DELIVERY_UNAVAILABLE"
    FINAL_HASH_MISMATCH = "FINAL_HASH_MISMATCH"

    INTERNAL = "INTERNAL"


class LayeredDesignError(Exception):
    """Base for every failure the service reports to a caller.

    `message` is caller-visible. Keep it free of secrets, signed URLs and raw
    provider payloads; put diagnostic detail in logs instead.
    """

    stage: str = Stage.ACCEPT_REQUEST
    code: str = Code.INTERNAL
    retryable: bool = False

    def __init__(self, message, stage=None, code=None, retryable=None):
        super().__init__(message)
        self.message = message
        if stage is not None:
            self.stage = stage
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable

    def to_dict(self):
        return {
            "stage": self.stage,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


class InvalidRequest(LayeredDesignError):
    stage = Stage.ACCEPT_REQUEST
    code = Code.INVALID_REQUEST


class IdempotencyConflict(LayeredDesignError):
    stage = Stage.ACCEPT_REQUEST
    code = Code.IDEMPOTENCY_CONFLICT


class Unauthorized(LayeredDesignError):
    stage = Stage.ACCEPT_REQUEST
    code = Code.UNAUTHORIZED


class JobNotFound(LayeredDesignError):
    stage = Stage.ACCEPT_REQUEST
    code = Code.JOB_NOT_FOUND


class NotImplementedStage(LayeredDesignError):
    """Raised by a stage that has no implementation yet.

    Invariant 1: a stub never reports success. This exists so an unfinished
    pipeline fails loudly at the real stage name rather than returning a
    plausible-looking empty result.
    """

    code = Code.NOT_IMPLEMENTED

    def __init__(self, stage):
        super().__init__(
            f"Stage {stage} is not implemented yet.", stage=stage, retryable=False
        )


class ConfigurationInvalid(LayeredDesignError):
    stage = Stage.PREFLIGHT
    code = Code.CONFIGURATION_INVALID


class CapabilityMissing(LayeredDesignError):
    stage = Stage.PREFLIGHT
    code = Code.CAPABILITY_MISSING


class TransparencyUnsupported(LayeredDesignError):
    """The configured image model will not return an alpha channel.

    Invariant 7: this is terminal. Never fall back to keying out white, and never
    continue with an opaque overlay.
    """

    stage = Stage.DECORATION_OVERLAY
    code = Code.TRANSPARENCY_UNSUPPORTED


class ProviderError(LayeredDesignError):
    code = Code.PROVIDER_ERROR
    retryable = True


class ProviderTimeout(ProviderError):
    code = Code.PROVIDER_TIMEOUT


class ProviderRateLimited(ProviderError):
    code = Code.PROVIDER_RATE_LIMITED


class ProviderOutcomeUnknown(LayeredDesignError):
    """A paid generation may or may not have happened.

    SPEC section 6: fail rather than blindly pay twice. Not retryable, because a
    retry is exactly the thing that would double-charge.
    """

    code = Code.PROVIDER_OUTCOME_UNKNOWN
    retryable = False


class InvalidModelJson(LayeredDesignError):
    code = Code.INVALID_MODEL_JSON
    retryable = True


class AssemblyFailed(LayeredDesignError):
    """A scene value could not be safely emitted into HTML."""

    stage = Stage.ASSEMBLE
    code = Code.ASSEMBLY_FAILED
    retryable = False


class ImpossibleContentRequirement(LayeredDesignError):
    """The design asks for more than the service will produce - typically more
    generated assets than the bound allows. A caller-fixable condition."""

    stage = Stage.INDEPENDENT_ASSETS
    code = Code.IMPOSSIBLE_CONTENT_REQUIREMENT
    retryable = False


class CopyInvalid(LayeredDesignError):
    """The copy manifest broke a grounding rule: invented facts, dropped
    requiredTexts or merged contact details."""

    stage = Stage.PREPARE_COPY
    code = Code.COPY_INVALID
    retryable = True


class InvalidScene(LayeredDesignError):
    stage = Stage.EXTRACT_SCENE
    code = Code.INVALID_SCENE


class InvalidTransparency(LayeredDesignError):
    """The overlay decoded, but its alpha channel is unusable."""

    stage = Stage.VALIDATE_ASSETS
    code = Code.INVALID_TRANSPARENCY


class OverlayRepairNeeded(InvalidTransparency):
    """The overlay is unusable but a bounded repair is still available.

    A separate class rather than a flag: Step Functions can only branch on the
    exception class name, so this is what lets the workflow route to a repair
    instead of failing the job. Once the repairs allowed by SPEC section 4 are
    spent, plain InvalidTransparency is raised and the job ends.
    """

    retryable = True


class AssetInvalid(LayeredDesignError):
    stage = Stage.VALIDATE_ASSETS
    code = Code.ASSET_INVALID


class ReferenceFetchFailed(LayeredDesignError):
    """A supplied image could not be retrieved. Transient, unlike AssetInvalid,
    which means the image itself is unusable."""

    stage = Stage.CONCEPT
    code = Code.REFERENCE_FETCH_FAILED
    retryable = True


class RenderFailed(LayeredDesignError):
    stage = Stage.RENDER
    code = Code.RENDER_FAILED


class FontMissing(LayeredDesignError):
    """SPEC section 5: a missing font is blocking, never a warning."""

    stage = Stage.RENDER
    code = Code.FONT_MISSING


class ReviewBlocked(LayeredDesignError):
    stage = Stage.REVIEW
    code = Code.REVIEW_BLOCKED


class CorrectionExhausted(LayeredDesignError):
    stage = Stage.REVIEW
    code = Code.CORRECTION_EXHAUSTED


class ReferenceAdherenceFailed(LayeredDesignError):
    """The concept still violates the reference policy after the bounded
    concept-repair rounds (SPEC section REVIEW_CONCEPT)."""

    stage = Stage.REVIEW_CONCEPT
    code = Code.REFERENCE_ADHERENCE_FAILED
    retryable = False


class ReferenceConstraintConflict(LayeredDesignError):
    """Mandatory content cannot coexist with the strict reference layout.

    Terminal: the service never silently redesigns, drops content or raises
    designFreedom to resolve it (SPEC section 2.7).
    """

    stage = Stage.REVIEW_CONCEPT
    code = Code.REFERENCE_CONSTRAINT_CONFLICT
    retryable = False


class MediaDeliveryUnavailable(LayeredDesignError):
    """Stable media hosting failed.

    SPEC section 4: fail finalization explicitly rather than persisting a customer
    design whose image URLs will expire.
    """

    stage = Stage.FINALIZE
    code = Code.MEDIA_DELIVERY_UNAVAILABLE
    retryable = True


class FinalHashMismatch(LayeredDesignError):
    """The delivered HTML no longer matches what ASSEMBLE produced.

    Never retryable: a mismatch means an artifact changed underneath the job,
    and re-running would deliver the altered bytes rather than detect them.
    """

    stage = Stage.FINALIZE
    code = Code.FINAL_HASH_MISMATCH
    retryable = False


def as_error_dict(exc, default_stage=Stage.ACCEPT_REQUEST):
    """Coerce any exception into the standard error shape.

    Unexpected exceptions become INTERNAL with a generic message: their text may
    contain provider payloads or credentials and must not reach a caller.
    """
    if isinstance(exc, LayeredDesignError):
        return exc.to_dict()
    return {
        "stage": default_stage,
        "code": Code.INTERNAL,
        "message": "Internal error.",
        "retryable": False,
    }
