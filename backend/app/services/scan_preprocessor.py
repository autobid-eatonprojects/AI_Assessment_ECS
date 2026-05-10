"""Scan pre-processing pipeline (P1 hardening per design doc, W1 mitigation).

Applied to scanned spec / drawing pages BEFORE OCR. Recovers pages that
pure-text OCR would miss because of skew, noise, faded ink, or low DPI.

Pipeline (all optional, applied in order if image needs them):
  1. Deskew    — Hough transform on edge map → rotate to align text rows
  2. Despeckle — small-component removal cleans speckle/dust artifacts
  3. Binarize  — Sauvola adaptive threshold (handles varying contrast across
                 a single page better than global Otsu)
  4. Upscale   — optional 2x via Real-ESRGAN if installed (faded text
                 recovery; falls back to Lanczos if Real-ESRGAN missing)

Each step decides whether to apply based on simple heuristics measured
on the input image (e.g. if skew angle < 0.5° we skip the rotate to save
the resampling artifact).

Cost: 0 (CPU only). Latency: 100-500ms per page.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)


@dataclass
class PreprocessResult:
    """Where the cleaned page lives + which steps actually fired."""

    output_path: Path
    steps_applied: list[str]
    skew_angle_deg: float | None = None
    binarized: bool = False
    upscaled: bool = False


# -----------------------------------------------------------------------------
# Step 1 — Deskew
# -----------------------------------------------------------------------------


def _detect_skew_angle(gray: np.ndarray) -> float:
    """Estimate rotation in degrees via Hough lines on Canny edges.

    Returns 0.0 if no confident estimate. Positive angle = counter-clockwise
    rotation needed to deskew.
    """
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    # threshold tuned for full-page scans; lower if text is faint
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 180,
        threshold=200, minLineLength=100, maxLineGap=20,
    )
    if lines is None or len(lines) == 0:
        return 0.0
    angles: list[float] = []
    for line in lines:
        x1, y1, x2, y2 = line[0]
        if x2 == x1:
            continue
        a = math.degrees(math.atan2(y2 - y1, x2 - x1))
        # We want angles near 0 (horizontal text rows). Map ±90° → 0°.
        if -45 <= a <= 45:
            angles.append(a)
    if not angles:
        return 0.0
    return float(np.median(angles))


def _deskew(image: np.ndarray) -> tuple[np.ndarray, float]:
    """Returns (rotated_image, applied_angle_deg)."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    angle = _detect_skew_angle(gray)
    if abs(angle) < 0.3:
        # below detection noise — leave alone, avoid resample artifacts
        return image, 0.0
    h, w = image.shape[:2]
    center = (w / 2, h / 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(
        image, matrix, (w, h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return rotated, angle


# -----------------------------------------------------------------------------
# Step 2 — Despeckle (small-component removal)
# -----------------------------------------------------------------------------


def _despeckle(binary: np.ndarray, min_area: int = 8) -> np.ndarray:
    """Drop connected components smaller than min_area pixels.

    Operates on a binarized image (0/255). Removes salt-and-pepper noise
    + ink dots without touching text strokes (which are larger).
    """
    # cv2.connectedComponentsWithStats expects 8-bit single-channel
    inverted = cv2.bitwise_not(binary)  # black ink → white blobs
    n, labels, stats, _ = cv2.connectedComponentsWithStats(inverted, connectivity=8)
    keep = np.zeros_like(binary)
    keep.fill(255)
    for i in range(1, n):  # skip background (label 0)
        area = stats[i, cv2.CC_STAT_AREA]
        if area >= min_area:
            keep[labels == i] = 0  # restore ink
    return keep


# -----------------------------------------------------------------------------
# Step 3 — Sauvola binarize
# -----------------------------------------------------------------------------


def _sauvola_binarize(gray: np.ndarray, window: int = 25, k: float = 0.2) -> np.ndarray:
    """Sauvola adaptive threshold.

    T(x,y) = m(x,y) * (1 + k * ((s(x,y) / R) - 1))
    where m, s are local mean and stddev, R = max(s) (often 128).

    Better than Otsu for spec books with varying illumination across a
    single page (margins lighter than text body, etc).
    """
    gray = gray.astype(np.float32)
    mean = cv2.boxFilter(gray, cv2.CV_32F, (window, window))
    sqmean = cv2.boxFilter(gray * gray, cv2.CV_32F, (window, window))
    stddev = np.sqrt(np.clip(sqmean - mean * mean, 0, None))
    R = 128.0
    threshold = mean * (1.0 + k * ((stddev / R) - 1.0))
    binary = (gray > threshold).astype(np.uint8) * 255
    return binary


# -----------------------------------------------------------------------------
# Step 4 — Upscale (optional Real-ESRGAN, fallback Lanczos)
# -----------------------------------------------------------------------------


def _try_realesrgan_upscale(image: np.ndarray) -> np.ndarray | None:
    """Attempt Real-ESRGAN 2x upscale. Returns None if not installed."""
    try:
        from realesrgan_ncnn_py import Realesrgan  # type: ignore
    except ImportError:
        return None
    try:
        upscaler = Realesrgan(model=2)  # 2x model for text
        return upscaler.process_cv2(image)
    except Exception as e:  # noqa: BLE001
        log.warning("Real-ESRGAN failed, falling back to Lanczos: %s", e)
        return None


def _upscale(image: np.ndarray, factor: int = 2) -> np.ndarray:
    """Upscale by `factor`. Tries Real-ESRGAN first, falls back to Lanczos."""
    fancy = _try_realesrgan_upscale(image)
    if fancy is not None:
        return fancy
    h, w = image.shape[:2]
    return cv2.resize(image, (w * factor, h * factor), interpolation=cv2.INTER_LANCZOS4)


# -----------------------------------------------------------------------------
# Pipeline driver
# -----------------------------------------------------------------------------


def needs_preprocessing(image_path: Path) -> bool:
    """Quick heuristic: low-resolution OR low-contrast pages benefit most.

    Returns True for typical scanned spec book pages. Drawings rendered
    at 150+ DPI from text-native PDFs will return False (they don't
    benefit from the pipeline + we don't want to slow down processing).
    """
    img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return False
    h, w = img.shape
    if h < 1200 or w < 1200:
        return True  # low res likely scanned
    # Contrast heuristic — stddev < 50 means flat / faded
    stddev = float(img.std())
    return stddev < 60.0


def preprocess_for_ocr(
    image_path: Path,
    *,
    output_path: Path | None = None,
    upscale: bool = False,
) -> PreprocessResult:
    """Apply the full pipeline to a single page image.

    `output_path` defaults to a sibling file `*_preprocessed.png`.
    `upscale=False` by default — skip the 2x Real-ESRGAN/Lanczos pass
    unless the caller knows the page is too low-res for OCR.
    """
    if output_path is None:
        output_path = image_path.with_name(image_path.stem + "_preprocessed.png")

    img = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(image_path)

    steps: list[str] = []

    img, skew = _deskew(img)
    if skew != 0.0:
        steps.append(f"deskew({skew:.2f}°)")

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    binary = _sauvola_binarize(gray)
    steps.append("sauvola")

    binary = _despeckle(binary)
    steps.append("despeckle")

    out = binary
    if upscale:
        out = _upscale(out, factor=2)
        steps.append("upscale_2x")

    cv2.imwrite(str(output_path), out)
    return PreprocessResult(
        output_path=output_path,
        steps_applied=steps,
        skew_angle_deg=skew if skew != 0.0 else None,
        binarized=True,
        upscaled=upscale,
    )
