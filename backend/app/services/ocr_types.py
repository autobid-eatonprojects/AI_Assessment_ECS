"""Shared types for the OCR provider ensemble.

Extracted so `ocr.py` and `ocr_mistral.py` can both produce the same
`OCRResult` class. Previously each module declared its own dataclass —
which made `ocr.py`'s ensemble silently filter out Mistral results via
`isinstance(r, OCRResult)`, because the two classes were unrelated.
Both modules now import `OCRResult` from here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .llm_log import Usage


@dataclass
class OCRResult:
    """One OCR provider's output for one page.

    `provider` / `candidates` / `disagreement` / `flagged_low_confidence`
    are populated by the ensemble after the winner is picked; single-
    provider runs leave them at the defaults.
    """

    text: str
    usage: Usage
    latency_ms: int
    # Multi-provider audit
    provider: str = "gemini"
    candidates: list[dict] = field(default_factory=list)
    disagreement: float = 0.0  # 0.0 = identical, 1.0 = totally different
    flagged_low_confidence: bool = False
