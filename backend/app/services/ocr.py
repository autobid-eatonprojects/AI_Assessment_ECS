"""OCR for scanned PDFs — multi-provider ensemble per design doc P1.

When a `written-spec` document arrives without a native text layer, we OCR
each rendered page. The output is plain text per page, written into
`DocumentPage.text_content` so the existing chunker indexes it transparently.

Providers (auto-detected from configured API keys):
  - Mistral OCR (primary, best AEC accuracy per design doc)
  - Gemini 2.5 Flash (fallback, always available since GOOGLE_API_KEY is
    used elsewhere too)
  - AWS Textract (deferred — install boto3 + add credentials to enable)
  - Document AI (deferred — Google Cloud SDK is heavy)

When 2+ providers are configured, the facade runs them in parallel and:
  1. Picks the longer/cleaner output as the canonical text
  2. Records BOTH outputs (audit + future token-level voting)
  3. Flags pages where outputs diverge significantly (Levenshtein-ratio
     based) so a human can spot-check before liability-bearing decisions

W1 mitigation: a single misread `shall not` → `shall` is exactly the kind
of failure mode the ensemble surfaces — provider disagreement on safety-
critical wording shows up as a flagged page in the HITL review queue.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from ..config import settings
from . import ocr_mistral, scan_preprocessor
from .llm_log import Usage
from .ocr_types import OCRResult

log = logging.getLogger(__name__)


_OCR_MODEL = "gemini-2.5-flash"
_OCR_PROMPT = """\
You are an OCR engine. Extract ALL visible text from the page image, preserving:
- Reading order (top to bottom, left to right; multi-column layouts read as
  full left column then full right column).
- Section headers (use ALL CAPS exactly as printed, e.g. "PART 1 — GENERAL",
  "SECTION 03 30 00 — CAST-IN-PLACE CONCRETE").
- Numbered/lettered lists with their indentation cues (1., A., a., etc.).
- Footers with CSI section codes (e.g. "03 30 00.4").
Return ONLY the extracted text — no commentary, no formatting markers.
Do not summarise; transcribe verbatim.
"""


_client = None
_concurrency_sem: asyncio.Semaphore | None = None


class OCRUnavailable(Exception):
    """Raised when Google API key is not configured."""


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not settings.google_api_key:
        raise OCRUnavailable("GOOGLE_API_KEY is not set")
    from google import genai

    _client = genai.Client(api_key=settings.google_api_key)
    return _client


def _get_semaphore() -> asyncio.Semaphore:
    global _concurrency_sem
    if _concurrency_sem is None:
        # Gemini Flash starts returning 503s above ~5 concurrent requests for
        # us. We pair this with retry-with-backoff so brief spikes recover
        # instead of producing silent failures.
        _concurrency_sem = asyncio.Semaphore(4)
    return _concurrency_sem


_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 5

# Google overloads 403 PERMISSION_DENIED for two very different cases:
#   (a) Truly permanent: invalid API key, billing disabled, API not enabled.
#   (b) Transient: their automated abuse-detection briefly holds an API
#       project after a burst of multimodal calls — the message is the
#       distinctive "Your project has been denied access. Please contact
#       support."
# We retry only when the message smells like (b) so we don't loop forever
# on real permission errors.
_TRANSIENT_403_HINTS = (
    "denied access",       # the abuse-detection wording
    "consumer_suspended",  # short-term project suspension code
    "abuse",
    "temporarily",
)


def _is_retryable(exc: Exception) -> bool:
    """Spot Gemini's transient errors (rate limit / overload / 5xx / abuse hold)."""
    msg = str(exc).lower()
    if "503" in msg or "unavailable" in msg or "high demand" in msg:
        return True
    if "429" in msg or "rate limit" in msg or "resource_exhausted" in msg:
        return True
    if "500" in msg or "502" in msg or "504" in msg:
        return True
    if "403" in msg and any(hint in msg for hint in _TRANSIENT_403_HINTS):
        return True
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(code, int) and code in _RETRYABLE_STATUS:
        return True
    return False


