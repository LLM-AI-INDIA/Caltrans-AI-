"""
End-to-end tests for the Program Fit Evaluator's two multi-module flows:

  A. The HIFL loop — evaluate -> reviewer override -> draft rule -> adjudicate -> synthesize
     rulebook -> re-evaluate with the rulebook injected as institutional memory.
  B. The scanned-PDF pipeline — real tesseract OCR -> chunk/index -> retrieve -> the retrieved
     excerpts reach the scorer's context.

The LLM boundary is faked (no API key needed); OCR uses the real local tesseract.
Run: python3 -m pytest tests/test_program_fit_e2e.py -v
"""
import json
import os
import types

import pytest

from src import program_fit_evaluator as pf
from src import program_fit_memory_manager as mm

SCANNED = "/tmp/nomd4/Traffic_Operations_Report_Oct08 Appendices.pdf"
_have_scanned = os.path.exists(SCANNED)
_have_tesseract = __import__("shutil").which("tesseract") is not None


# ---- fake LLM client that captures prompts and returns canned output --------------------------
class _FakeCompletions:
    def __init__(self, capture, canned):
        self._capture, self._canned = capture, canned

    def create(self, model, messages, **kwargs):
        self._capture["system"] = messages[0]["content"]
        self._capture["user"] = messages[1]["content"]
        choice = types.SimpleNamespace(
            message=types.SimpleNamespace(content=self._canned), finish_reason="stop")
        return types.SimpleNamespace(choices=[choice])


class _FakeClient:
    def __init__(self, capture, canned):
        self.chat = types.SimpleNamespace(completions=_FakeCompletions(capture, canned))


def _canned_eval(scores=None):
    scores = scores or {}
    return json.dumps({
        "project_name": "SR99/120", "district": "10", "program": "TCEP",
        "criteria": [{"criterion_id": c["id"], "name": c["name"], "group": c["group"],
                      "score": scores.get(c["id"], 3), "source_reasoning": "cited",
                      "missing_info": False, "confidence": 0.8} for c in pf.RUBRIC_TCEP],
        "missing_criteria": [], "summary": "ok",
    })


# ============================================================================================
# A. HIFL loop end-to-end
# ============================================================================================
def test_hifl_loop_injects_reviewer_calibration_on_reevaluation(monkeypatch):
    # --- Round 1: AI scores Advanced Technology (T6) low ---
    capture1 = {}
    monkeypatch.setattr(pf, "_get_client",
                        lambda *a, **k: _FakeClient(capture1, _canned_eval({"T6": 2})))
    eval1 = pf.run_program_fit_evaluation(
        {"narratives": [{"name": "form", "text": "ITS deployment on the corridor"}]},
        program="TCEP")
    assert "error" not in eval1
    t6 = next(c for c in eval1["criteria"] if c["criterion_id"] == "T6")
    assert t6["score"] == 2

    # --- Reviewer overrides T6 up and drafts a calibration rule ---
    rule = mm.make_draft_rule(
        criterion_id="T6",
        summary="Advanced Technology scored low despite documented ITS deployment",
        source_evidence="Narrative cites corridor ITS deployment",
        user_rationale="ITS deployment is a direct Advanced Technology benefit; score should be 4")
    ok, _ = mm.validate_rule(rule)
    assert ok

    # --- Adjudicator approves (LLM faked) ---
    monkeypatch.setattr(mm, "_get_client", lambda *a, **k: _FakeClient(
        {}, json.dumps({"approved": True, "concern": "", "clarifying_question": ""})))
    verdict = mm.adjudicate_rule(rule)
    assert verdict["approved"] is True
    rule["status"] = "approved"

    # --- Synthesize the rulebook, persist + reload it ---
    rulebook, _note = mm.synthesize_rulebook([], [rule])
    assert len(rulebook) == 1
    reloaded, warn = mm.load_rulebook(mm.save_rulebook(rulebook))
    assert len(reloaded) == 1

    # --- Round 2: re-evaluate WITH the rulebook -> calibration reaches the prompt ---
    capture2 = {}
    monkeypatch.setattr(pf, "_get_client",
                        lambda *a, **k: _FakeClient(capture2, _canned_eval({"T6": 4})))
    eval2 = pf.run_program_fit_evaluation(
        {"narratives": [{"name": "form", "text": "ITS deployment on the corridor"}]},
        program="TCEP", pf_rules=reloaded)
    assert "error" not in eval2
    assert "Advanced Technology scored low despite documented ITS deployment" in capture2["system"]


def test_hifl_empty_rulebook_is_noop(monkeypatch):
    capture = {}
    monkeypatch.setattr(pf, "_get_client",
                        lambda *a, **k: _FakeClient(capture, _canned_eval()))
    base = {}
    monkeypatch.setattr(pf, "_get_client",
                        lambda *a, **k: _FakeClient(base, _canned_eval()))
    pf.run_program_fit_evaluation({"narratives": []}, program="TCEP", pf_rules=[])
    # No institutional-memory header when there are no approved rules.
    assert "INSTITUTIONAL MEMORY" not in base["system"]


# ============================================================================================
# B. Scanned-PDF -> OCR -> retrieval -> scorer context (real tesseract)
# ============================================================================================
@pytest.mark.skipif(not (_have_scanned and _have_tesseract),
                    reason="scanned appendix or tesseract not available")
def test_scanned_pdf_ocr_reaches_scorer_context(monkeypatch, tmp_path):
    from src.program_fit_ocr import extract_pdf_text
    from src.program_fit_retrieval import HybridRetriever

    # Real OCR of the first few scanned pages.
    ocr = extract_pdf_text(SCANNED, cache_dir=str(tmp_path), max_pages=3)
    assert ocr["num_ocr"] >= 1
    full = ocr["full_text"]
    assert full.strip(), "OCR produced no text"

    # Pick a real distinctive token that OCR actually recovered.
    token = next((w for w in full.split() if len(w) >= 6 and w.isalpha()), None)
    assert token, "no usable OCR token found"

    retriever = HybridRetriever()
    retriever.build([full])

    capture = {}
    monkeypatch.setattr(pf, "_get_client",
                        lambda *a, **k: _FakeClient(capture, _canned_eval()))
    out = pf.run_program_fit_evaluation({"retriever": retriever, "narratives": []}, program="TCEP")
    assert "error" not in out
    # The OCR'd text must have flowed through retrieval into the scorer's user message.
    assert token in capture["user"]


@pytest.mark.slow  # OCRs the full 234-page appendix (~4-5 min cold, cached after); run with -m slow
@pytest.mark.skipif(not (_have_scanned and _have_tesseract),
                    reason="scanned appendix or tesseract not available")
def test_extract_package_builds_retriever_for_scanned_pdf(tmp_path, monkeypatch):
    monkeypatch.setenv("PROGRAM_FIT_CACHE_DIR", str(tmp_path))
    pkg = pf.extract_package([SCANNED])
    # A scanned PDF should yield a retriever (OCR corpus indexed), not just empty narratives.
    assert pkg.get("retriever") is not None
    hits = pkg["retriever"].retrieve("traffic volume level of service delay", k=3)
    assert hits and hits[0]["chunk"].strip()
