"""
TDD suite for src/program_fit_memory_manager.py — the Human-in-the-Feedback-Loop
(HIFL) precedent-memory engine for the Program Fit Evaluator.

Mirrors the PDE memory manager (src/pde_memory_manager.py) but specialized for the
10 Program Fit rubric criteria (F1-F3, T1-T7).

Run:  python3 -m pytest tests/test_program_fit_hifl.py -v
"""
import json
import types

import pytest

from src import program_fit_memory_manager as mm


def test_synthesize_keeps_reaffirmed_rule_over_cap():
    # A rule re-approved in a later cycle must survive the 20-rule cap (it's "most recent"),
    # not be dropped as if it were the oldest (reviewer finding #3).
    reaffirmed = mm.make_draft_rule("F1", "throughput rule", "ev", "rationale")
    reaffirmed["status"] = "approved"
    existing = [reaffirmed] + [
        {**mm.make_draft_rule("T1", f"filler {i}", "ev", "r"), "status": "approved"}
        for i in range(25)
    ]
    # Re-approve the F1 rule now (same criterion_id+summary) in the new batch.
    merged, _ = mm.synthesize_rulebook(existing, [dict(reaffirmed)])
    assert len(merged) == mm.MAX_APPROVED_RULES
    keys = {(r["criterion_id"], r["summary"]) for r in merged}
    assert ("F1", "throughput rule") in keys  # survived the cap


# ---------------------------------------------------------------------------
# Fakes for LLM client monkeypatching
# ---------------------------------------------------------------------------
class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, content):
        self._content = content

    def create(self, *args, **kwargs):
        return _FakeResponse(self._content)


class _FakeChat:
    def __init__(self, content):
        self.completions = _FakeCompletions(content)


class _FakeClient:
    def __init__(self, content):
        self.chat = _FakeChat(content)


def _approved_rule(criterion_id="T6", summary="Advanced Tech benefits were understated"):
    r = mm.make_draft_rule(criterion_id, summary, "p.12 cites ITS deployment",
                           "reviewer disagrees")
    r["status"] = "approved"
    return r


# ===========================================================================
# 1. make_draft_rule + validate_rule
# ===========================================================================
def test_make_draft_rule_has_all_keys_and_is_valid():
    rule = mm.make_draft_rule("T6", "Advanced Tech benefits were understated",
                              "p.12 cites ITS deployment", "reviewer disagrees")
    assert mm.REQUIRED_RULE_KEYS <= set(rule.keys())
    assert rule["status"] == "draft"
    assert rule["criterion_id"] == "T6"
    ok, msg = mm.validate_rule(rule)
    assert ok, msg


def test_make_draft_rule_id_is_deterministic():
    r1 = mm.make_draft_rule("T6", "Advanced Tech benefits were understated",
                            "p.12 cites ITS deployment", "reviewer disagrees")
    r2 = mm.make_draft_rule("T6", "Advanced Tech benefits were understated",
                            "p.12 cites ITS deployment", "reviewer disagrees")
    assert r1["rule_id"] == r2["rule_id"]


# ===========================================================================
# 2. validate_rule rejects bad rules
# ===========================================================================
def test_validate_rule_rejects_missing_keys():
    ok, msg = mm.validate_rule({"criterion_id": "F1"})
    assert not ok
    assert msg


def test_validate_rule_rejects_bad_status():
    rule = mm.make_draft_rule("F1", "summary here", "evidence", "rationale")
    rule["status"] = "bogus"
    ok, msg = mm.validate_rule(rule)
    assert not ok
    assert msg


def test_validate_rule_rejects_empty_summary():
    rule = mm.make_draft_rule("F1", "summary", "evidence", "rationale")
    rule["summary"] = ""
    ok, _ = mm.validate_rule(rule)
    assert not ok


# ===========================================================================
# 3. save / load round-trip
# ===========================================================================
def test_save_load_roundtrip():
    rule = mm.make_draft_rule("T3", "Congestion reduction weighted too low",
                              "p.4 travel-time data", "peak-hour delay is severe")
    blob = mm.save_rulebook([rule])
    assert isinstance(blob, str)
    rules, warn = mm.load_rulebook(blob)
    assert len(rules) == 1
    assert rules[0]["criterion_id"] == "T3"


def test_load_rulebook_never_raises_on_garbage():
    rules, warn = mm.load_rulebook("not valid json {")
    assert rules == []
    assert warn  # warning message present


# ===========================================================================
# 4. synthesize_rulebook caps at MAX_APPROVED_RULES
# ===========================================================================
def test_synthesize_caps_and_excludes_non_approved():
    approved = [_approved_rule("T6", f"summary {i}") for i in range(25)]
    rejected = mm.make_draft_rule("F2", "rejected one", "e", "r")
    rejected["status"] = "rejected"
    draft = mm.make_draft_rule("F3", "draft one", "e", "r")  # status draft

    result, note = mm.synthesize_rulebook([], approved + [rejected, draft])
    assert len(result) == mm.MAX_APPROVED_RULES == 20
    assert all(r["status"] == "approved" for r in result)


# ===========================================================================
# 5. build_institutional_memory_block
# ===========================================================================
def test_memory_block_contains_criterion_and_summary():
    rule = _approved_rule("T6", "Advanced Tech benefits were understated")
    block = mm.build_institutional_memory_block([rule])
    assert "T6" in block
    assert "Advanced Tech benefits were understated" in block


def test_memory_block_empty_for_no_rules():
    assert mm.build_institutional_memory_block([]) == ""


# ===========================================================================
# 6. adjudicate_rule guard: client raises -> safe dict, no exception
# ===========================================================================
def test_adjudicate_rule_guard_on_client_failure(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("no api key")

    monkeypatch.setattr(mm, "_get_client", _boom)
    rule = mm.make_draft_rule("T6", "some correction", "evidence", "rationale")
    result = mm.adjudicate_rule(rule)
    assert result["approved"] is False
    assert result["concern"]  # non-empty
    assert "clarifying_question" in result


def test_adjudicate_defense_guard_on_client_failure(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("no api key")

    monkeypatch.setattr(mm, "_get_client", _boom)
    rule = mm.make_draft_rule("T6", "some correction", "evidence", "rationale")
    result = mm.adjudicate_defense(rule, "here is my defense")
    assert result["approved"] is False
    assert result["concern"]


# ===========================================================================
# 7. adjudicate_rule happy path with canned JSON
# ===========================================================================
def test_adjudicate_rule_happy_path(monkeypatch):
    canned = json.dumps({"approved": True, "concern": "", "clarifying_question": ""})
    monkeypatch.setattr(mm, "_get_client", lambda *a, **k: _FakeClient(canned))
    rule = mm.make_draft_rule("T6", "some correction", "evidence", "rationale")
    result = mm.adjudicate_rule(rule)
    assert result["approved"] is True
    assert result["concern"] == ""
    assert result["clarifying_question"] == ""