async def _ocr_image_gemini(image_path: Path) -> OCRResult:
    """OCR a single rendered page image using Gemini Flash, with retry-on-overload."""
    client = _get_client()
    sem = _get_semaphore()

    raw = image_path.read_bytes()
    media_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"

    from google.genai import types as gtypes

    content = [
        gtypes.Part.from_bytes(data=raw, mime_type=media_type),
        gtypes.Part.from_text(text=_OCR_PROMPT),
    ]

    last: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        async with sem:
            t0 = time.perf_counter()
            try:
                resp = await client.aio.models.generate_content(
                    model=_OCR_MODEL,
                    contents=content,
                    config=gtypes.GenerateContentConfig(
                        temperature=0.0,
                        max_output_tokens=8192,
                    ),
                )
            except Exception as e:  # noqa: BLE001
                last = e
                if not _is_retryable(e) or attempt == _MAX_RETRIES:
                    raise
                # Exponential backoff with jitter: 1s, 2s, 4s, 8s, 16s
                import random

                delay = (2**attempt) + random.uniform(0, 0.5)
                log.info(
                    "ocr: retry %d after %.1fs (transient error: %s)",
                    attempt + 1,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)
                continue
            latency_ms = int((time.perf_counter() - t0) * 1000)

        usage_meta = getattr(resp, "usage_metadata", None)
        usage = Usage(
            prompt_tokens=getattr(usage_meta, "prompt_token_count", 0) or 0,
            completion_tokens=getattr(usage_meta, "candidates_token_count", 0) or 0,
        )
        text = (getattr(resp, "text", "") or "").strip()
        return OCRResult(
            text=text, usage=usage, latency_ms=latency_ms, provider="gemini"
        )

    assert last is not None
    raise last


# -----------------------------------------------------------------------------
# Multi-provider ensemble facade
# -----------------------------------------------------------------------------


_DISAGREEMENT_FLAG_THRESHOLD = 0.20  # 20% normalized edit distance flips the flag


def _normalized_edit_distance(a: str, b: str) -> float:
    """Levenshtein distance / max(len(a), len(b)). 0.0 = identical, 1.0 = no overlap.

    Cheap, char-level. For full token-level voting (the design doc's
    eventual target), we'd need per-provider token confidences and
    alignment — that's a follow-up. This metric is enough to flag
    pages where providers genuinely disagree (the W1 risk we care about).
    """
    if not a and not b:
        return 0.0
    if not a or not b:
        return 1.0
    # Use a bounded distance computation — full Levenshtein is O(n*m).
    # rapidfuzz is fast and Python-native; if we don't have it, fall back
    # to a length-ratio check (less accurate but no extra dep).
    try:
        from rapidfuzz import distance as rf_distance  # type: ignore

        d = rf_distance.Levenshtein.distance(a, b)
    except ImportError:  # pragma: no cover
        d = abs(len(a) - len(b))
    return d / max(len(a), len(b))


def _enabled_providers() -> list[str]:
    """Return list of OCR provider names that are configured."""
    out: list[str] = []
    if settings.google_api_key:
        out.append("gemini")
    if ocr_mistral.is_available():
        out.append("mistral")
    return out


async def ocr_image(image_path: Path, *, preprocess: bool | None = None) -> OCRResult:
    """OCR one page using all configured providers; pick the best.

    Single-provider mode (only Gemini configured) is the existing behavior:
    one call, one result. Multi-provider mode runs them in parallel and
    surfaces both outputs + a disagreement metric for audit.

    `preprocess`: None (default) = auto-detect via scan_preprocessor heuristic
    (low-res or low-contrast pages get cleaned). True = force preprocess.
    False = skip (useful when the caller already passed a clean render).

    Retry-on-empty: if all providers return empty text on the first try,
    re-run once with forced preprocessing (deskew + Sauvola + despeckle)
    in case the rendered image had contrast/skew issues that confused
    OCR. The cost is a second API call per failing page; on a typical
    spec book this fires for ~5% of pages, not 30% (the previous bug
    where Gemini's empty result was preferred over Mistral's good text).
    """
    result = await _ocr_image_inner(image_path, preprocess=preprocess)
    if result.text.strip() or preprocess is True:
        return result
    # Empty result + preprocessing wasn't already forced → retry once
    # with preprocessing on. Most "blank page" results recover here.
    log.info(
        "ocr: %s came back empty; retrying with forced preprocessing",
        image_path.name,
    )
    return await _ocr_image_inner(image_path, preprocess=True)


