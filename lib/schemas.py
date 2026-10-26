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

from pydantic import (
    BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator,
)

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
    # How the design is produced. "legacy" generates a flattened concept and
    # decomposes it; "layout-first" plans an editable layout and generates only the
    # text-free photograph. Absent -> "legacy". "layout-first" is refused unless the
    # deployment enables it, and does not support suppliedAssets.
    designFlow: str = "legacy"
    # Direction, decided before the concept is generated rather than patched
    # afterwards. "auto" resolves from the brief and the copy's script
    # (lib/direction_policy.py); the resolved values and the reason are recorded
    # in the policy document. `layoutDirection` governs where the columns sit,
    # `textDirection` how the writing runs, and they are separate because a
    # Hebrew design still has left-to-right phone numbers in it.
    layoutDirection: Literal["auto", "rtl", "ltr"] = "auto"
    textDirection: Literal["auto", "rtl", "ltr"] = "auto"
    # Whether the reference's own composition direction is kept, or adapted to
    # the locale of the copy. Adapting is the default: a Hebrew brief gets a
    # Hebrew composition even when the reference is an English one.
    referenceLayoutPolicy: Literal["adapt-to-locale", "preserve"] = "adapt-to-locale"
    # Rebuild the editable design from the approved concept of an earlier job of
    # the same owner instead of generating a new one. For re-running reconstruction
    # after a pipeline fix: the concept the customer approved is not replaced, so
    # nothing about the composition can change. The brief should be the earlier
    # job's, so the copy matches the concept.
    reuseConceptFromJobId: Optional[str] = None

    @field_validator("reuseConceptFromJobId")
    @classmethod
    def _reuse_source(cls, v):
        if v is not None and not re.fullmatch(r"[0-9a-f]{32}", v):
            raise ValueError("reuseConceptFromJobId must be a 32-character job id")
        return v

    @field_validator("designFlow", mode="before")
    @classmethod
    def _design_flow(cls, v):
        if v not in ("legacy", "layout-first"):
            raise ValueError("designFlow must be 'legacy' or 'layout-first'")
        if v == "layout-first" and not (conf.LAYOUT_FIRST_ENABLED and conf.CODE_SHAPES_ENABLED):
            raise ValueError("designFlow 'layout-first' is not enabled in this environment")
        return v

    @model_validator(mode="after")
    def _flow_assets(self):
        if self.designFlow == "layout-first" and self.suppliedAssets:
            raise ValueError("designFlow 'layout-first' does not support suppliedAssets yet")
        if self.designFlow == "layout-first" and self.reuseConceptFromJobId:
            raise ValueError("a layout-first design has no concept to reuse")
        return self

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
    if body.get("designFlow") == "legacy":
        body.pop("designFlow")
    # Same reasoning for the direction fields: "auto" and "adapt-to-locale" are
    # what every request accepted before they existed meant, so a replay of one
    # must still hash to the same value.
    for field, default in (
        ("layoutDirection", "auto"),
        ("textDirection", "auto"),
        ("referenceLayoutPolicy", "adapt-to-locale"),
    ):
        if body.get(field) == default:
            body.pop(field)
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
    # Typography beyond the basics. Every field has a default that reproduces the
    # pre-existing behaviour exactly, so a scene written before they existed still
    # validates and still assembles to identical bytes.
    #: Extra space between letters, in px (negative tightens).
    letterSpacing: float = 0.0
    #: "transparent" leaves only the outline (hollow letters); `color` is then
    #: the layer's nominal colour, not what is painted inside the glyphs.
    fill: Literal["solid", "transparent"] = "solid"
    strokeColor: Optional[str] = None
    #: Outline thickness in px. 0 means no outline.
    strokeWidth: float = 0.0
    #: "nowrap" keeps the copy on one line.
    wrap: Literal["wrap", "nowrap"] = "wrap"
    #: "shrink": the renderer may reduce the size until the copy fits its box.
    #: "visible": the size is kept and overflow is reported, never hidden.
    overflow: Literal["shrink", "visible"] = "shrink"

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

    @field_validator("letterSpacing")
    @classmethod
    def _letter_spacing(cls, v):
        if not -10.0 <= v <= 40.0:
            raise ValueError("letterSpacing is in px and must be -10..40")
        return v

    @field_validator("strokeWidth")
    @classmethod
    def _stroke_width(cls, v):
        if not 0.0 <= v <= 24.0:
            raise ValueError("strokeWidth is in px and must be 0..24")
        return v

    @field_validator("strokeColor")
    @classmethod
    def _stroke_color(cls, v):
        if v is not None and not HEX_COLOR.match(v):
            raise ValueError(f"invalid stroke colour {v!r}")
        return v

    @model_validator(mode="after")
    def _outline_is_complete(self):
        # An outline needs both a colour and a width, and hollow letters need an
        # outline: transparent fill with no stroke would render nothing at all.
        if self.strokeWidth > 0 and not self.strokeColor:
            raise ValueError(f"layer {self.id!r} has a strokeWidth but no strokeColor")
        if self.strokeColor and self.strokeWidth <= 0:
            raise ValueError(f"layer {self.id!r} has a strokeColor but no strokeWidth")
        if self.fill == "transparent" and self.strokeWidth <= 0:
            raise ValueError(
                f"layer {self.id!r} has a transparent fill and no outline, so it "
                "would not be visible"
            )
        return self

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


