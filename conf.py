"""Configuration for the LayeredDesign service.

Every value is read from the environment so one container image runs in any stage.
Credentials never live in source; serverless.yml injects them from the gitignored
config.<stage>.json, matching the convention in the sibling repos.

The model IDs below are pinned and verified by scripts/probe_image.py and
scripts/probe_vision.py. Read .cursor/rules/layered-design.mdc before changing them.
"""

import os

# --- Models ------------------------------------------------------------------
# SPEC section 6 requires the image, concept-analysis and review models to remain
# SEPARATELY configurable. Do not collapse these into a single constant.
#
# gpt-image-2 is deliberately NOT used: it rejects background="transparent", which
# DECORATION_OVERLAY depends on entirely. gpt-4o is deliberately NOT used: it is
# deprecated with a 2027-01-14 shutdown.
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-2.5-sunburst")
SCENE_MODEL = os.environ.get("SCENE_MODEL", "gpt-6-astra")
REVIEW_MODEL = os.environ.get("REVIEW_MODEL", "gpt-6-astra")
COPY_MODEL = os.environ.get("COPY_MODEL", "gpt-5.6-terra")

# --- AWS resources -----------------------------------------------------------
JOBS_TABLE = os.environ.get("JOBS_TABLE", "")
ARTIFACT_BUCKET = os.environ.get("ARTIFACT_BUCKET", "")
WORKFLOW_ARN = os.environ.get("WORKFLOW_ARN", "")
STAGE_FUNCTION_NAME = os.environ.get("STAGE_FUNCTION_NAME", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# --- Bedrock agent integration (SPEC section 7) ------------------------------
# The session attribute carrying job ownership for agent calls. The application
# backend sets it in InvokeAgent; it is never an Action Group parameter. The
# agent ARN is deliberately absent: only CloudFormation references it.
BEDROCK_OWNER_SCOPE_ATTRIBUTE = os.environ.get(
    "BEDROCK_OWNER_SCOPE_ATTRIBUTE", "ownerScope"
)

# --- Providers ---------------------------------------------------------------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# Cloudinary credentials come from Secrets Manager in a deployed environment, so
# they never appear in Lambda environment variables, CloudFormation state or a
# local file, and a rotated key takes effect on the next cold start rather than
# needing a redeploy. The direct variables below remain as an override for local
# runs and tests, where there is no AWS role to assume.
CLOUDINARY_SECRET_ARN = os.environ.get("CLOUDINARY_SECRET_ARN", "")
CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY", "")
CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "")
CLOUDINARY_FOLDER = os.environ.get("CLOUDINARY_FOLDER", "layered-design")

# Reference images are fetched only from these HTTPS origins (SPEC section 6).
AUTHORIZED_IMAGE_ORIGINS = tuple(
    o.strip()
    for o in os.environ.get(
        "AUTHORIZED_IMAGE_ORIGINS", "res.cloudinary.com"
    ).split(",")
    if o.strip()
)

# --- Canvas ------------------------------------------------------------------
MIN_CANVAS = 256
MAX_CANVAS = 2048
DEFAULT_CANVAS_WIDTH = 1080
DEFAULT_CANVAS_HEIGHT = 1080

# GPT Image renders only these dimensions. A requested canvas is generated at the
# closest supported aspect and resampled to target in exactly one place
# (lib/validate_assets.py). See docs/PROBE_RESULTS.md.
SUPPORTED_GENERATION_SIZES = ("1024x1024", "1024x1536", "1536x1024")

# --- Request limits (SPEC section 2.1) ---------------------------------------
MAX_POST_CHARS = 12000
MAX_REQUEST_BYTES = 64 * 1024
MAX_SUPPLIED_ASSETS = 10
MAX_REFERENCE_BYTES = 16 * 1024 * 1024

# --- Pipeline bounds ---------------------------------------------------------
MAX_INDEPENDENT_ASSETS = 3
INDEPENDENT_ASSET_CONCURRENCY = 2
MAX_CORRECTION_ROUNDS = 2
MAX_OVERLAY_REPAIRS = 2
WORKFLOW_MAX_MINUTES = 30

# Provider deadlines stay below the stage Lambda's 240s budget so a timeout
# surfaces as our own error rather than a Lambda kill.
IMAGE_PROVIDER_TIMEOUT_SECONDS = 180
VISION_PROVIDER_TIMEOUT_SECONDS = 120

# --- Rendering ---------------------------------------------------------------
FEED_RENDER_WIDTH = 360
APPROVED_FONT_FAMILIES = tuple(
    f.strip()
    for f in os.environ.get("APPROVED_FONT_FAMILIES", "Heebo,Arial").split(",")
    if f.strip()
)


class ConfigurationError(RuntimeError):
    """Raised at startup when required configuration is absent."""


#: Names required by each entry point. Checked at startup and again in PREFLIGHT,
#: so a misconfigured deployment fails immediately instead of mid-pipeline.
API_REQUIRED = ("JOBS_TABLE", "ARTIFACT_BUCKET", "WORKFLOW_ARN")
STAGE_REQUIRED = ("JOBS_TABLE", "ARTIFACT_BUCKET", "OPENAI_API_KEY")
#: Only used when CLOUDINARY_SECRET_ARN is absent; see validate_media below.
MEDIA_REQUIRED = (
    "CLOUDINARY_CLOUD_NAME",
    "CLOUDINARY_API_KEY",
    "CLOUDINARY_API_SECRET",
)


def validate(required_names):
    """Raise ConfigurationError naming every missing setting at once.

    Reporting all of them together matters: a deploy with three unset variables
    should not need three failed invocations to diagnose.
    """
    missing = [name for name in required_names if not globals().get(name)]
    if missing:
        raise ConfigurationError(
            "Missing required configuration: " + ", ".join(sorted(missing))
        )


def validate_media():
    """Media credentials may arrive either way, so check for one or the other.

    A deployment supplies CLOUDINARY_SECRET_ARN; a local run supplies the three
    values directly. Requiring both would break one of the two environments.
    """
    if CLOUDINARY_SECRET_ARN:
        return
    validate(MEDIA_REQUIRED)
