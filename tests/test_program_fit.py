"""
TDD suite for src/program_fit_evaluator.py (Program Fit Evaluator).

Layers:
  1. Rubric/constant contract tests — pass as soon as the module imports.
  2. Deterministic pure-function tests (rating math, rules) — the TDD red->green targets.
  3. Sample-file reader/integration tests — assert against real ground truth from the two
     provided nomination packages; auto-skip if the sample files aren't present.

Run:  python -m pytest tests/test_program_fit.py -v
"""
import os

import pytest

from src import program_fit_evaluator as pf

# --- Sample file locations (from the provided nomination zips) -------------------------------
D4_XLSM = "/tmp/nomd4/80 SPDR Cal BCA model.xlsm"
D4_XLSX = "/tmp/nomd4/80 SPDR Ph2 SB1 Cycle 5 Program Fit 032726.xlsx"
D10_XLSM = "/tmp/nomd10/Cal BC_8-1_SJCOG SR99-120 Interchange phase 1B 10.14.25 (1).xlsm"
D10_XLSX = "/tmp/nomd10/SB1 Cycle 5 Program Fit 09_29_2025_FINAL SR120-99 Phase 1B 3.31.2026 (1).xlsx"

_have_d4 = os.path.exists(D4_XLSM) and os.path.exists(D4_XLSX)
_have_d10 = os.path.exists(D10_XLSM) and os.path.exists(D10_XLSX)


# ============================================================================================
# 1. Rubric / constant contract
# ============================================================================================
class TestRubricContract:
    def test_tcep_rubric_has_10_criteria(self):
        assert len(pf.RUBRIC_TCEP) == 10

    def test_sccp_rubric_has_10_criteria(self):
        assert len(pf.RUBRIC_SCCP) == 10

    def test_three_freight_seven_transport(self):
        freight = [c for c in pf.RUBRIC_TCEP if c["group"] == "Freight System"]
        transport = [c for c in pf.RUBRIC_TCEP if c["group"] == "Transportation System"]
        assert len(freight) == 3
        assert len(transport) == 7

    def test_criteria_ids_unique(self):
        ids = [c["id"] for c in pf.RUBRIC_TCEP]
        assert len(ids) == len(set(ids)) == 10

    def test_rating_anchors_cover_1_to_5(self):
        assert set(pf.RATING_ANCHORS.keys()) == {1, 2, 3, 4, 5}

    def test_ineligibility_conditions_present(self):
        # 9 verbatim conditions from Risk Ratings B9 (spec §5.4)
        assert len(pf.INELIGIBILITY_CONDITIONS) == 9

    def test_calbc_defined_names_present(self):
        for name in ("LifeCycleCost", "NetPresentValue", "Payback", "DiscRate"):
            assert name in pf.CALBC_DEFINED_NAMES