SHAPE_KINDS = ("panel", "gradient", "circle", "dot", "ring", "border", "chevron")
#: Effects that may exist once per design. A readability gradient baked into the
#: photograph AND drawn in code is the double-gradient defect this prevents.
SINGLETON_EFFECTS = ("readabilityGradient",)
EFFECT_KINDS = (
    "readabilityGradient", "panel", "ctaBackground", "badge", "dots", "chevrons",
    "border", "icon", "other",
)
EFFECT_OWNERS = ("layout", "photo", "overlay")


class GradientStop(Strict):
    color: str
    #: 0 is fully transparent, 1 fully opaque.
    opacity: float = 1.0
    #: Position along the gradient line, 0..1.
    position: float

    @field_validator("color")
    @classmethod
    def _color(cls, v):
        if not HEX_COLOR.match(v):
            raise ValueError(f"invalid colour {v!r}")
        return v

    @field_validator("opacity", "position")
    @classmethod
    def _unit(cls, v):
        if not 0.0 <= v <= 1.0:
            raise ValueError("value must be between 0 and 1")
        return v


class ShapeLayer(Strict):
    """A simple visual element owned by the layout, drawn by code.

    Panels, gradients, circles, rings, borders and chevrons are editable HTML/CSS
    rather than pixels in a generated image. Each one names the effect it
    implements (`effect`), and that effect is owned by exactly one thing - this
    layer - which is what stops the same gradient existing in the photograph and
    again here.
    """

    id: str
    type: Literal["shape"] = "shape"
    kind: Literal["panel", "gradient", "circle", "dot", "ring", "border", "chevron"]
    effect: str
    x: int
    y: int
    width: int
    height: int
    zIndex: int
    #: Solid fill for panel, circle and dot.
    color: Optional[str] = None
    opacity: float = 1.0
    radius: int = 0
    #: Gradient direction in CSS degrees: 180 runs top to bottom, 90 left to right.
    angle: int = 180
    stops: list[GradientStop] = Field(default_factory=list, max_length=4)
    borderColor: Optional[str] = None
    borderWidth: float = 0.0
    #: Chevrons point by rotation: 45 down, 135 left, 225 up, 315 right.
    rotation: int = 0

    @field_validator("color", "borderColor")
    @classmethod
    def _color(cls, v):
        if v is not None and not HEX_COLOR.match(v):
            raise ValueError(f"invalid colour {v!r}")
        return v

    @field_validator("opacity")
    @classmethod
    def _opacity(cls, v):
        if not 0.0 <= v <= 1.0:
            raise ValueError("opacity must be between 0 and 1")
        return v

    @field_validator("zIndex")
    @classmethod
    def _z(cls, v):
        if not 0 <= v <= 100:
            raise ValueError("zIndex must be 0..100")
        return v

    @field_validator("radius")
    @classmethod
    def _radius(cls, v):
        if not 0 <= v <= 2048:
            raise ValueError("radius must be 0..2048 px")
        return v

    @field_validator("angle")
    @classmethod
    def _angle(cls, v):
        if not 0 <= v <= 360:
            raise ValueError("angle must be 0..360 degrees")
        return v

    @field_validator("borderWidth")
    @classmethod
    def _border_width(cls, v):
        if not 0.0 <= v <= 48.0:
            raise ValueError("borderWidth must be 0..48 px")
        return v

    @field_validator("rotation")
    @classmethod
    def _rotation(cls, v):
        if v not in (0, 45, 90, 135, 180, 225, 270, 315):
            raise ValueError("rotation must be a multiple of 45 degrees")
        return v

    @model_validator(mode="after")
    def _complete_for_kind(self):
        if self.kind == "gradient":
            if len(self.stops) < 2:
                raise ValueError(f"gradient {self.id!r} needs at least two stops")
            positions = [stop.position for stop in self.stops]
            if positions != sorted(positions):
                raise ValueError(f"gradient {self.id!r} stops must be in order")
        elif self.kind in ("panel", "circle", "dot"):
            if not self.color:
                raise ValueError(f"{self.kind} {self.id!r} needs a color")
        else:
            if not self.borderColor or self.borderWidth <= 0:
                raise ValueError(
                    f"{self.kind} {self.id!r} needs a borderColor and borderWidth"
                )
        return self


