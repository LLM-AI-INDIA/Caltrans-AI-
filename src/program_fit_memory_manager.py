"""
Program Fit Memory Manager (HIFL)
=================================
The Human-in-the-Feedback-Loop precedent-memory engine for the Program Fit
Evaluator (TCEP / SCCP nomination scoring).

Mirrors src/pde_memory_manager.py, but specialized for the 10 Program Fit rubric
criteria (Freight System F1-F3, Transportation System T1-T7). When an HQ reviewer
overrides an AI criterion score, they draft a "rule" capturing the correction. An
LLM adjudicator guards against manipulative / illogical overrides before a rule
enters institutional memory. Approved rules are synthesized into a rulebook that is
injected into future evaluation prompts as institutional memory.

Rule states: draft -> approved | rejected | deprecated

Rules are persisted as a downloadable/uploadable JSON file (state-as-a-file pattern)
to support stateless Cloud Run deployments without requiring a database.
"""

import json
import hashlib
import logging

# LLM client + defensive JSON parsing are reused from the PDE evaluator so both
# the Cloud Run and Databricks deployment targets keep working.
from src.project_delivery_evaluator import _get_client, _extract_json

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rule Schema
# ---------------------------------------------------------------------------
# Each rule in the rulebook follows this structure:
# {
#   "rule_id":         "pf-t6-a1b2c3d4",                 # deterministic, stable
#   "criterion_id":    "T6",                              # rubric criterion this targets
#   "summary":         "Advanced Tech benefits understated by AI",
#   "source_evidence": "p.12 cites ITS deployment",
#   "user_rationale":  "ITS deployment is a clear Advanced Tech signal",
#   "status":          "approved",                        # draft|approved|rejected|deprecated
#   "version":         1,                                 # monotonically bumped on update
# }

REQUIRED_RULE_KEYS = {"rule_id", "criterion_id", "summary", "source_evidence",
                      "user_rationale", "status", "version"}
VALID_STATUSES = {"draft", "approved", "rejected", "deprecated"}

# Hard cap: approved rulebook never exceeds this many entries.
MAX_APPROVED_RULES = 20


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def _make_rule_id(criterion_id: str, summary: str) -> str:
    """Deterministic rule id: a criterion slug + a stable hash of the summary.

    No randomness / no timestamps, so the same (criterion_id, summary) always
    yields the same id — keeps tests stable and dedupe reliable.
    """
    slug = str(criterion_id).strip().lower().replace(" ", "-") or "criterion"
    digest = hashlib.sha1((str(criterion_id) + "|" + str(summary)).encode("utf-8")).hexdigest()[:8]
    return f"pf-{slug}-{digest}"


def make_draft_rule(criterion_id: str, summary: str, source_evidence: str,
                    user_rationale: str) -> dict:
    """Create a new rule in DRAFT status, ready for adjudication."""
    return {
        "rule_id": _make_rule_id(criterion_id, summary),
        "criterion_id": criterion_id,
        "summary": summary,
        "source_evidence": source_evidence,
        "user_rationale": user_rationale,
        "status": "draft",
        "version": 1,
    }


def validate_rule(rule: dict) -> tuple[bool, str]:
    """Validate a single rule dict against the expected schema.

    Returns (is_valid, error_message).
    """
    if not isinstance(rule, dict):
        return False, "Rule must be a JSON object."
    missing = REQUIRED_RULE_KEYS - set(rule.keys())
    if missing:
        return False, f"Missing keys: {sorted(missing)}"
    if rule.get("status") not in VALID_STATUSES:
        return False, f"Invalid status '{rule.get('status')}'. Must be one of: {sorted(VALID_STATUSES)}"
    if not str(rule.get("summary", "")).strip():
        return False, "Empty summary."
    if not str(rule.get("criterion_id", "")).strip():
        return False, "Empty criterion_id."
    return True, ""


# ---------------------------------------------------------------------------
# Load / Save
# ---------------------------------------------------------------------------