# ============================================================================================
# 2. Deterministic rating math (spec §4.3: AVERAGE of 10 -> bucket)
# ============================================================================================
class TestRatingMath:
    def _scores(self, value):
        return {c["id"]: value for c in pf.RUBRIC_TCEP}  # all 10 criteria == value

    def test_all_fives_is_high(self):
        r = pf.compute_program_fit_rating(self._scores(5))
        assert r["average"] == 5.0
        assert r["rating"] == "HIGH"

    def test_all_threes_is_medium(self):
        r = pf.compute_program_fit_rating(self._scores(3))
        assert r["average"] == 3.0
        assert r["rating"] == "MEDIUM"

    def test_all_ones_is_low(self):
        r = pf.compute_program_fit_rating(self._scores(1))
        assert r["average"] == 1.0
        assert r["rating"] == "LOW"

    def test_all_twos_is_medium_low(self):
        r = pf.compute_program_fit_rating(self._scores(2))
        assert r["rating"] == "MEDIUM-LOW"

    def test_four_point_zero_is_medium_high(self):
        # e.g. five 5s + five 3s -> avg 4.0
        scores = {c["id"]: (5 if i < 5 else 3) for i, c in enumerate(pf.RUBRIC_TCEP)}
        r = pf.compute_program_fit_rating(scores)
        assert r["average"] == 4.0
        assert r["rating"] == "MEDIUM-HIGH"

    def test_prc_pool_average_is_medium(self):
        # PRC population avg was 2.72 -> MEDIUM (spec §4.5)
        r = pf.compute_program_fit_rating([3, 3, 3, 3, 3, 2, 2, 3, 2, 3])  # avg 2.7
        assert r["rating"] == "MEDIUM"

    def test_missing_criterion_is_padded_to_ten(self):
        # If the LLM omits a criterion, score_all_factors must default it to 2 (insufficient
        # info, spec §4.2) so the overall average is always over 10 (reviewer finding #2),
        # not silently inflated by a smaller denominator.
        criteria = [
            {"criterion_id": c["id"], "score": 5}
            for c in pf.RUBRIC_TCEP[:9]  # only 9 of 10 returned
        ]
        factors = pf.score_all_factors({"criteria": criteria})
        assert len(factors["scores"]) == 10
        missing_id = pf.RUBRIC_TCEP[9]["id"]
        assert factors["scores"][missing_id] == 2       # padded, not dropped
        assert missing_id in factors["missing"]
        # average over 10 = (9*5 + 2)/10 = 4.7 -> HIGH, not 45/9 = 5.0
        rating = pf.compute_program_fit_rating(factors["scores"])
        assert round(rating["average"], 2) == 4.7

    def test_accepts_list_or_dict(self):
        as_list = pf.compute_program_fit_rating([4] * 10)
        as_dict = pf.compute_program_fit_rating({c["id"]: 4 for c in pf.RUBRIC_TCEP})
        assert as_list["rating"] == as_dict["rating"] == "MEDIUM-HIGH"

    def test_empty_scores_does_not_crash(self):
        # A malformed-but-not-errored LLM response can yield no usable criteria.
        # Rating math must degrade gracefully, never ZeroDivisionError (reviewer finding #1).
        for empty in ({}, []):
            r = pf.compute_program_fit_rating(empty)
            assert isinstance(r, dict)
            assert r["average"] == 0.0
            assert r["rating"] == "INCOMPLETE"
            assert r["split"] is False

    def test_exact_half_boundary_is_split(self):
        # avg exactly 3.5 -> split label between MEDIUM and MEDIUM-HIGH
        scores = [4, 4, 4, 4, 4, 3, 3, 3, 3, 5]  # sum 37 -> 3.7 ... use a clean 3.5
        scores = [4, 4, 4, 4, 4, 3, 3, 3, 3, 3]  # sum 35 -> 3.5
        r = pf.compute_program_fit_rating(scores)
        assert r["average"] == 3.5
        assert r["split"] is True
        assert "MEDIUM" in r["rating"] and "MEDIUM-HIGH" in r["rating"]


# ============================================================================================
# 3. Cal-B/C reader — real sample files, defined-name reads, edition branch (spec §6)
# ============================================================================================
@pytest.mark.skipif(not _have_d4, reason="D4 sample package not present")
class TestCalBCReaderSketch:
    def test_d4_is_sketch_edition(self):
        out = pf.read_calbc_model(D4_XLSM)
        assert out["edition"] == "Sketch"

    def test_d4_benefit_cost_ratio(self):
        out = pf.read_calbc_model(D4_XLSM)
        assert round(out["benefit_cost_ratio"], 2) == 6.71


@pytest.mark.skipif(not _have_d10, reason="D10 sample package not present")
class TestCalBCReaderCorridor:
    def test_d10_is_corridor_edition(self):
        out = pf.read_calbc_model(D10_XLSM)
        assert out["edition"] == "Corridor"

    def test_d10_benefit_cost_ratio(self):
        out = pf.read_calbc_model(D10_XLSM)
        assert round(out["benefit_cost_ratio"], 2) == 1.98