class EffectOwner(Strict):
    """Who is responsible for one visual effect in the finished design.

    Exactly one owner: the layout (a shape layer draws it), the photograph or the
    overlay (it is baked into that asset's pixels). Declaring the owner is what
    lets a repair know whether a missing gradient is a code problem it can fix in
    place, or something baked into an image it must not paint over a second time.
    """

    id: str
    kind: Literal[
        "readabilityGradient", "panel", "ctaBackground", "badge", "dots",
        "chevrons", "border", "icon", "other",
    ]
    owner: Literal["layout", "photo", "overlay"]
    note: Optional[str] = None


#: How each element of the approved concept is delivered. The same plan drives
#: extraction (what to decompose), assembly (what is drawn in code) and
#: validation (what must not still be baked into an image).
COMPONENT_KINDS = (
    "photo",            # the photograph itself
    "editableText",     # a text layer
    "editableShape",    # a panel, circle or gradient drawn in CSS
    "rasterIcon",       # an independent transparent image, e.g. a phone pictogram
    "retainedRaster",   # artwork too complex to decompose; stays in the overlay
)


class Component(Strict):
    """One element of the approved concept, and how it is delivered.

    This is the decomposition plan: every component of the concept is classified
    once, and nothing is left unclassified. `pairsWith` records that a background
    circle and a pictogram belong to a particular contact line, or that a panel
    is the background of a particular call to action - which is what lets
    validation check that a contact row really arrived as three independent
    layers rather than one baked picture.
    """

    id: str
    kind: Literal[
        "photo", "editableText", "editableShape", "rasterIcon", "retainedRaster",
    ]
    #: The scene layer that delivers it.
    layerId: str
    #: The text layer this belongs to, when it is part of a pair or a row.
    pairsWith: Optional[str] = None
    note: Optional[str] = None


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


class AlignmentGroup(Strict):
    """Text layers that share one edge (or centre line) in the concept.

    The model sizes each text box on its own, so a column the concept set flush on
    the right comes back with right edges a few pixels apart and drifts when the
    boxes are resized. The group is the relationship, measured from the concept's
    ink: members keep their different widths and are placed so the edge is shared.
    Layers that sit off the line on purpose, and centred layers, are not members.
    """

    id: str
    #: Which line is shared: the members' left edge, right edge or centre.
    edge: Literal["left", "right", "center"]
    layerIds: list[str] = Field(min_length=2, max_length=20)
    #: The shared x coordinate in px, measured from the concept.
    anchor: int
    #: Ink edge measured per member in the concept, px (evidence, not instruction).
    inkEdges: dict[str, int] = Field(default_factory=dict)
    #: Vertical gaps between consecutive members' ink in the concept, top to bottom.
    gaps: list[int] = Field(default_factory=list, max_length=20)
    note: Optional[str] = None


class FillGroup(Strict):
    """Shapes that share one fill in the concept, without forcing it.

    A brand accent used on the call to action, the contact circles and the dots is
    one colour to the customer and one editing handle to the editor. Members keep
    their own `color`: the group records the measured reference and how far each
    member sits from it, so a restyle can move them together and a review can tell
    a deliberate second colour from a mis-sampled one.
    """

    id: str
    layerIds: list[str] = Field(min_length=2, max_length=40)
    color: str
    note: Optional[str] = None

    @field_validator("color")
    @classmethod
    def _color(cls, v):
        if not HEX_COLOR.match(v):
            raise ValueError(f"invalid colour {v!r}")
        return v


