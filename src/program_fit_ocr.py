"""
Local OCR for scanned traffic-study PDFs (Program Fit Evaluator).

Some nomination packages ship a large scanned appendix (e.g. a 234-page traffic-operations
appendix with *zero* embedded text). This module renders each such page with PyMuPDF (``fitz``)
and runs tesseract via ``pytesseract`` to recover the text. Because OCR is expensive, results
are cached on disk keyed by the SHA-256 of the raw file bytes, so a given file is OCR'd once.

Design notes:
  * Heavy/optional bindings (``pytesseract``, ``PIL``) are imported lazily inside functions so
    that importing this module never fails when a binding is absent. After ``_ensure_ocr_deps()``
    runs, ``pytesseract`` and ``Image`` are available as module attributes (handy for tests/mocks).
  * Every OCR call is defensive: a failure on one page yields ``""`` for that page rather than
    aborting the whole document.
"""
from __future__ import annotations

import hashlib
import json
import os

# Populated lazily by _ensure_ocr_deps(); declared here so they exist as module attributes.
pytesseract = None  # type: ignore[assignment]
Image = None  # type: ignore[assignment]


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ensure_ocr_deps():
    """Import pytesseract + PIL.Image once and bind them to module globals.

    Kept lazy so module import never fails if the bindings are missing; callers that actually
    need OCR trigger the import here. Returns (pytesseract, Image).
    """
    global pytesseract, Image
    if pytesseract is None:
        import pytesseract as _pt  # noqa: WPS433 (intentional lazy import)

        pytesseract = _pt
    if Image is None:
        from PIL import Image as _Image  # noqa: WPS433

        Image = _Image
    return pytesseract, Image


def page_is_scanned(page, min_chars: int = 20) -> bool:
    """True when a fitz page has (almost) no embedded text and therefore needs OCR."""
    try:
        text = page.get_text().strip()
    except Exception:
        return True
    return len(text) < min_chars


def ocr_page(page, dpi: int = 220) -> str:
    """Render a fitz page to a raster image and OCR it. Returns "" on any failure."""
    try:
        import fitz  # noqa: WPS433

        pt, image = _ensure_ocr_deps()
        matrix = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=matrix)
        import io  # noqa: WPS433

        img = image.open(io.BytesIO(pix.tobytes("png")))
        return pt.image_to_string(img)
    except Exception:
        return ""


def _default_cache_dir() -> str:
    env = os.getenv("PROGRAM_FIT_CACHE_DIR")
    if env:
        return env
    return os.path.join(_repo_root(), ".pf_cache")


def _read_source_bytes(source):
    """Return (raw_bytes, source_name, is_stream) for a path / bytes / file-like source."""
    if isinstance(source, (bytes, bytearray)):
        return bytes(source), "<bytes>", True
    if hasattr(source, "read"):
        try:
            source.seek(0)
        except Exception:
            pass
        data = source.read()
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        name = getattr(source, "name", "<stream>")
        return bytes(data), os.path.basename(str(name)), True
    # Treat as a path.
    path = str(source)
    if not os.path.exists(path):
        raise ValueError(f"PDF source not found: {path}")
    with open(path, "rb") as fh:
        return fh.read(), os.path.basename(path), False


def extract_pdf_text(source, cache_dir=None, dpi: int = 220, max_pages=None) -> dict:
    """Extract text from a PDF, OCR'ing scanned pages, with file-hash caching.

    ``source`` may be a filesystem path, raw ``bytes``, or a file-like object (read/seek).
    Pages with a usable embedded text layer are used verbatim (``ocr=False``); scanned pages
    are OCR'd (``ocr=True``). Results are cached under ``cache_dir`` keyed by the file's SHA-256
    so the expensive OCR runs only once per (file, max_pages) pair.

    Returns a dict::

        {
          "source_name": str,
          "num_pages": int,
          "num_ocr": int,
          "pages": [{"page": int (0-indexed), "text": str, "ocr": bool}, ...],
          "full_text": str,  # "\n\n".join of page texts
        }

    Raises ``ValueError`` if the source cannot be opened as a PDF.
    """
    import fitz  # noqa: WPS433

    raw, source_name, is_stream = _read_source_bytes(source)

    cache_dir = cache_dir or _default_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)

    digest = hashlib.sha256(raw).hexdigest()
    pages_key = "all" if max_pages is None else str(max_pages)
    # dpi is part of the key so a later higher-DPI request doesn't return a stale low-DPI result.
    cache_path = os.path.join(cache_dir, f"ocr_{digest[:16]}_{pages_key}_dpi{int(dpi)}.json")

    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            # Corrupt cache — fall through and recompute.
            pass

    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception as exc:
        raise ValueError(f"Could not open PDF source '{source_name}': {exc}") from exc

    try:
        total = doc.page_count
        limit = total if max_pages is None else min(total, int(max_pages))

        pages = []
        num_ocr = 0
        for idx in range(limit):
            page = doc[idx]
            if page_is_scanned(page):
                text = ocr_page(page, dpi=dpi)
                is_ocr = True
                num_ocr += 1
            else:
                try:
                    text = page.get_text()
                except Exception:
                    text = ""
                is_ocr = False
            pages.append({"page": idx, "text": text, "ocr": is_ocr})
    finally:
        doc.close()

    result = {
        "source_name": source_name,
        "num_pages": len(pages),
        "num_ocr": num_ocr,
        "pages": pages,
        "full_text": "\n\n".join(p["text"] for p in pages),
    }

    # Do NOT cache a result where OCR ran on scanned pages but recovered nothing — that almost
    # always means the OCR engine was unavailable (missing tesseract binary), and caching the
    # empty text would permanently poison this file's hash key even after the engine is installed.
    ocr_produced_nothing = result["num_ocr"] > 0 and not result["full_text"].strip()
    if not ocr_produced_nothing:
        try:
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump(result, fh)
        except Exception:
            # Caching is best-effort; never fail the call because we couldn't write the cache.
            pass

    return result