def load_rulebook(file_obj) -> tuple[list, str]:
    """Parse a rulebook from a file-like (.read()), a path, a JSON string, or bytes.

    Validates each rule; invalid rules are skipped (not fatal). Never raises.

    Returns:
        (valid_rules, warning_message) — valid_rules is [] on failure.
    """
    if file_obj is None:
        return [], ""

    try:
        # file-like with .read()
        if hasattr(file_obj, "read"):
            raw = file_obj.read()
        elif isinstance(file_obj, (bytes, bytearray)):
            raw = file_obj
        elif isinstance(file_obj, str):
            # Could be a JSON string OR a path to a file on disk.
            stripped = file_obj.strip()
            if stripped[:1] in ("{", "[") or "\n" in file_obj:
                raw = file_obj
            else:
                try:
                    with open(file_obj, "r", encoding="utf-8") as fh:
                        raw = fh.read()
                except (OSError, ValueError):
                    raw = file_obj  # treat as raw JSON string
        else:
            raw = file_obj

        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")

        data = json.loads(raw)
    except Exception as e:
        return [], f"Could not parse rulebook JSON: {e}"

    # Accept either a bare list or a wrapped object {"rules": [...]}
    if isinstance(data, list):
        rules = data
    elif isinstance(data, dict) and "rules" in data:
        rules = data["rules"]
    else:
        return [], "Rulebook must be a JSON array or an object with a 'rules' key."

    valid_rules = []
    warnings = []
    for i, rule in enumerate(rules):
        ok, err = validate_rule(rule)
        if ok:
            valid_rules.append(rule)
        else:
            warnings.append(f"Rule #{i + 1} skipped: {err}")

    return valid_rules, ("; ".join(warnings) if warnings else "")


def save_rulebook(rules: list) -> str:
    """Serialize rules to a pretty JSON string for download."""
    return json.dumps({"rules": rules}, indent=2)


# ---------------------------------------------------------------------------
# Adjudication Gateway (LLM guard)
# ---------------------------------------------------------------------------

def adjudicate_rule(rule: dict) -> dict:
    """Run an LLM guard on a single draft rule to detect manipulative or
    illogical reviewer overrides before they enter institutional memory.

    Returns:
        {"approved": bool, "concern": str, "clarifying_question": str}

    On ANY failure, returns a cautious REJECTION requiring manual review — a
    transient API error must never silently approve a rule. Never raises.
    """
    prompt_system = """You are a public-infrastructure program-fit policy analyst reviewing a
proposed rule correction submitted by a Caltrans HQ nomination reviewer.

The human reviewer is OVERRIDING the AI's original score for one Program Fit criterion
(Freight System F1-F3 or Transportation System T1-T7). The 'Summary' field describes the
correction in plain language.

Your job: assess whether the human's correction is a legitimate, evidence-based calibration,
or whether it appears illogical, policy-violating, or an attempt to game the scoring (e.g.,
inflating a nomination's fit regardless of evidence, or contradicting TCEP/SCCP eligibility
criteria).

Respond ONLY with valid JSON:
{
  "approved": true or false,
  "concern": "Brief explanation if suspicious. Empty string if approved cleanly.",
  "clarifying_question": "A single clarifying question for the reviewer if suspicious. Empty string if clean."
}"""

    prompt_user = f"""Proposed rule:
Criterion: {rule.get('criterion_id', 'UNKNOWN')}
Summary: {rule.get('summary', '')}
Source evidence: {rule.get('source_evidence', '')}
Reviewer rationale: {rule.get('user_rationale', '')}

Assess whether this is a valid, policy-grounded calibration."""

    try:
        client = _get_client("gpt-4o")
        response = client.chat.completions.create(
            model="gpt-4o",
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": prompt_system},
                {"role": "user", "content": prompt_user},
            ],
        )
        result = _extract_json(response.choices[0].message.content)
        return {
            "approved": bool(result.get("approved", False)),
            "concern": result.get("concern", ""),
            "clarifying_question": result.get("clarifying_question", ""),
        }
    except Exception as e:
        logger.warning("Adjudicator failed for rule %s: %s", rule.get("rule_id"), e)
        return {
            "approved": False,
            "concern": "Adjudicator unavailable — manual review required.",
            "clarifying_question": "",
        }