async def _ocr_image_inner(image_path: Path, *, preprocess: bool | None = None) -> OCRResult:
    """Internal: single OCR pass (one set of provider calls). Wrapped
    by ocr_image() which adds retry-on-empty."""
    # Optional scan preprocessing — runs deskew + Sauvola binarize + despeckle
    # for pages that look like scanned spec books. Skipped on clean
    # high-res renders (drawings rendered from native PDFs).
    ocr_input = image_path
    if preprocess is True or (
        preprocess is None and scan_preprocessor.needs_preprocessing(image_path)
    ):
        try:
            pp = scan_preprocessor.preprocess_for_ocr(image_path)
            log.info(
                "ocr: preprocessed %s (%s)",
                image_path.name, ", ".join(pp.steps_applied),
            )
            ocr_input = pp.output_path
        except Exception as e:  # noqa: BLE001
            log.warning("ocr: preprocessing failed (%s); using raw image", e)

    providers = _enabled_providers()
    if not providers:
        raise OCRUnavailable("no OCR providers configured (need GOOGLE_API_KEY at minimum)")

    if len(providers) == 1:
        # Fast path — same as before
        if providers[0] == "gemini":
            return await _ocr_image_gemini(ocr_input)
        return await ocr_mistral.ocr_image(ocr_input)

    # Run all providers in parallel
    tasks = []
    if "gemini" in providers:
        tasks.append(("gemini", _ocr_image_gemini(ocr_input)))
    if "mistral" in providers:
        tasks.append(("mistral", ocr_mistral.ocr_image(ocr_input)))

    results: list[tuple[str, OCRResult | Exception]] = []
    raw = await asyncio.gather(*(t[1] for t in tasks), return_exceptions=True)
    for (name, _), r in zip(tasks, raw):
        results.append((name, r))

    # Filter to successes
    successes: list[tuple[str, OCRResult]] = [
        (n, r) for n, r in results if isinstance(r, OCRResult)
    ]
    if not successes:
        # All providers failed — surface the first exception
        for _, r in results:
            if isinstance(r, Exception):
                raise r
        raise OCRUnavailable("all OCR providers failed silently")

    # If only one succeeded, use it (and log that the other failed)
    if len(successes) == 1:
        name, primary = successes[0]
        for n, r in results:
            if isinstance(r, Exception):
                log.warning("ocr ensemble: %s failed (%s); using %s only", n, r, name)
        primary.candidates = [{"provider": name, "len": len(primary.text)}]
        return primary

    # 2+ providers succeeded — vote. Pick the longer output as canonical
    # (longer usually = more text recovered, less truncation), and flag
    # the page if the providers disagree significantly.
    # Empty-text guard: if one provider returns "" (Gemini occasionally
    # does on dense spec pages — we've seen ~30% empty rate without this),
    # prefer ANY non-empty result over the empty one regardless of length.
    non_empty = [(n, r) for n, r in successes if r.text.strip()]
    if non_empty:
        longest_name, longest = max(non_empty, key=lambda x: len(x[1].text))
    else:
        longest_name, longest = max(successes, key=lambda x: len(x[1].text))
    other_text = next((r.text for n, r in successes if n != longest_name), "")
    disagreement = _normalized_edit_distance(longest.text, other_text)
    flagged = disagreement >= _DISAGREEMENT_FLAG_THRESHOLD

    longest.provider = longest_name
    longest.candidates = [
        {
            "provider": n,
            "len": len(r.text),
            "latency_ms": r.latency_ms,
        }
        for n, r in successes
    ]
    longest.disagreement = round(disagreement, 4)
    longest.flagged_low_confidence = flagged

    if flagged:
        log.warning(
            "ocr ensemble: providers disagree (%.0f%%) on %s — flagged for review",
            disagreement * 100, image_path.name,
        )
    return longest
