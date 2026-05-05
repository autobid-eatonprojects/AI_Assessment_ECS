"""Magic-byte image format detection.

Browsers and `mimetypes` derive content-type from filename extension. A JPEG
renamed `.png` therefore reaches us declared `image/png`, and Anthropic /
Gemini / Pillow all reject it on the magic-number mismatch. Detect the
truth from the first 16 bytes once, override at every consumer that talks
to those APIs.
"""

from __future__ import annotations


def detect_image_mime(data: bytes) -> str | None:
    """Return the real image media type, or None if the bytes don't match."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def resolve_image_mime(data: bytes, fallback: str | None = None) -> str:
    """Sniff the bytes; fall back to the supplied content_type if no signature
    matches. Default fallback is image/jpeg (the most common true format we
    see when extensions lie)."""
    return detect_image_mime(data[:16]) or (fallback or "image/jpeg")
