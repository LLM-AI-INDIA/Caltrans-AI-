"""
Streamlit UI for the Program Fit Evaluator use case.

Kept in its own module (mirroring the ROW module's render_landing_ai_evaluation_ui pattern) so the
wiring in app.py stays to 3 additive lines — an import, a selectbox entry, and an elif branch —
and nothing about the other use cases changes.

Two review perspectives share the same evaluation:
  * District  — the original single-flow: evaluate -> rating/eligibility/contradictions -> Excel.
  * Headquarters (HQ) — a 3-step Human-in-the-Feedback-Loop (HIFL) wizard that lets a reviewer
    override AI scores, adjudicate the corrections into calibration rules, and export both the
    reviewed Excel report and the synthesized institutional-memory rulebook.

The module is import-safe: every Streamlit call lives inside render_program_fit_ui, and all
session_state keys are pf_-prefixed so nothing collides with the other use cases.
"""
import streamlit as st

from src.program_fit_evaluator import (
    extract_package,
    run_program_fit_evaluation,
    score_all_factors,
    compute_program_fit_rating,
    screen_eligibility,
    detect_contradictions,
    build_program_fit_excel,
    RUBRIC_TCEP,
)
from src.program_fit_memory_manager import (
    make_draft_rule,
    adjudicate_rule,
    synthesize_rulebook,
    load_rulebook,
    save_rulebook,
)

_SEVERITY_ICON = {"high": "🔴", "medium": "🟡", "low": "🟣"}

_WIZARD_LABELS = [
    "1 · Review & Override",
    "2 · Validation & Adjudication",
    "3 · Export",
]


def _reset_pf_state():
    for key in [k for k in list(st.session_state.keys()) if k.startswith("pf_")]:
        del st.session_state[key]


def _crit_lookup(eval_result):
    """Map criterion_id -> the raw criterion dict returned by the evaluator."""
    lookup = {}
    for crit in (eval_result.get("criteria") or []):
        if not isinstance(crit, dict):
            continue
        cid = crit.get("criterion_id") or crit.get("id")
        if cid:
            lookup[cid] = crit
    return lookup


def _reviewed_scores(ai_scores):
    """Build a {criterion_id: score} dict from staged overrides, falling back to the AI score."""
    overrides = st.session_state.get("pf_overrides", {}) or {}
    scores = {}
    for cid, ai in ai_scores.items():
        ov = overrides.get(cid) or {}
        try:
            scores[cid] = int(ov.get("score", ai))
        except (TypeError, ValueError):
            scores[cid] = int(ai)
    return scores


def _patched_evaluation(eval_result, reviewed):
    """Return a shallow copy of eval_result whose criteria carry the reviewed scores."""
    patched = dict(eval_result)
    new_criteria = []
    for crit in (eval_result.get("criteria") or []):
        if isinstance(crit, dict):
            crit = dict(crit)
            cid = crit.get("criterion_id") or crit.get("id")
            if cid in reviewed:
                crit["score"] = reviewed[cid]
        new_criteria.append(crit)
    patched["criteria"] = new_criteria
    return patched


def _step_indicator(active_step):
    cols = st.columns(len(_WIZARD_LABELS))
    for i, lbl in enumerate(_WIZARD_LABELS):
        step_num = i + 1
        is_active = (active_step == step_num)
        is_done = (active_step > step_num)
        disp = ("✓ " + lbl) if is_done else lbl
        border = "#1F4E79" if is_active else ("#16a34a" if is_done else "#e2e8f0")
        color = "#1F4E79" if is_active else ("#15803d" if is_done else "#94a3b8")
        weight = "700" if is_active else "500"
        cols[i].markdown(
            f"<div style='text-align:center; padding:6px 0; "
            f"border-bottom:3px solid {border}; color:{color}; "
            f"font-weight:{weight}; font-size:0.85rem;'>{disp}</div>",
            unsafe_allow_html=True,
        )
    st.write("")


