"""Office-format → PDF conversion via LibreOffice headless.

GCs and subcontractors don't all submit PDFs. They send .docx scope
letters, .doc legacy specs, .pptx kickoff decks, .rtf cover letters.
Without conversion these files upload but produce no chunks (PyMuPDF
can't open them) and the LLM classifier guesses from binary bytes —
the file effectively disappears from search and from Phase 4 / 8.

This service shells out to `soffice --headless --convert-to pdf` to
turn any LibreOffice-supported format into a PDF that flows through
our existing pipeline (renderer → classifier → chunker → indexer)
unchanged. Output PDF goes alongside the original in storage so we
keep both — original for audit, PDF for processing.

Supported source formats out of the box:
  Word:    .doc .docx .docm .dot .dotx .rtf
  Excel:   .xls .xlsm .ods            (note: .xlsx still uses our
                                       structured trade-list parser)
  PowerPnt: .ppt .pptx .ppsx .odp
  Writer:  .odt .pages
  Other:   .txt .csv .html .epub
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)


# Extensions we route through LibreOffice. .xlsx is excluded because we
# already have a richer structured parser for it (trade_list_parser.py)
# and converting xlsx → pdf would lose the cell structure.
OFFICE_EXTENSIONS = {
    ".doc",
    ".docx",
    ".docm",
    ".dot",
    ".dotx",
    ".odt",
    ".rtf",
    ".pages",
    ".ppt",
    ".pptx",
    ".ppsx",
    ".pps",
    ".odp",
    ".xls",
    ".xlsm",
    ".ods",
}


class OfficeConverterUnavailable(Exception):
    """LibreOffice (`soffice`) binary not found on PATH."""


def is_office_format(path: Path) -> bool:
    return path.suffix.lower() in OFFICE_EXTENSIONS


def soffice_path() -> str | None:
    """Locate the LibreOffice binary; returns None if not installed."""
    return shutil.which("soffice") or shutil.which("libreoffice")


async def convert_to_pdf(input_path: Path, output_dir: Path) -> Path:
    """Convert an Office document to PDF in `output_dir`. Returns the PDF path.

    Runs `soffice --headless --convert-to pdf` in a subprocess. We use a
    50s timeout — even large .ppt decks finish well under that.
    """
    binary = soffice_path()
    if binary is None:
        raise OfficeConverterUnavailable(
            "soffice / libreoffice not found on PATH. "
            "On macOS: `brew install --cask libreoffice`. "
            "On Debian/Ubuntu: `apt-get install libreoffice-core libreoffice-writer`."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    expected = output_dir / (input_path.stem + ".pdf")
    if expected.exists():
        # Idempotent — don't reconvert if we already produced the PDF
        return expected

    cmd = [
        binary,
        "--headless",
        "--nologo",
        "--nofirststartwizard",
        "--convert-to",
        "pdf",
        "--outdir",
        str(output_dir),
        str(input_path),
    ]
    log.info("office_converter: running %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(
            f"office_converter: timed out converting {input_path.name}"
        )

    if proc.returncode != 0:
        raise RuntimeError(
            f"office_converter: soffice exited {proc.returncode} for "
            f"{input_path.name}: {stderr.decode(errors='replace')[:500]}"
        )

    if not expected.exists():
        raise RuntimeError(
            f"office_converter: soffice claimed success but output PDF "
            f"not found at {expected}"
        )

    log.info(
        "office_converter: %s → %s (%d bytes)",
        input_path.name,
        expected.name,
        expected.stat().st_size,
    )
    return expected
