"""Fetch caller-supplied reference and asset images.

This is the one place the service retrieves a URL the caller chose, so it is a
security boundary as much as a utility:

* only https, only authorized origins (SPEC section 6),
* no redirects followed, since a redirect would escape the origin allowlist,
* a hard byte cap enforced while streaming, not after, so an attacker cannot
  exhaust Lambda memory with a declared-small, actually-huge response, and
* the content type is confirmed by decoding the bytes, not by trusting the
  server's Content-Type header.
"""

from __future__ import annotations

import io

import conf
from lib import schemas
from lib.errors import AssetInvalid, ReferenceFetchFailed

#: Formats Pillow can decode that the image provider also accepts.
_ALLOWED_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}

_FETCH_TIMEOUT_SECONDS = 20


def fetch(url, *, max_bytes=None):
    """Download and verify one image. Returns `{bytes, mimeType, width, height}`."""
    import requests

    try:
        schemas.check_authorized_origin(url)
    except ValueError as exc:
        raise AssetInvalid(str(exc)) from exc

    limit = max_bytes or conf.MAX_REFERENCE_BYTES
    try:
        response = requests.get(
            url,
            timeout=_FETCH_TIMEOUT_SECONDS,
            stream=True,
            allow_redirects=False,  # A redirect would bypass the origin allowlist.
        )
    except requests.RequestException as exc:
        raise ReferenceFetchFailed(f"Could not fetch the reference image: {url}") from exc

    if response.status_code in (301, 302, 303, 307, 308):
        raise AssetInvalid(
            "The reference URL redirects; supply a direct URL on an authorized origin."
        )
    if response.status_code != 200:
        raise ReferenceFetchFailed(
            f"The reference image returned HTTP {response.status_code}."
        )

    chunks = []
    total = 0
    for chunk in response.iter_content(64 * 1024):
        total += len(chunk)
        if total > limit:
            response.close()
            raise AssetInvalid(
                f"The reference image exceeds the {limit} byte limit."
            )
        chunks.append(chunk)
    data = b"".join(chunks)

    if not data:
        raise ReferenceFetchFailed("The reference image was empty.")
    return decode(data)


def decode(data):
    """Verify bytes really are a supported image, and report its true format."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format
            width, height = img.size
            # Pillow is lazy; force a decode so a truncated or malformed file
            # fails here rather than inside a stage three steps later.
            img.load()
    except Exception as exc:
        raise AssetInvalid("The supplied file is not a decodable image.") from exc

    if fmt not in _ALLOWED_FORMATS:
        raise AssetInvalid(
            f"Image format {fmt} is not supported; use PNG, JPEG or WebP."
        )
    return {
        "bytes": data,
        "mimeType": _ALLOWED_FORMATS[fmt],
        "format": fmt,
        "width": width,
        "height": height,
    }