def render_program_fit_ui(pf_files):
    """Render the full Program Fit evaluation flow for an uploaded nomination package.

    Args:
        pf_files: list of Streamlit UploadedFile objects (or None) from the app.py file_uploader.
    """
    if not pf_files:
        st.markdown(
            """
            <div style="background:#ffffff;border:1.5px solid #bcd4f0;border-radius:14px;
                        padding:28px 32px;margin:20px 0;box-shadow:0 2px 8px rgba(0,0,0,0.06);">
                <h3 style="margin:0 0 14px 0;color:#1F4E79;">SB1 Program Fit Evaluator (TCEP / SCCP)</h3>
                <p style="color:#334155;margin:0 0 16px 0;">
                    Upload a nomination <strong>package</strong> and this tool runs the PRC's first-pass:
                    it scores the 10-criteria rubric with citations and confidence, screens eligibility and
                    cycle deadlines, and flags contradictions — then exports a reviewable Excel report.
                </p>
                <ol style="color:#475569;margin:0;padding-left:20px;line-height:1.8;">
                    <li><strong>Upload</strong> the Program Fit workbook (.xlsx), the Cal-B/C model (.xlsm),
                        and any traffic / performance-metric docs (.pdf, .docx).</li>
                    <li><strong>Evaluate</strong> to score all 10 rubric criteria.</li>
                    <li><strong>Review</strong> the rating, eligibility, contradictions, and evidence.</li>
                    <li><strong>Download</strong> the Excel evaluation package.</li>
                </ol>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    # Reset cached state when the uploaded file set changes.
    file_names = sorted([f.name for f in pf_files])
    if st.session_state.get("pf_current_files") != file_names:
        _reset_pf_state()
        st.session_state.pf_current_files = file_names

    st.success(f"Loaded {len(pf_files)} file(s): **{', '.join(file_names)}**")

    # ---- Controls: program, perspective, optional institutional-memory rulebook ----
    program = st.radio("Program", ["TCEP", "SCCP"], horizontal=True, key="pf_program")
    role = st.radio("View Perspective", ["District", "Headquarters (HQ)"],
                    horizontal=True, key="pf_role")

    st.session_state.setdefault("pf_rules", [])
    with st.expander("Institutional Memory (optional rulebook)", expanded=False):
        st.caption(
            "Upload a previously exported rulebook (.json) to inject prior HQ calibrations into "
            "this evaluation. Optional — leave empty to score without prior memory."
        )
        rb_upload = st.file_uploader("Rulebook (.json)", type=["json"], key="pf_rulebook_upload")
        if rb_upload is not None:
            try:
                loaded, warn = load_rulebook(rb_upload)
            except Exception as e:  # noqa: BLE001 — a bad file must never break the page
                loaded, warn = [], f"Could not read rulebook: {e}"
            st.session_state.pf_rules = loaded or []
            if warn:
                st.warning(warn)
            st.info(f"{len(st.session_state.pf_rules)} approved rule(s) loaded into memory.")
        elif st.session_state.get("pf_rules"):
            st.info(f"{len(st.session_state.pf_rules)} rule(s) currently in memory.")

    # ---- Evaluation (shared by both perspectives) ----
    if "pf_eval_result" not in st.session_state:
        if st.button("Evaluate Program Fit", type="primary", key="pf_run"):
            with st.spinner("Extracting package and scoring the rubric… this may take a couple minutes."):
                package = extract_package(pf_files)
                st.session_state.pf_package = package
                eval_result = run_program_fit_evaluation(
                    package,
                    program=program,
                    pf_rules=st.session_state.get("pf_rules", []),
                )
            if "error" in eval_result:
                st.error(f"Evaluation failed: {eval_result['error']}")
            else:
                st.session_state.pf_eval_result = eval_result
                st.rerun()
        return

    # ---- Shared derived data ----
    package = st.session_state.get("pf_package", {})
    eval_result = st.session_state.pf_eval_result

    factors = score_all_factors(eval_result)
    if factors.get("count", 0) == 0:
        st.error("The evaluator did not return any scored criteria. Please re-run the evaluation.")
        st.session_state.pop("pf_eval_result", None)
        return

    ai_scores = factors.get("scores", {}) or {}
    project_name = eval_result.get("project_name") or "Program Fit Evaluation"

    if role == "Headquarters (HQ)":
        _render_hq_wizard(eval_result, ai_scores, project_name)
    else:
        _render_district(eval_result, package, factors, project_name)


# =============================================================================
# DISTRICT perspective — the original single-flow view
# =============================================================================
def _render_district(eval_result, package, factors, project_name):
    rating = compute_program_fit_rating(factors["scores"])
    if factors.get("missing"):
        st.info(
            f"{len(factors['missing'])} criterion/criteria lacked usable evidence and were "
            "scored 2 (insufficient information)."
        )
    eligibility = screen_eligibility(package)
    contradictions = detect_contradictions(package)

    # Headline rating
    c1, c2, c3 = st.columns(3)
    c1.metric("Program Fit Rating", rating.get("rating", "—"))
    c2.metric("Average Score (1–5)", f"{rating.get('average', 0):.2f}")
    c3.metric("Eligibility", "Eligible" if eligibility.get("eligible") else "Review")

    # Eligibility / risk
    with st.expander("Eligibility & Risk", expanded=True):
        st.write(f"**Funding risk:** {eligibility.get('funding_risk') or '—'}")
        st.write(f"**Schedule risk:** {eligibility.get('schedule_risk') or '—'}")
        for f in eligibility.get("failures", []):
            st.error(f"Ineligible — {f.get('rule')}: {f.get('detail')}")
        for w in eligibility.get("warnings", []):
            st.warning(w)

    # Contradictions
    with st.expander(f"Contradictions & Data-Quality Flags ({len(contradictions)})",
                     expanded=bool(contradictions)):
        if not contradictions:
            st.info("None detected.")
        for c in contradictions:
            icon = _SEVERITY_ICON.get(c.get("severity", ""), "•")
            st.write(f"{icon} **{c.get('field')}** — {c.get('detail')}")

    # Criteria detail
    with st.expander("Criteria Detail (10)", expanded=False):
        _id_to_name = {c["id"]: c["name"] for c in RUBRIC_TCEP}
        rows = []
        for crit in eval_result.get("criteria", []):
            cid = crit.get("criterion_id") or crit.get("id", "")
            rows.append({
                "Criterion": crit.get("name") or _id_to_name.get(cid, cid),
                "Score": crit.get("score"),
                "Confidence": crit.get("confidence"),
                "Missing Info": crit.get("missing_info"),
                "Evidence": (crit.get("source_reasoning") or "")[:300],
            })
        if rows:
            st.dataframe(rows, use_container_width=True)

    if eval_result.get("summary"):
        st.markdown(f"**Summary:** {eval_result['summary']}")

    # Excel download
    try:
        buf = build_program_fit_excel(eval_result, rating, eligibility, contradictions, project_name)
        st.download_button(
            "Download Excel Evaluation",
            data=buf.getvalue() if hasattr(buf, "getvalue") else buf,
            file_name=f"program_fit_{project_name[:30].replace(' ', '_')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="pf_download",
        )
    except Exception as e:  # never let a report-render error break the results view
        st.warning(f"Excel report could not be generated: {e}")


# =============================================================================
# HQ perspective — 3-step HIFL review wizard
# =============================================================================
def _render_hq_wizard(eval_result, ai_scores, project_name):
    st.session_state.setdefault("pf_wizard_step", 1)
    st.session_state.setdefault("pf_overrides", {})
    st.session_state.setdefault("pf_approved_rules", [])
    step = st.session_state.pf_wizard_step

    _step_indicator(step)

    id_to_meta = {c["id"]: c for c in RUBRIC_TCEP}
    crit_lookup = _crit_lookup(eval_result)

    if step == 1:
        _hq_step1(eval_result, ai_scores, id_to_meta, crit_lookup)
    elif step == 2:
        _hq_step2(eval_result, ai_scores, id_to_meta, crit_lookup)
    else:
        _hq_step3(eval_result, ai_scores, project_name)


def _hq_step1(eval_result, ai_scores, id_to_meta, crit_lookup):
    st.subheader("Step 1 — Review & Override")
    st.caption(
        "Review the AI score for each of the 10 criteria. Adjust any score (1–5) and add a "
        "rationale. For criteria you change, tick 'Draft calibration rule' to send the correction "
        "for adjudication in Step 2."
    )

    for cid in [c["id"] for c in RUBRIC_TCEP]:
        meta = id_to_meta.get(cid, {})
        crit = crit_lookup.get(cid, {})
        ai_score = int(ai_scores.get(cid, 2))
        name = crit.get("name") or meta.get("name", cid)
        group = meta.get("group", "")
        confidence = crit.get("confidence")
        evidence = (crit.get("source_reasoning") or "")

        with st.container(border=True):
            hc1, hc2 = st.columns([8, 2])
            with hc1:
                st.markdown(f"**{cid} · {name}**  \n<span style='color:#64748b;font-size:0.8rem;'>"
                            f"{group}</span>", unsafe_allow_html=True)
                if evidence:
                    with st.expander(
                        "View AI evidence"
                        + (f" (Confidence: {confidence:.0%})" if isinstance(confidence, (int, float)) else ""),
                        expanded=False,
                    ):
                        st.caption(evidence[:600])
            with hc2:
                st.markdown(
                    f"<div style='text-align:center;background:#eff6ff;color:#1e40af;"
                    f"border-radius:6px;padding:6px 0;font-weight:700;'>AI: {ai_score}</div>",
                    unsafe_allow_html=True,
                )

            score_val = st.slider(
                "Reviewer score (1–5)", min_value=1, max_value=5, value=ai_score, step=1,
                key=f"pf_ovr_score_{cid}",
            )
            st.text_area(
                "Rationale (optional)", value="",
                placeholder="Why does this score need adjusting? (recorded with any calibration rule)",
                key=f"pf_ovr_rat_{cid}", height=68,
            )
            if score_val != ai_score:
                st.checkbox("Draft calibration rule from this change", key=f"pf_ovr_rule_{cid}")

    _n1, _n2 = st.columns([1, 4])
    with _n1:
        if st.button("Next → Validation", type="primary", key="pf_to2"):
            # Snapshot the widget state into pf_overrides so later steps read a stable copy.
            overrides = {}
            for cid in [c["id"] for c in RUBRIC_TCEP]:
                ai = int(ai_scores.get(cid, 2))
                try:
                    sc = int(st.session_state.get(f"pf_ovr_score_{cid}", ai))
                except (TypeError, ValueError):
                    sc = ai
                rat = (st.session_state.get(f"pf_ovr_rat_{cid}") or "").strip()
                flagged = bool(st.session_state.get(f"pf_ovr_rule_{cid}", False)) and (sc != ai)
                overrides[cid] = {"score": sc, "rationale": rat, "draft_rule": flagged}
            st.session_state.pf_overrides = overrides
            # Adjudication depends on the staged overrides — clear any stale run.
            st.session_state.pop("pf_adjudications", None)
            st.session_state.pop("pf_drafts", None)
            st.session_state.pf_wizard_step = 2
            st.rerun()


def _hq_step2(eval_result, ai_scores, id_to_meta, crit_lookup):
    st.subheader("Step 2 — Validation & Adjudication")
    overrides = st.session_state.get("pf_overrides", {}) or {}

    # ---- Raw-vs-reviewed comparison table ----
    rows = []
    for cid in [c["id"] for c in RUBRIC_TCEP]:
        meta = id_to_meta.get(cid, {})
        ov = overrides.get(cid) or {}
        ai = int(ai_scores.get(cid, 2))
        rev = int(ov.get("score", ai))
        rows.append({
            "Criterion": f"{cid} · {crit_lookup.get(cid, {}).get('name') or meta.get('name', cid)}",
            "AI Score": ai,
            "Reviewer Score": rev,
            "Δ": ("=" if rev == ai else f"{'+' if rev > ai else ''}{rev - ai}"),
        })
    st.markdown("**Raw vs. Reviewed Scores**")
    st.dataframe(rows, use_container_width=True)

    # ---- Recomputed rating from the overridden scores ----
    reviewed = _reviewed_scores(ai_scores)
    ai_rating = compute_program_fit_rating(ai_scores)
    rev_rating = compute_program_fit_rating(reviewed)
    m1, m2, m3 = st.columns(3)
    m1.metric("AI Rating", ai_rating.get("rating", "—"), f"avg {ai_rating.get('average', 0):.2f}")
    m2.metric("Reviewed Rating", rev_rating.get("rating", "—"),
              f"avg {rev_rating.get('average', 0):.2f}")
    m3.metric("Criteria Changed", sum(1 for cid in ai_scores if reviewed.get(cid) != ai_scores.get(cid)))

    # ---- Adjudicate flagged corrections ----
    flagged = [cid for cid, ov in overrides.items() if ov.get("draft_rule")]
    st.markdown("---")
    st.markdown("**Calibration Rule Adjudication**")

    if not flagged:
        st.info("No corrections were flagged for calibration rules. You may proceed to Export.")
    else:
        if "pf_adjudications" not in st.session_state:
            if st.button("Run Adjudication", type="primary", key="pf_run_adj"):
                drafts, verdicts = {}, {}
                with st.spinner("Adjudicating flagged corrections…"):
                    for cid in flagged:
                        ov = overrides.get(cid) or {}
                        meta = id_to_meta.get(cid, {})
                        name = crit_lookup.get(cid, {}).get("name") or meta.get("name", cid)
                        ai = int(ai_scores.get(cid, 2))
                        rev = int(ov.get("score", ai))
                        rationale = ov.get("rationale") or ""
                        summary = rationale or f"{name} score changed from {ai} to {rev}"
                        source_ev = (crit_lookup.get(cid, {}).get("source_reasoning") or "")[:400] \
                            or "Not available"
                        try:
                            draft = make_draft_rule(
                                criterion_id=cid,
                                summary=summary,
                                source_evidence=source_ev,
                                user_rationale=rationale or summary,
                            )
                            verdict = adjudicate_rule(draft)
                        except Exception as e:  # noqa: BLE001 — never crash the wizard
                            draft = {
                                "criterion_id": cid, "summary": summary,
                                "source_evidence": source_ev, "user_rationale": rationale,
                                "status": "draft", "version": 1, "rule_id": f"pf-{cid}-error",
                            }
                            verdict = {"approved": False,
                                       "concern": f"Adjudicator error: {e}",
                                       "clarifying_question": ""}
                        drafts[cid] = draft
                        verdicts[cid] = verdict
                st.session_state.pf_drafts = drafts
                st.session_state.pf_adjudications = verdicts
                st.rerun()
            st.caption(f"{len(flagged)} correction(s) staged for adjudication.")
        else:
            verdicts = st.session_state.get("pf_adjudications", {}) or {}
            drafts = st.session_state.get("pf_drafts", {}) or {}
            for cid in flagged:
                verdict = verdicts.get(cid, {})
                draft = drafts.get(cid, {})
                approved = bool(verdict.get("approved"))
                meta = id_to_meta.get(cid, {})
                name = crit_lookup.get(cid, {}).get("name") or meta.get("name", cid)
                with st.container(border=True):
                    hc1, hc2 = st.columns([7, 3])
                    with hc1:
                        st.markdown(f"**{cid} · {name}**")
                        st.caption(draft.get("summary", ""))
                    with hc2:
                        if approved:
                            st.success("Adjudicator: APPROVED")
                        else:
                            st.error("Adjudicator: CONCERN")
                    if verdict.get("concern"):
                        st.warning(f"Concern: {verdict['concern']}")
                    if verdict.get("clarifying_question"):
                        st.info(f"Clarifying question: {verdict['clarifying_question']}")
                    st.checkbox(
                        "Approve — add this rule to the rulebook",
                        value=approved, key=f"pf_approve_{cid}",
                    )

    _b1, _b2, _b3 = st.columns([1, 1, 3])
    with _b1:
        if st.button("← Back", key="pf_back1"):
            st.session_state.pf_wizard_step = 1
            st.rerun()
    with _b2:
        if st.button("Next → Export", type="primary", key="pf_to3"):
            approved_rules = []
            drafts = st.session_state.get("pf_drafts", {}) or {}
            for cid in flagged:
                if st.session_state.get(f"pf_approve_{cid}", False):
                    rule = dict(drafts.get(cid, {}))
                    if rule:
                        rule["status"] = "approved"
                        approved_rules.append(rule)
            st.session_state.pf_approved_rules = approved_rules
            st.session_state.pf_wizard_step = 3
            st.rerun()


def _hq_step3(eval_result, ai_scores, project_name):
    st.subheader("Step 3 — Export")

    reviewed = _reviewed_scores(ai_scores)
    rev_rating = compute_program_fit_rating(reviewed)

    approved_rules = st.session_state.get("pf_approved_rules", []) or []
    existing = st.session_state.get("pf_rules", []) or []
    try:
        merged, note = synthesize_rulebook(existing, approved_rules)
    except Exception as e:  # noqa: BLE001 — synthesis must never crash export
        merged, note = existing, f"Rulebook synthesis failed: {e}"

    # Grow institutional memory so subsequent evaluations pick up these calibrations.
    st.session_state.pf_rules = merged

    c1, c2 = st.columns(2)
    c1.metric("Reviewed Rating", rev_rating.get("rating", "—"),
              f"avg {rev_rating.get('average', 0):.2f}")
    c2.metric("Approved Rules Added", len(approved_rules))

    if note:
        st.info(f"📚 {note}")

    if merged:
        with st.expander(f"Institutional Rulebook ({len(merged)} rule(s))", expanded=False):
            for r in merged:
                st.markdown(
                    f"- **[{r.get('criterion_id', '?')}]** {r.get('summary', '')}"
                    + (f" · *{r.get('user_rationale', '')[:100]}*" if r.get("user_rationale") else "")
                )

    # ---- Rulebook JSON download ----
    try:
        rb_json = save_rulebook(merged)
        st.download_button(
            "Download Rulebook (.json)",
            data=rb_json,
            file_name=f"program_fit_rulebook_{project_name[:30].replace(' ', '_')}.json",
            mime="application/json",
            key="pf_rulebook_download",
        )
    except Exception as e:  # noqa: BLE001
        st.warning(f"Rulebook could not be serialized: {e}")

    # ---- Reviewed Excel report download ----
    try:
        patched = _patched_evaluation(eval_result, reviewed)
        eligibility = screen_eligibility(st.session_state.get("pf_package", {}))
        contradictions = detect_contradictions(st.session_state.get("pf_package", {}))
        buf = build_program_fit_excel(patched, rev_rating, eligibility, contradictions, project_name)
        st.download_button(
            "Download Reviewed Excel Evaluation",
            data=buf.getvalue() if hasattr(buf, "getvalue") else buf,
            file_name=f"program_fit_{project_name[:30].replace(' ', '_')}_reviewed.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="pf_reviewed_download",
        )
    except Exception as e:  # noqa: BLE001 — never let a report error break export
        st.warning(f"Excel report could not be generated: {e}")

    if st.button("← Back", key="pf_back2"):
        st.session_state.pf_wizard_step = 2
        st.rerun()
