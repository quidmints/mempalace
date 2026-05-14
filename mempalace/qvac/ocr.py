"""
QVAC OCR adapter.

Wraps QVAC's ONNX-based OCR engine (`ocr-onnx` per the QVAC repo) for the
`mempalace/diary_ingest.py` scanned-document path. Accepts image bytes,
returns transcribed text plus per-region detail when the model emits it.

# Where this plugs in

`diary_ingest.py` (kept-files set) ingests photographed pages of
handwritten or printed diary entries. The current implementation may use
Tesseract or a cloud OCR; this adapter is a local alternative that runs
fully offline through the sidecar.

# Why HTTP and not direct onnxruntime

We could `pip install onnxruntime` and run the OCR model directly in
Python — that would skip the sidecar entirely and reduce one process.
The reason not to: QVAC's `ocr-onnx` includes preprocessing (image
binarization, deskew, line segmentation) and postprocessing (BPE-style
token merging) tuned to the specific ONNX model. Re-implementing that
preprocessing in Python is bug-prone — line detection alone is ~300
lines of opencv. Going through the sidecar means we get QVAC's
pipeline at the cost of one JSON+base64 hop. For diary ingest (batch
processing, not per-keystroke latency) that's a non-issue.

If a future deployment wants direct onnxruntime, this module can be
subclassed with `_transcribe` overridden to call onnxruntime locally.
"""

from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass, field
from typing import Any

from mempalace.qvac.client import QvacClient, get_default_client

logger = logging.getLogger(__name__)


@dataclass
class OCRRegion:
    """One detected text region with bounding box and recognized text."""
    bbox: tuple[int, int, int, int]   # x, y, w, h in original-image pixels
    text: str
    confidence: float = 0.0


@dataclass
class OCRResult:
    text: str                         # full concatenation
    confidence: float = 0.0           # average confidence across regions
    regions: list[OCRRegion] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_high_confidence(self) -> bool:
        return self.confidence >= 0.85


class QvacOCR:
    """OCR via the QVAC sidecar."""

    def __init__(
        self,
        client: QvacClient | None = None,
        *,
        default_language: str = "en",
    ) -> None:
        self._client = client or get_default_client()
        self._default_language = default_language

    def transcribe(
        self,
        image_bytes: bytes,
        *,
        language: str | None = None,
    ) -> OCRResult:
        """Transcribe a single image. Caller supplies raw bytes (PNG, JPG,
        TIFF — whatever the underlying ONNX model accepts)."""
        b64 = base64.b64encode(image_bytes).decode("ascii")
        resp = self._client.ocr(b64, language=language or self._default_language)
        return self._parse_response(resp)

    def transcribe_pages(
        self,
        pages: list[bytes],
        *,
        language: str | None = None,
    ) -> list[OCRResult]:
        """Transcribe each page individually. The sidecar currently has no
        batch endpoint; we sequence them client-side for simplicity."""
        return [self.transcribe(p, language=language) for p in pages]

    # ---------------------------------------------------------------------
    # Internal
    # ---------------------------------------------------------------------

    def _parse_response(self, resp: dict) -> OCRResult:
        text = resp.get("text", "")
        confidence = float(resp.get("confidence", 0.0))
        regions_raw = resp.get("regions", []) or []
        regions: list[OCRRegion] = []
        for r in regions_raw:
            bbox = r.get("bbox") or [0, 0, 0, 0]
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                bbox = [0, 0, 0, 0]
            regions.append(OCRRegion(
                bbox=tuple(int(v) for v in bbox),  # type: ignore[arg-type]
                text=str(r.get("text", "")),
                confidence=float(r.get("confidence", confidence)),
            ))
        return OCRResult(
            text=text,
            confidence=confidence,
            regions=regions,
            raw=resp,
        )