# ============================================================================================
# 4. Contradiction detection — real D10 $65M vs $60M mismatch (spec §8)
# ============================================================================================
@pytest.mark.skipif(not _have_d10, reason="D10 sample package not present")
class TestContradictionDetection:
    def test_d10_funding_mismatch_flagged(self):
        pkg = pf.extract_package([D10_XLSX])
        contradictions = pf.detect_contradictions(pkg)
        # Section IV TCEP request ($65M) contradicts the fund-table CON total ($60M).
        joined = " ".join(str(c) for c in contradictions).lower()
        assert any("request" in str(c).lower() or "fund" in str(c).lower() for c in contradictions)
        assert contradictions, "expected at least one contradiction for D10"


# ============================================================================================
# 6. LLM scorer — prompt structure (offline-safe) + graceful failure
# ============================================================================================
class TestScoringPrompt:
    def test_prompt_mentions_all_ten_criteria(self):
        prompt = pf._build_system_prompt("TCEP")
        for c in pf.RUBRIC_TCEP:
            assert c["name"] in prompt, f"criterion {c['name']} missing from prompt"

    def test_prompt_states_confidence_gate(self):
        # Confidence < 0.5 must force the low/insufficient rating (mirrors PDE N/E gate).
        prompt = pf._build_system_prompt("TCEP").lower()
        assert "confidence" in prompt
        assert "0.5" in prompt or "0.50" in prompt

    def test_prompt_includes_1_to_5_anchors(self):
        prompt = pf._build_system_prompt("TCEP")
        # every anchor level should be represented
        assert "5" in prompt and "1" in prompt
        assert "primary" in prompt.lower()  # High anchor language

    def test_run_evaluation_returns_error_dict_on_failure(self, monkeypatch):
        # With no working client, the scorer must degrade to {"error": ...}, never raise.
        def _boom(*a, **k):
            raise RuntimeError("no api key")
        monkeypatch.setattr(pf, "_get_client", _boom)
        out = pf.run_program_fit_evaluation({"narratives": [{"name": "x", "text": "y"}]}, program="TCEP")
        assert isinstance(out, dict)
        assert "error" in out


# ============================================================================================
# 8. Excel report builder — produces a valid workbook (offline)
# ============================================================================================
class TestExcelBuilder:
    def _fixture(self):
        evaluation = {
            "project_name": "Test Project", "district": "4", "program": "TCEP",
            "criteria": [
                {"criterion_id": c["id"], "name": c["name"], "group": c["group"],
                 "score": 3, "source_reasoning": "cited text", "missing_info": False,
                 "confidence": 0.8}
                for c in pf.RUBRIC_TCEP
            ],
            "missing_criteria": [], "summary": "ok",
        }
        rating = pf.compute_program_fit_rating({c["id"]: 3 for c in pf.RUBRIC_TCEP})
        eligibility = {"eligible": True, "failures": [], "schedule_risk": "Low",
                       "funding_risk": "Low", "warnings": []}
        contradictions = [{"field": "TCEP funding request", "detail": "gap", "values": {},
                           "severity": "high"}]
        return evaluation, rating, eligibility, contradictions

    def test_returns_valid_xlsx_bytesio(self):
        import openpyxl
        from io import BytesIO
        ev, rating, elig, contra = self._fixture()
        buf = pf.build_program_fit_excel(ev, rating, elig, contra, "Test Project")
        assert buf is not None
        data = buf.getvalue() if hasattr(buf, "getvalue") else buf
        assert len(data) > 0
        wb = openpyxl.load_workbook(BytesIO(data))
        assert len(wb.sheetnames) >= 1  # at least a summary sheet


# ============================================================================================
# 9. Workbook reader — real sample files
# ============================================================================================
@pytest.mark.skipif(not _have_d4, reason="D4 sample package not present")
def test_d4_workbook_reads_project_identity():
    wb = pf.read_program_fit_workbook(D4_XLSX)
    assert wb["project_name"]
    assert wb["district"]


