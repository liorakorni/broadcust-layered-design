"""Request, scene and review validation.

Three documents cross a trust boundary and each is validated here:

  StartRequest  - from the caller          (SPEC section 2.1)
  Scene         - from the vision model    (SPEC section EXTRACT_SCENE)
  ReviewVerdict - from the vision model    (SPEC section REVIEW)

All three are treated as input data, never as instructions. Unknown structural
fields are rejected rather than ignored, so a silently-dropped field can't change
the meaning of a design.
"""

from __future__ import annotations

import json
import re
from typing import Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

import conf
from lib import review_policy
from lib.errors import InvalidRequest, InvalidScene

HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
FONT_NAME = re.compile(r"^[A-Za-z0-9 _-]{1,64}$")
ASSET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Must accept every key PREPARE_COPY accepts (lib/stages/prepare_copy.CONTENT_KEY,
# which allows underscores). The two disagreeing is what failed a live job at
# EXTRACT_SCENE on `service.custom_itinerary`, a key the copy stage had approved.
# camelCase stays allowed here so keys that were valid before keep validating.
CONTENT_KEY = re.compile(r"^[a-z][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)*$")

#: SPEC section 5. Roles exist to describe real content; none of them licenses
#: inventing a supporting fact to fill the slot.
ALLOWED_ROLES = (
    "businessName",
    "businessDescriptor",
    "title",
    "subtitle",
    "serviceItem",
    "expertiseItem",
    "benefitItem",
    "informationItem",
    "eventDetail",
    "offerDetail",
    "cta",
    "userInformation",
    "icon",
)

ALLOWED_ASSET_KINDS = ("logo", "photo", "product", "portrait")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------
# Start request (SPEC section 2.1)
# --------------------------------------------------------------------------


def check_authorized_origin(url):
    """Return `url` if it is an https URL on an authorized origin, else raise.

    SPEC section 6: reference and supplied images are fetched only from
    configured origins. Shared by request validation and PREFLIGHT so there is
    one rule rather than two that can drift apart.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"image URLs must be https: {url!r}")
    if parsed.hostname not in conf.AUTHORIZED_IMAGE_ORIGINS:
        raise ValueError(f"host {parsed.hostname!r} is not an authorized origin")
    return url


class Business(Strict):
    name: Optional[str] = Field(default=None, max_length=200)
    phone: Optional[str] = Field(default=None, max_length=40)
    email: Optional[str] = Field(default=None, max_length=200)
    website: Optional[str] = Field(default=None, max_length=500)

    @field_validator("website")
    @classmethod
    def _https_website(cls, v):
        if v and urlparse(v).scheme not in ("http", "https"):
            raise ValueError("website must be an http(s) URL")
        return v


class Brief(Strict):
    post: str = Field(min_length=1, max_length=conf.MAX_POST_CHARS)
    business: Optional[Business] = None
    instructions: Optional[str] = Field(default=None, max_length=4000)
    #: Strings that must appear unchanged in the output.
    requiredTexts: list[str] = Field(default_factory=list, max_length=40)

    @field_validator("post")
    @classmethod
    def _non_blank(cls, v):
        if not v.strip():
            raise ValueError("brief.post must not be blank")
        return v

    @field_validator("requiredTexts")
    @classmethod
    def _non_blank_required(cls, v):
        for item in v:
            if not item or not item.strip():
                raise ValueError("requiredTexts entries must not be blank")
            if len(item) > 500:
                raise ValueError("requiredTexts entries must be <= 500 characters")
        return v


class Canvas(Strict):
    width: int = conf.DEFAULT_CANVAS_WIDTH
    height: int = conf.DEFAULT_CANVAS_HEIGHT

    @field_validator("width", "height")
    @classmethod
    def _in_range(cls, v):
        if not conf.MIN_CANVAS <= v <= conf.MAX_CANVAS:
            raise ValueError(
                f"canvas dimensions must be {conf.MIN_CANVAS}..{conf.MAX_CANVAS}"
            )
        return v


class Brand(Strict):
    colors: list[str] = Field(default_factory=list, max_length=12)
    fontFamilies: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("colors")
    @classmethod
    def _hex(cls, v):
        for c in v:
            if not HEX_COLOR.match(c):
                raise ValueError(f"invalid brand colour {c!r}; use #RGB or #RRGGBB")
        return v

    @field_validator("fontFamilies")
    @classmethod
    def _font_names(cls, v):
        for f in v:
            if not FONT_NAME.match(f):
                raise ValueError(f"invalid font family name {f!r}")
        return v


class SuppliedAsset(Strict):
    id: str
    kind: Literal["logo", "photo", "product", "portrait"]
    url: str

    @field_validator("id")
    @classmethod
    def _id(cls, v):
        if not ASSET_ID.match(v):
            raise ValueError(f"invalid asset id {v!r}")
        return v

    @field_validator("url")
    @classmethod
    def _authorized_origin(cls, v):
        return check_authorized_origin(v)


class StartRequest(Strict):
    schemaVersion: str = "1.0"
    requestId: str = Field(min_length=1, max_length=128)
    brief: Brief
    canvas: Canvas = Field(default_factory=Canvas)
    referenceImageUrl: str
    brand: Optional[Brand] = None
    suppliedAssets: list[SuppliedAsset] = Field(
        default_factory=list, max_length=conf.MAX_SUPPLIED_ASSETS
    )
    # SPEC section 2.7. Absent -> 0 (default, validators do not run); explicit
    # null is rejected by the validator below rather than defaulted.
    designFreedom: int = 0
    # How strictly the concept is judged, independent of how freely it is
    # generated. Absent -> "balanced"; explicit null or an unknown value is
    # rejected rather than defaulted.
    reviewStrictness: str = review_policy.DEFAULT_STRICTNESS

    @field_validator("reviewStrictness", mode="before")
    @classmethod
    def _review_strictness(cls, v):
        try:
            return review_policy.validate_strictness(v)
        except ValueError as exc:
            raise ValueError(str(exc)) from None

    @field_validator("designFreedom", mode="before")
    @classmethod
    def _design_freedom(cls, v):
        # bool is an int subclass, so it must be rejected explicitly. "10" and
        # 10.5 would otherwise be coerced by pydantic's default lax mode.
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("designFreedom must be an integer from 0 through 100")
        if not 0 <= v <= 100:
            raise ValueError("designFreedom must be an integer from 0 through 100")
        return v

    @field_validator("requestId")
    @classmethod
    def _request_id(cls, v):
        if not ASSET_ID.match(v):
            raise ValueError("requestId must be alphanumeric with . _ -")
        return v

    @field_validator("referenceImageUrl")
    @classmethod
    def _reference(cls, v):
        return check_authorized_origin(v)

    @field_validator("suppliedAssets")
    @classmethod
    def _unique_ids(cls, v):
        ids = [a.id for a in v]
        if len(ids) != len(set(ids)):
            raise ValueError("suppliedAssets ids must be unique")
        return v


def parse_start_request(payload):
    """Validate a start request, raising InvalidRequest with a readable message.

    The whole-payload size check comes first: a 10 MB body should be refused
    before pydantic walks it.
    """
    if not isinstance(payload, dict):
        raise InvalidRequest("Request must be a JSON object.")
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(encoded) > conf.MAX_REQUEST_BYTES:
        raise InvalidRequest(
            f"Request exceeds {conf.MAX_REQUEST_BYTES} bytes."
        )
    try:
        return StartRequest.model_validate(payload)
    except ValidationError as exc:
        raise InvalidRequest(_first_error(exc)) from exc


def canonical_request_hash(request: "StartRequest") -> str:
    """Stable hash of request content, for caller-scoped idempotency.

    Keys are sorted and defaults are materialised, so two payloads that mean the
    same thing hash the same and a reused requestId with different content is
    detectable as a conflict.
    """
    import hashlib

    body = request.model_dump(mode="json")
    # An omitted reviewStrictness and an explicit default mean the same thing, and
    # a request accepted before the field existed must keep hashing the way it
    # did, or replaying it would read as an idempotency conflict.
    if body.get("reviewStrictness") == review_policy.DEFAULT_STRICTNESS:
        body.pop("reviewStrictness")
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


# --------------------------------------------------------------------------
# Scene document (SPEC section EXTRACT_SCENE)
# --------------------------------------------------------------------------


class TextLayer(Strict):
    id: str
    type: Literal["text"] = "text"
    role: str
    contentKey: str
    text: str
    x: int
    y: int
    width: int
    height: int
    zIndex: int
    fontFamily: str
    fontSize: int
    fontWeight: int = 400
    color: str
    align: Literal["left", "right", "center"] = "right"
    direction: Literal["rtl", "ltr"] = "rtl"
    #: Dimensionless multiplier, never pixels.
    lineHeight: float = 1.2

    @field_validator("role")
    @classmethod
    def _role(cls, v):
        if v not in ALLOWED_ROLES:
            raise ValueError(f"role {v!r} is not an allowed role")
        return v

    @field_validator("contentKey")
    @classmethod
    def _content_key(cls, v):
        if not CONTENT_KEY.match(v):
            raise ValueError(f"invalid contentKey {v!r}")
        return v

    @field_validator("color")
    @classmethod
    def _color(cls, v):
        if not HEX_COLOR.match(v):
            raise ValueError(f"invalid colour {v!r}")
        return v

    @field_validator("fontSize")
    @classmethod
    def _font_size(cls, v):
        if not 4 <= v <= 512:
            raise ValueError("fontSize must be 4..512 px")
        return v

    @field_validator("lineHeight")
    @classmethod
    def _line_height(cls, v):
        # A multiplier above 10 almost certainly means pixels were sent instead.
        if not 0.5 <= v <= 4.0:
            raise ValueError(
                "lineHeight is a dimensionless multiplier and must be 0.5..4.0"
            )
        return v

    @field_validator("zIndex")
    @classmethod
    def _z(cls, v):
        if not 0 <= v <= 100:
            raise ValueError("zIndex must be 0..100")
        return v


class ImageLayer(Strict):
    id: str
    type: Literal["background", "decoration", "image"]
    assetId: Optional[str] = None
    x: int
    y: int
    width: int
    height: int
    zIndex: int
    objectFit: Literal["cover", "contain"] = "contain"
    objectPosition: Optional[str] = None
    radius: Optional[int] = None

    @field_validator("zIndex")
    @classmethod
    def _z(cls, v):
        if not 0 <= v <= 100:
            raise ValueError("zIndex must be 0..100")
        return v


class SampleRegion(Strict):
    """A region the overlay must be transparent or opaque in.

    These come from the scene rather than a global threshold, which is what makes
    overlay validation meaningful for dense and sparse designs alike.
    """

    kind: Literal["transparent", "opaque"]
    x: int
    y: int
    width: int
    height: int


class Scene(Strict):
    canvas: Canvas
    textLayers: list[TextLayer] = Field(default_factory=list, max_length=60)
    imageLayers: list[ImageLayer] = Field(default_factory=list, max_length=20)
    overlaySamples: list[SampleRegion] = Field(default_factory=list, max_length=20)


def parse_scene(payload, canvas_width, canvas_height, allowed_content_keys=None,
                allowed_fonts=None):
    """Validate a scene document against the real canvas and approved copy.

    Beyond shape, this enforces the things a model gets wrong in ways that render
    without error: out-of-bounds geometry, duplicate IDs, invented content keys and
    fonts that are not actually installed.
    """
    try:
        scene = Scene.model_validate(payload)
    except ValidationError as exc:
        raise InvalidScene(_first_error(exc)) from exc

    if scene.canvas.width != canvas_width or scene.canvas.height != canvas_height:
        raise InvalidScene(
            f"Scene canvas {scene.canvas.width}x{scene.canvas.height} does not match "
            f"the requested {canvas_width}x{canvas_height}."
        )

    seen_ids = set()
    seen_keys = set()
    for layer in list(scene.textLayers) + list(scene.imageLayers):
        if layer.id in seen_ids:
            raise InvalidScene(f"Duplicate layer id {layer.id!r}.")
        seen_ids.add(layer.id)
        if layer.width <= 0 or layer.height <= 0:
            raise InvalidScene(f"Layer {layer.id!r} has a non-positive size.")
        if layer.x < 0 or layer.y < 0:
            raise InvalidScene(f"Layer {layer.id!r} starts outside the canvas.")
        if layer.x + layer.width > canvas_width or layer.y + layer.height > canvas_height:
            raise InvalidScene(
                f"Layer {layer.id!r} extends beyond the {canvas_width}x{canvas_height} "
                "canvas."
            )

    for layer in scene.textLayers:
        if layer.contentKey in seen_keys:
            raise InvalidScene(f"Duplicate contentKey {layer.contentKey!r}.")
        seen_keys.add(layer.contentKey)
        if allowed_content_keys is not None and layer.contentKey not in allowed_content_keys:
            raise InvalidScene(
                f"contentKey {layer.contentKey!r} is not in the approved copy manifest."
            )
        if allowed_fonts is not None and layer.fontFamily not in allowed_fonts:
            raise InvalidScene(
                f"fontFamily {layer.fontFamily!r} is not an installed, approved font."
            )
    return scene


# --------------------------------------------------------------------------
# Review verdict (SPEC section REVIEW)
# --------------------------------------------------------------------------


class NormalizedBox(Strict):
    """Canvas-independent geometry: every value is a 0..1 fraction."""

    x: float
    y: float
    width: float
    height: float

    @field_validator("x", "y", "width", "height")
    @classmethod
    def _unit(cls, v):
        if not 0.0 <= v <= 1.0:
            raise ValueError("box values must be between 0 and 1")
        return v


class BlockingIssue(Strict):
    """A finding from the final review.

    The structured fields are optional here so a review written before they
    existed still parses; the model's schema requires them.
    """

    code: str
    layerId: Optional[str] = None
    assetId: Optional[str] = None
    message: str
    category: Optional[Literal["visual", "content", "technical"]] = None
    severity: Optional[Literal["warning", "blocking"]] = None
    affected: Optional[str] = None
    impact: Optional[str] = None
    suggestion: Optional[str] = None
    region: Optional[NormalizedBox] = None


class ReviewVerdict(Strict):
    # All three are required, matching the strict json_schema sent to the model.
    # A response missing one means the call did not go through structured outputs,
    # so it is rejected rather than defaulted - defaulting blockingIssues to []
    # would silently turn a malformed response into an approval.
    passed: bool
    blockingIssues: list[BlockingIssue] = Field(max_length=40)
    # Non-blocking findings. Defaults to empty so an older response still parses;
    # a warning can never block, so omitting them is the safe direction.
    warnings: list[BlockingIssue] = Field(default_factory=list, max_length=40)
    suggestions: list[str] = Field(max_length=40)
    # Defaults to False, the safe direction: an absent flag routes to the ordinary
    # HTML correction rather than regenerating a paid concept.
    conceptDefect: bool = False

    @property
    def approved(self) -> bool:
        """Backend approval requires zero blocking issues.

        SPEC section REVIEW: a model asserting passed:true does not override
        blocking issues it reported in the same breath.
        """
        return self.passed and not self.blockingIssues


def parse_review(payload):
    try:
        return ReviewVerdict.model_validate(payload)
    except ValidationError as exc:
        from lib.errors import InvalidModelJson

        raise InvalidModelJson(_first_error(exc)) from exc


# --------------------------------------------------------------------------
# Reference analysis and concept review (SPEC sections ANALYZE_REFERENCE and
# REVIEW_CONCEPT). Both come from the vision model and are treated as data.
# Strict structured outputs cannot express numeric ranges, so they are enforced
# here instead.
# --------------------------------------------------------------------------

ANCHOR_KINDS = ("dominantImage", "graphicPanel", "textZone", "logo", "other")
#: What the model may declare. Historical reviews used a different vocabulary;
#: review_policy.LEGACY_CATEGORY maps it for anything that still reads them.
ISSUE_CATEGORIES = review_policy.ISSUE_CATEGORIES
ISSUE_SEVERITIES = review_policy.SEVERITIES


class ReferenceAnchor(Strict):
    id: str = Field(min_length=1, max_length=64)
    kind: Literal["dominantImage", "graphicPanel", "textZone", "logo", "other"]
    box: NormalizedBox
    relativeScale: float
    confidence: float

    @field_validator("relativeScale", "confidence")
    @classmethod
    def _unit(cls, v):
        if not 0.0 <= v <= 1.0:
            raise ValueError("value must be between 0 and 1")
        return v


class CopyPlacement(Strict):
    """Where one approved copy item is planned to sit.

    Planned once, from the reference, then reused unchanged by generation, review
    and every correction. Without it each attempt chooses its own place for text
    that has no counterpart in the reference (a business name, usually), and a
    correction moves it somewhere new instead of putting it back.
    """

    contentKey: str
    box: NormalizedBox
    basis: str = Field(min_length=1, max_length=200)


class ReferenceAnalysis(Strict):
    # No font-identification field on purpose: a font cannot be named from pixels.
    anchors: list[ReferenceAnchor] = Field(max_length=40)
    copyPlacements: list[CopyPlacement] = Field(max_length=40)
    hierarchy: list[str] = Field(max_length=40)
    overlaps: list[str] = Field(max_length=40)
    alignment: str
    readingFlow: str
    palette: list[str] = Field(max_length=20)
    visualQualities: list[str] = Field(max_length=40)
    notes: str


class ConceptCheck(Strict):
    anchorId: Optional[str]
    feature: str
    expectedPolicy: str
    observed: str
    passed: bool


class ConceptIssue(Strict):
    """One finding, with the evidence the backend needs to weigh it.

    `severity` is the model's proposal; the backend applies its own rules to it
    (review_policy.normalize_issue). `region` is where the problem is, so a
    correction can edit just there instead of regenerating the frame.
    """

    code: str
    category: Literal["visual", "content", "technical"]
    severity: Literal["warning", "blocking"]
    affected: str
    impact: str
    suggestion: Optional[str]
    region: Optional[NormalizedBox]


class CopyCheck(Strict):
    contentKey: str
    present: bool
    correct: bool
    readable: bool
    observed: str


class ConstraintConflict(Strict):
    conflicted: bool
    reason: Optional[str]


class ConceptReviewVerdict(Strict):
    # No `passed`: approval is computed by the backend from the evidence below,
    # so there is no model-asserted flag to trust or to contradict.
    mode: str
    checks: list[ConceptCheck] = Field(max_length=60)
    copyChecks: list[CopyCheck] = Field(max_length=60)
    documentedExceptions: list[str] = Field(max_length=40)
    issues: list[ConceptIssue] = Field(max_length=60)
    suggestions: list[str] = Field(max_length=40)
    constraintConflict: ConstraintConflict


def parse_reference_analysis(payload):
    try:
        return ReferenceAnalysis.model_validate(payload)
    except ValidationError as exc:
        from lib.errors import InvalidModelJson

        raise InvalidModelJson(_first_error(exc)) from exc


def parse_concept_review(payload):
    try:
        return ConceptReviewVerdict.model_validate(payload)
    except ValidationError as exc:
        from lib.errors import InvalidModelJson

        raise InvalidModelJson(_first_error(exc)) from exc


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    loc = ".".join(str(p) for p in err.get("loc", ()))
    msg = err.get("msg", "invalid value")
    return f"{loc}: {msg}" if loc else msg
