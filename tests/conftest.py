"""Test configuration.

Environment is set before any module import so conf picks it up, matching the
pattern used in the sibling repos.
"""

import os

os.environ.setdefault("JOBS_TABLE", "test-jobs")
os.environ.setdefault("ARTIFACT_BUCKET", "test-artifacts")
os.environ.setdefault("WORKFLOW_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:test")
os.environ.setdefault("STAGE_FUNCTION_NAME", "test-stage")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
# PREFLIGHT checks stable media publishing is configured, so these must be
# present even though every test fakes the upload itself.
os.environ.setdefault("CLOUDINARY_CLOUD_NAME", "test-cloud")
os.environ.setdefault("CLOUDINARY_API_KEY", "test-cloudinary-key")
os.environ.setdefault("CLOUDINARY_API_SECRET", "test-cloudinary-secret")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

import pytest  # noqa: E402

CLOUDINARY = "https://res.cloudinary.com/broadcust/image/upload"


@pytest.fixture
def start_payload():
    """The acceptance brief from SPEC section 9: an Italy winter travel offer.

    Hebrew RTL body with Latin contact details, real business facts and
    deliberately no invented price or date.
    """
    return {
        "schemaVersion": "1.0",
        "requestId": "req-italy-001",
        "brief": {
            "post": (
                "חופשת חורף באיטליה - חג המולד וחנוכה בטוסקנה, אומבריה ודרום איטליה. "
                "טיסות, רכב, מלונות, מסלול מותאם אישית ושירות אישי לאורך כל הדרך."
            ),
            "business": {
                "name": "Broadcust Travel",
                "phone": "0501234567",
                "email": "travel@broadcust.co.il",
                "website": "https://broadcust.co.il",
            },
            "instructions": "Warm, premium, winter light.",
            "requiredTexts": ["Broadcust Travel"],
        },
        "canvas": {"width": 1080, "height": 1080},
        "referenceImageUrl": f"{CLOUDINARY}/inspiration.png",
        "brand": {"colors": ["#071B33", "#D4AF37"], "fontFamilies": ["Heebo"]},
        "suppliedAssets": [
            {
                "id": "business-logo",
                "kind": "logo",
                "url": f"{CLOUDINARY}/business-logo.png",
            }
        ],
    }


@pytest.fixture
def scene_payload():
    """A minimal valid scene for a 1080x1080 canvas."""
    return {
        "canvas": {"width": 1080, "height": 1080},
        "textLayers": [
            {
                "id": "text-1001",
                "type": "text",
                "role": "title",
                "contentKey": "title.primary",
                "text": "חורף באיטליה",
                "x": 540,
                "y": 80,
                "width": 480,
                "height": 180,
                "zIndex": 10,
                "fontFamily": "Heebo",
                "fontSize": 72,
                "fontWeight": 800,
                "color": "#FFFFFF",
                "align": "right",
                "direction": "rtl",
                "lineHeight": 1.12,
            }
        ],
        "imageLayers": [
            {
                "id": "background-1",
                "type": "background",
                "x": 0,
                "y": 0,
                "width": 1080,
                "height": 1080,
                "zIndex": 0,
                "objectFit": "cover",
            },
            {
                "id": "decorations-1",
                "type": "decoration",
                "x": 0,
                "y": 0,
                "width": 1080,
                "height": 1080,
                "zIndex": 1,
                "objectFit": "contain",
            },
        ],
        "overlaySamples": [
            {"kind": "transparent", "x": 100, "y": 600, "width": 200, "height": 200},
            {"kind": "opaque", "x": 700, "y": 100, "width": 120, "height": 120},
        ],
    }