# ============================================================================================
# 10. Integration — HIFL memory injection + retrieval context feed the scorer
# ============================================================================================
import json as _json
import types as _types


class _FakeCompletions:
    def __init__(self, capture, canned):
        self._capture = capture
        self._canned = canned

    def create(self, model, messages, **kwargs):
        self._capture["system"] = messages[0]["content"]
        self._capture["user"] = messages[1]["content"]
        msg = _types.SimpleNamespace(content=self._canned)
        choice = _types.SimpleNamespace(message=msg, finish_reason="stop")
        return _types.SimpleNamespace(choices=[choice])


class _FakeClient:
    def __init__(self, capture, canned):
        self.chat = _types.SimpleNamespace(completions=_FakeCompletions(capture, canned))


def _canned_eval_json():
    return _json.dumps({
        "project_name": "X", "district": "4", "program": "TCEP",
        "criteria": [{"criterion_id": c["id"], "name": c["name"], "group": c["group"],
                      "score": 3, "source_reasoning": "cited", "missing_info": False,
                      "confidence": 0.8} for c in pf.RUBRIC_TCEP],
        "missing_criteria": [], "summary": "ok",
    })


class TestIntegration:
    def test_pf_rules_injected_into_prompt(self, monkeypatch):
        from src import program_fit_memory_manager as mm
        rule = mm.make_draft_rule(
            "T6", "Advanced Technology benefits were consistently understated",
            "p.12 ITS deployment", "reviewer calibration")
        rule["status"] = "approved"
        capture = {}
        monkeypatch.setattr(pf, "_get_client",
                            lambda *a, **k: _FakeClient(capture, _canned_eval_json()))
        out = pf.run_program_fit_evaluation(
            {"narratives": [{"name": "n", "text": "body"}]}, program="TCEP", pf_rules=[rule])
        assert "error" not in out
        # The approved rule's substance must reach the system prompt as institutional memory.
        assert "Advanced Technology benefits were consistently understated" in capture["system"]

    def test_retriever_context_included(self, monkeypatch):
        from src.program_fit_retrieval import HybridRetriever
        r = HybridRetriever()
        r.build(["ZORPTASTIC freight bottleneck relief at the interchange reduces truck delay",
                 "unrelated pavement drainage work", "unrelated landscaping"])
        capture = {}
        monkeypatch.setattr(pf, "_get_client",
                            lambda *a, **k: _FakeClient(capture, _canned_eval_json()))
        out = pf.run_program_fit_evaluation(
            {"retriever": r, "narratives": []}, program="TCEP")
        assert "error" not in out
        # A distinctive retrieved token must appear in the user message context.
        assert "ZORPTASTIC" in capture["user"]

    def test_pf_rules_defaults_none_backward_compatible(self, monkeypatch):
        # Existing callers that don't pass pf_rules must still work.
        capture = {}
        monkeypatch.setattr(pf, "_get_client",
                            lambda *a, **k: _FakeClient(capture, _canned_eval_json()))
        out = pf.run_program_fit_evaluation({"narratives": [{"name": "n", "text": "b"}]})
        assert "error" not in out and out["criteria"]


# ============================================================================================
# 11. Fixes from the 2026-07-10 real-package audit
# ============================================================================================
import datetime as _dt


