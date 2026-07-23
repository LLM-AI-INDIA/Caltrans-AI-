"""
TDD suite for src/program_fit_ocr.py — local tesseract OCR of scanned traffic-study PDFs
with file-hash caching.

Run:  python3 -m pytest tests/test_program_fit_ocr.py -v

The conftest.py in this directory already inserts the repo root on sys.path, so
`from src import program_fit_ocr` works regardless of the invocation directory.
"""
import json
import os

import pytest

from src import program_fit_ocr

# --- Sample file locations -------------------------------------------------------------------
SCANNED = "/tmp/nomd4/Traffic_Operations_Report_Oct08 Appendices.pdf"  # 234 pp, scanned, 0 text
TEXT = "/tmp/nomd4/Traffic_Operations_Report_Oct08.pdf"                # has embedded text

_have_scanned = os.path.exists(SCANNED)
_have_text = os.path.exists(TEXT)


@pytest.mark.skipif(not _have_scanned, reason="scanned appendix not present")
def test_empty_ocr_result_is_not_cached(tmp_path, monkeypatch):
    # If OCR runs on scanned pages but recovers nothing (engine unavailable), the empty result
    # must NOT be cached — otherwise the file's hash key is poisoned forever (reviewer finding #1).
    monkeypatch.setattr(program_fit_ocr, "ocr_page", lambda page, dpi=220: "")
    out = program_fit_ocr.extract_pdf_text(SCANNED, cache_dir=str(tmp_path), max_pages=2)
    assert out["num_ocr"] >= 1
    assert out["full_text"].strip() == ""
    assert not list(tmp_path.glob("ocr_*.json"))  # nothing cached

_DOC_KEYS = {"page", "text", "ocr"}


# ============================================================================================
# 1. page_is_scanned
# ============================================================================================
@pytest.mark.skipif(not (_have_scanned and _have_text), reason="sample PDFs not present")
def test_page_is_scanned_true_and_false():
    import fitz

    scanned_doc = fitz.open(SCANNED)
    text_doc = fitz.open(TEXT)
    try:
        assert program_fit_ocr.page_is_scanned(scanned_doc[0]) is True
        assert program_fit_ocr.page_is_scanned(text_doc[0]) is False
    finally:
        scanned_doc.close()
        text_doc.close()


# ============================================================================================
# 2. extract_pdf_text on the scanned PDF actually OCRs
# ============================================================================================
@pytest.mark.skipif(not _have_scanned, reason="scanned sample PDF not present")
def test_extract_scanned_runs_ocr(tmp_path):
    result = program_fit_ocr.extract_pdf_text(SCANNED, cache_dir=str(tmp_path), max_pages=5)

    assert result["num_pages"] == 5
    assert len(result["pages"]) == 5
    assert result["num_ocr"] >= 1
    assert any(p["text"].strip() for p in result["pages"])

    for i, page in enumerate(result["pages"]):
        assert set(page.keys()) == _DOC_KEYS
        assert page["page"] == i
        assert isinstance(page["text"], str)
        assert isinstance(page["ocr"], bool)

    assert isinstance(result["source_name"], str)
    assert isinstance(result["full_text"], str)


# ============================================================================================
# 3. Caching — second call reuses the cache file and returns an identical dict
# ============================================================================================
@pytest.mark.skipif(not _have_scanned, reason="scanned sample PDF not present")
def test_caching_reuses_result(tmp_path):
    first = program_fit_ocr.extract_pdf_text(SCANNED, cache_dir=str(tmp_path), max_pages=3)

    cache_files = list(tmp_path.glob("ocr_*.json"))
    assert len(cache_files) == 1, f"expected exactly one cache file, got {cache_files}"

    second = program_fit_ocr.extract_pdf_text(SCANNED, cache_dir=str(tmp_path), max_pages=3)
    assert second == first

    # The cache file content round-trips to the same dict.
    with open(cache_files[0]) as fh:
        assert json.load(fh) == first


# ============================================================================================
# 4. extract_pdf_text on the text PDF uses the embedded layer (no OCR)
# ============================================================================================
@pytest.mark.skipif(not _have_text, reason="text sample PDF not present")
def test_extract_text_uses_embedded_layer(tmp_path):
    result = program_fit_ocr.extract_pdf_text(TEXT, cache_dir=str(tmp_path), max_pages=2)

    assert len(result["pages"]) == 2
    for page in result["pages"]:
        assert page["ocr"] is False
        assert page["text"].strip()
    assert result["num_ocr"] == 0


# ============================================================================================
# 5. Graceful failure — OCR raising must not crash ocr_page
# ============================================================================================
@pytest.mark.skipif(not _have_scanned, reason="scanned sample PDF not present")
def test_ocr_page_graceful_on_failure(monkeypatch):
    import fitz

    doc = fitz.open(SCANNED)
    try:
        program_fit_ocr._ensure_ocr_deps()  # make sure the binding exists to patch

        def _boom(*args, **kwargs):
            raise RuntimeError("tesseract exploded")

        monkeypatch.setattr(program_fit_ocr.pytesseract, "image_to_string", _boom)
        assert program_fit_ocr.ocr_page(doc[0]) == ""
    finally:
        doc.close()


# ============================================================================================
# 6. A missing / unopenable file raises a clear ValueError
# ============================================================================================
def test_bad_source_raises_valueerror(tmp_path):
    with pytest.raises(ValueError):
        program_fit_ocr.extract_pdf_text(str(tmp_path / "does_not_exist.pdf"))