class Scene(Strict):
    canvas: Canvas
    textLayers: list[TextLayer] = Field(default_factory=list, max_length=60)
    imageLayers: list[ImageLayer] = Field(default_factory=list, max_length=20)
    overlaySamples: list[SampleRegion] = Field(default_factory=list, max_length=20)
    # Code-owned simple elements and who owns each visual effect. Both default to
    # empty, which is exactly what every scene written before they existed means:
    # nothing is drawn in code and no effect has a declared owner.
    shapeLayers: list[ShapeLayer] = Field(default_factory=list, max_length=40)
    effects: list[EffectOwner] = Field(default_factory=list, max_length=20)
    #: The decomposition plan. Empty means the scene was never decomposed (every
    #: scene written before this existed), and the plan checks are skipped; a plan
    #: that exists must be complete.
    components: list[Component] = Field(default_factory=list, max_length=40)
    #: Relationships between layers, measured from the concept. Empty on every scene
    #: written before they existed, which means "no relationship was recorded".
    alignmentGroups: list[AlignmentGroup] = Field(default_factory=list, max_length=20)
    fillGroups: list[FillGroup] = Field(default_factory=list, max_length=10)


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
    for layer in list(scene.textLayers) + list(scene.imageLayers) + list(scene.shapeLayers):
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
    check_effect_ownership(scene)
    check_component_plan(scene)
    check_groups(scene)
    return scene


def check_groups(scene):
    """A relationship may only name layers that exist, once."""
    text = {layer.id for layer in scene.textLayers}
    shapes = {layer.id for layer in scene.shapeLayers}
    claimed = {}
    for group in scene.alignmentGroups:
        for layer_id in group.layerIds:
            if layer_id not in text:
                raise InvalidScene(
                    f"Alignment group {group.id!r} names {layer_id!r}, which is not a "
                    "text layer."
                )
            if layer_id in claimed:
                raise InvalidScene(
                    f"Text layer {layer_id!r} is in alignment groups {claimed[layer_id]!r} "
                    f"and {group.id!r}; a layer has one shared edge."
                )
            claimed[layer_id] = group.id
    for group in scene.fillGroups:
        missing = [i for i in group.layerIds if i not in shapes]
        if missing:
            raise InvalidScene(
                f"Fill group {group.id!r} names {missing[0]!r}, which is not a shape layer."
            )


def check_effect_ownership(scene):
    """Every visual effect has exactly one owner, and nothing is drawn twice.

    The failure this prevents is two owners for one effect: a dark readability
    gradient painted into the photograph and again by a shape layer, or an effect
    declared as layout-owned with nothing in the layout that draws it (it then
    exists nowhere, and the design silently loses it).
    """
    effects = {}
    for effect in scene.effects:
        if effect.id in effects:
            raise InvalidScene(f"Effect {effect.id!r} is declared more than once.")
        effects[effect.id] = effect

    drawn = {}
    for shape in scene.shapeLayers:
        owner = effects.get(shape.effect)
        if owner is None:
            raise InvalidScene(
                f"Shape {shape.id!r} implements effect {shape.effect!r}, which has "
                "no declared owner."
            )
        if owner.owner != "layout":
            raise InvalidScene(
                f"Shape {shape.id!r} draws effect {shape.effect!r}, but that effect "
                f"is owned by the {owner.owner}; drawing it here as well would "
                "duplicate it."
            )
        drawn.setdefault(shape.effect, []).append(shape.id)

    for effect in scene.effects:
        if effect.owner == "layout" and effect.id not in drawn:
            raise InvalidScene(
                f"Effect {effect.id!r} is owned by the layout but no shape draws it."
            )

    for kind in SINGLETON_EFFECTS:
        owners = [e for e in scene.effects if e.kind == kind]
        if len(owners) > 1:
            raise InvalidScene(
                f"The design declares {len(owners)} {kind} effects "
                f"({', '.join(e.id for e in owners)}); there may be only one."
            )