def adjudicate_defense(rule: dict, defense_text: str) -> dict:
    """Second-round adjudication after the reviewer defends a flagged rule.

    Returns the same shape as adjudicate_rule(). Never raises; on any failure
    returns a cautious rejection requiring manual review.
    """
    prompt_system = """You are a public-infrastructure program-fit policy analyst re-evaluating a
flagged Program Fit rule correction. The Caltrans HQ reviewer has provided additional
justification. Assess whether this defense adequately explains why the correction is
policy-grounded and specific to the nomination's evidence (not an attempt to game scoring).

Respond ONLY with valid JSON:
{
  "approved": true or false,
  "concern": "Brief explanation if still rejected. Empty string if now approved.",
  "clarifying_question": "A single clarifying question if still unsure. Empty string otherwise."
}"""

    prompt_user = f"""Original rule:
Criterion: {rule.get('criterion_id', 'UNKNOWN')}
Summary: {rule.get('summary', '')}
Source evidence: {rule.get('source_evidence', '')}
Reviewer rationale: {rule.get('user_rationale', '')}

Reviewer defense: {defense_text}

Is this defense sufficient to approve the rule?"""

    try:
        client = _get_client("gpt-4o")
        response = client.chat.completions.create(
            model="gpt-4o",
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": prompt_system},
                {"role": "user", "content": prompt_user},
            ],
        )
        result = _extract_json(response.choices[0].message.content)
        return {
            "approved": bool(result.get("approved", False)),
            "concern": result.get("concern", ""),
            "clarifying_question": result.get("clarifying_question", ""),
        }
    except Exception as e:
        logger.warning("Defense adjudicator failed: %s", e)
        return {
            "approved": False,
            "concern": "Adjudicator unavailable — manual review required.",
            "clarifying_question": "",
        }


# ---------------------------------------------------------------------------
# Synthesis (Memory Compression)
# ---------------------------------------------------------------------------

def synthesize_rulebook(existing_rules: list, new_approved_rules: list) -> tuple[list, str]:
    """Merge existing + newly approved rules into the institutional rulebook.

    Pure-Python (no LLM required):
      - Keeps only status == "approved".
      - Dedupes by (criterion_id, summary), preferring the most recent occurrence.
      - Caps the result at MAX_APPROVED_RULES, keeping the most recently added.

    Returns:
        (rules, synthesis_note)
    """
    existing_rules = existing_rules or []
    new_approved_rules = new_approved_rules or []

    # Oldest first, newest last — later duplicates win, and the tail is "most recent".
    combined = [r for r in (existing_rules + new_approved_rules)
                if isinstance(r, dict) and r.get("status") == "approved"]

    deduped = {}
    for r in combined:
        key = (str(r.get("criterion_id", "")), str(r.get("summary", "")))
        # Re-seeing a rule must move it to the END (most-recent position), not just overwrite in
        # place — otherwise the cap below could drop a just-re-affirmed rule as if it were oldest.
        if key in deduped:
            del deduped[key]
        deduped[key] = r

    merged = list(deduped.values())

    note = f"Merged into {len(merged)} approved rule(s)."
    if len(merged) > MAX_APPROVED_RULES:
        dropped = len(merged) - MAX_APPROVED_RULES
        merged = merged[-MAX_APPROVED_RULES:]  # keep most recent
        note = f"Capped rulebook at {MAX_APPROVED_RULES} rules (dropped {dropped} oldest)."

    return merged, note


# ---------------------------------------------------------------------------
# Prompt Injection Helper
# ---------------------------------------------------------------------------

def build_institutional_memory_block(rules: list) -> str:
    """Render approved rules into a prompt-injectable institutional-memory block.

    Returns "" if there are no approved rules.
    """
    approved = [r for r in (rules or [])
                if isinstance(r, dict) and r.get("status") == "approved"]
    if not approved:
        return ""

    lines = [
        "INSTITUTIONAL MEMORY (prior reviewer calibrations):",
        "The following calibrations were approved by past Caltrans HQ reviewers. Apply them "
        "as strong prior knowledge when scoring Program Fit criteria. They resolve ambiguity "
        "but do not override direct evidence from the nomination.",
        "",
    ]
    for r in approved:
        cid = r.get("criterion_id", "?")
        summary = r.get("summary", "")
        rationale = r.get("user_rationale", "")
        lines.append(f"  [{cid}] {summary}")
        if rationale:
            lines.append(f"         Rationale: {rationale}")
    return "\n".join(lines)