class TestParseAnswerDate:
    def test_datetime_and_date_pass_through(self):
        assert pf._parse_answer_date(_dt.datetime(2029, 8, 15)) == _dt.date(2029, 8, 15)
        assert pf._parse_answer_date(_dt.date(2029, 8, 15)) == _dt.date(2029, 8, 15)

    def test_month_slash_year_string(self):
        # The D10 sample stores milestones as strings like '08/2029'.
        assert pf._parse_answer_date("08/2029") == _dt.date(2029, 8, 1)
        assert pf._parse_answer_date("3/2029") == _dt.date(2029, 3, 1)

    def test_full_date_strings(self):
        assert pf._parse_answer_date("06/30/2029") == _dt.date(2029, 6, 30)
        assert pf._parse_answer_date("2029-06-30") == _dt.date(2029, 6, 30)

    def test_month_name_strings(self):
        assert pf._parse_answer_date("Aug 2029") == _dt.date(2029, 8, 1)
        assert pf._parse_answer_date("August 2029") == _dt.date(2029, 8, 1)

    def test_non_dates_return_none(self):
        for v in (None, "", "Enter Text or N/A", 12.5, "13/2029", "N/A"):
            assert pf._parse_answer_date(v) is None


class TestScheduleRiskFromStringDates:
    def _pkg(self, con_answer):
        return {"workbook": {
            "program": "TCEP",
            "funding": {},
            "answers": {"39": {"question": "8. Target Begin Construction (Month/Year)",
                               "answer": con_answer}},
        }}

    def test_d10_style_string_con_date_is_high_risk(self):
        # CON '08/2029' falls in the High window (Jun-Dec 2029); previously silently dropped.
        out = pf.screen_eligibility(self._pkg("08/2029"))
        assert out["schedule_risk"] == "High"

    def test_string_con_date_after_deadline_is_ineligible(self):
        out = pf.screen_eligibility(self._pkg("01/2030"))
        assert out["eligible"] is False
        assert out["failures"]


class TestFundingSheetMilestoneDates:
    def test_synthetic_1933_funding_milestone_flagged(self):
        pkg = {"workbook": {
            "funding": {},
            "answers": {},
            "funding_milestones": [
                {"cell": "'Project Funding Info #1'!V33", "value": "1933-02-01", "year": 1933},
                {"cell": "'Project Funding Info #1'!V20", "value": "2027-10-01", "year": 2027},
            ],
        }}
        flags = pf.detect_contradictions(pkg)
        milestone_flags = [c for c in flags if c.get("field") == "milestone date"]
        assert len(milestone_flags) == 1
        assert "1933" in milestone_flags[0]["detail"]

    @pytest.mark.skipif(not _have_d10, reason="D10 sample package not present")
    def test_d10_real_1933_dates_flagged(self):
        pkg = pf.extract_package([D10_XLSX])
        assert pkg["workbook"].get("funding_milestones"), "funding-sheet dates not captured"
        flags = pf.detect_contradictions(pkg)
        joined = " ".join(c.get("detail", "") for c in flags)
        assert "1933" in joined, "the funding-sheet 1930s closeout artifacts must be flagged"

    @pytest.mark.skipif(not _have_d10, reason="D10 sample package not present")
    def test_d10_string_milestones_give_high_schedule_risk(self):
        # RTL '03/2029' / CON '08/2029' are strings in the real D10 workbook.
        pkg = pf.extract_package([D10_XLSX])
        out = pf.screen_eligibility(pkg)
        assert out["schedule_risk"] == "High"


class TestMetricTablesReachPrompt:
    def test_docx_tables_rendered_into_user_message(self, monkeypatch):
        # Table-only backup DOCX used to be invisible to scoring (empty paragraph text).
        capture = {}
        monkeypatch.setattr(pf, "_get_client",
                            lambda *a, **k: _FakeClient(capture, _canned_eval_json()))
        pkg = {
            "narratives": [],
            "metric_tables": [{
                "name": "SR99 Performance Metrics.docx",
                "tables": [[["Metric", "Value", "BCA source"],
                            ["VMT reduction", "28,548", "Cal-B/C 1) Project Info"]]],
            }],
        }
        out = pf.run_program_fit_evaluation(pkg, program="TCEP")
        assert "error" not in out
        assert "PERFORMANCE-METRIC BACKUP TABLES" in capture["user"]
        assert "28,548" in capture["user"]
        assert "SR99 Performance Metrics.docx" in capture["user"]