#: Text roles whose element is a contact line. Each one is expected to arrive as
#: three independent layers: a circle, a pictogram and the value itself.
CONTACT_KEY_PREFIX = "contact."


def check_component_plan(scene):
    """A decomposition plan that exists must be complete and consistent.

    An empty plan means the scene predates the plan (or comes from a flow that
    does not decompose), and nothing here applies. A plan that exists is the
    contract for what the customer can edit, so an element it classifies must be
    delivered the way it says, and an element it omits is a component someone
    forgot to decompose - which in practice means it is still baked into a
    picture, where no one can move it.
    """
    if not scene.components:
        return

    layers = {layer.id: layer for layer in scene.textLayers}
    layers.update({layer.id: layer for layer in scene.imageLayers})
    layers.update({layer.id: layer for layer in scene.shapeLayers})
    expected = {
        "photo": ("background",),
        "editableText": ("text",),
        "editableShape": ("shape",),
        "rasterIcon": ("image",),
        "retainedRaster": ("decoration", "image"),
    }

    classified = {}
    for component in scene.components:
        layer = layers.get(component.layerId)
        if layer is None:
            raise InvalidScene(
                f"Component {component.id!r} names layer {component.layerId!r}, "
                "which the scene does not contain."
            )
        if layer.type not in expected[component.kind]:
            raise InvalidScene(
                f"Component {component.id!r} is classified {component.kind!r} but "
                f"layer {component.layerId!r} is a {layer.type} layer."
            )
        if component.pairsWith and component.pairsWith not in layers:
            raise InvalidScene(
                f"Component {component.id!r} pairs with {component.pairsWith!r}, "
                "which the scene does not contain."
            )
        classified.setdefault(component.layerId, []).append(component)

    # Text is unambiguous - it is delivered as text or it is not there at all - so
    # the completeness check is on the elements that could silently stay pixels.
    for layer in list(scene.shapeLayers) + [
        l for l in scene.imageLayers if l.type != "background"
    ]:
        if layer.id not in classified:
            raise InvalidScene(
                f"Layer {layer.id!r} has no entry in the decomposition plan; every "
                "shape and non-photograph image must be classified."
            )

    paired = {}
    for component in scene.components:
        if component.pairsWith:
            paired.setdefault(component.pairsWith, set()).add(component.kind)

    for text in scene.textLayers:
        kinds = paired.get(text.id, set())
        if "retainedRaster" in kinds:
            continue                        # deliberately left as artwork, declared
        if text.role == "cta" and not _has_background(scene, text, kinds):
            raise InvalidScene(
                f"Call to action {text.id!r} has neither an editable background "
                "shape nor a declared retained raster; its button would still be "
                "part of a picture."
            )
        if text.contentKey.startswith(CONTACT_KEY_PREFIX):
            missing = {"editableShape", "rasterIcon"} - kinds
            if missing:
                raise InvalidScene(
                    f"Contact line {text.id!r} is missing {', '.join(sorted(missing))} "
                    "in the decomposition plan; a contact row is a circle, a "
                    "pictogram and the value, each editable on its own."
                )


def _has_background(scene, text, paired_kinds):
    """A CTA background: declared by pairing, or a shape drawn under the words."""
    if "editableShape" in paired_kinds:
        return True
    return any(
        shape.zIndex <= text.zIndex
        and shape.x <= text.x
        and shape.y <= text.y
        and shape.x + shape.width >= text.x + text.width
        and shape.y + shape.height >= text.y + text.height
        for shape in scene.shapeLayers
    )


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


class Constraint(Strict):
    """One property-level correction for one element."""

    elementId: str
    property: str
    value: str


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
    # Repair routing. Optional so a review written before they existed still
    # parses - it then reads as unrouted, which is never a reason to regenerate.
    repairTarget: Optional[Literal["content", "typography", "layout", "asset", "concept"]] = None
    affectedElementIds: list[str] = Field(default_factory=list, max_length=20)
    requiresImageGeneration: Optional[bool] = None
    constraints: list[Constraint] = Field(default_factory=list, max_length=30)


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
    #: Whether the concept is composed in the direction the policy resolved.
    #: Defaults to None so a verdict stored before the check existed can still be
    #: re-decided; the backend reads a missing answer as a check that did not run.
    compositionDirectionOk: Optional[bool] = None


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
