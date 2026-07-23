# Program Fit Evaluator — Source-of-Truth Spec

> Consolidated reference for the **AI-Assisted TCEP/SCCP Program Fit Evaluation** effort (PMP-facing).
> Built from an exhaustive read of every sheet, formula, and page of the source files provided 2026-07-10.
> This is the single doc to hand business and to build `src/program_fit_evaluator.py` from.

---

## 0. TL;DR — what this is

Caltrans runs two SB1 competitive grant programs — **TCEP** (Trade Corridor Enhancement Program, ~$300M/yr) and **SCCP** (Solutions for Congested Corridors Program, ~$250M/yr). Districts submit a nomination **package** (a standardized Excel "Program Fit" workbook + traffic studies + funding plan + schedule + a Cal-B/C benefit-cost model + performance-metric backup docs). A human committee (the **PRC**, Program Review Committee) reads each package and hand-scores it on a rubric to recommend program fit (HIGH / MEDIUM-HIGH / MEDIUM / MEDIUM-LOW / LOW).

**The ask:** an AI tool that does the PRC's first-pass job — ingest a package, extract data, score every rubric factor with a **citation + confidence**, flag missing info / contradictions / vague language / (in)eligibility / cycle-deadline failures, and produce a reviewable evaluation package. **PMP-facing only** (internal analyst tool); no district-facing component in v1.

**This is architecturally the existing PDE (Project Delivery Evaluator) re-skinned:** score documents against a rubric with confidence + citations, human-in-the-loop review, Excel/Markdown export. ~60–70% of the capability matrix is already-built PDE pattern.

---

## 1. Source corpus (everything read)

| File | Type | Sheets/Pages | Notes |
|---|---|---|---|
| `SB1 Cycle 5 Program Fit 09_29_2025_FINAL.xlsx` | Blank master template | 13 sheets, 1,651 formulas | The submission form districts fill |
| `TCEP Program Fit Scoring Sheet - PRC Results.xlsx` | Human scoring results | 4 sheets, 552 formulas | ~32 scored projects + rubric anchors |
| `SB1-Cycle 5 Risk Ratings - Funding Plan  Schedule - 03.16.26.xlsx` | Risk rubric | 2 sheets | Funding + schedule risk rules, ineligibility list |
| `AI Assisted Program Fit Evaluation — Capability Matrix (1).xlsx` | Draft requirements | 1 sheet | 12 capability groups (A–L) |
| `SB1 TCEP Nom_D4 San Pablo Dam.zip` | Sample package | — | See below |
| ↳ `80 SPDR Ph2 ... Program Fit ...xlsx` | Filled submission | 13 sheets | I-80/SPDR Ph2, District 4 |
| ↳ `80 SPDR Cal BCA model.xlsm` | Benefit-cost | **Cal-B/C Sketch v8.1**, 12 sheets, 17,095 formulas | B/C=6.71 |
| ↳ `Traffic_Operations_Report_Oct08.pdf` | Traffic study | 80 pp | Text extracts clean |
| ↳ `..._Oct08 Appendices.pdf` | Traffic appendix | 234 pp | **Scanned images, 0 text → OCR required** |
| ↳ `EA 04-0A0821 ... Traffic Analysis Memo ...pdf` | Traffic memo | 70 pp | Text extracts clean |
| `SB1 TCEP Nom_D10 SR99_120.zip` | Sample package | — | See below |
| ↳ `SB1 Cycle 5 Program Fit ... SR120-99 Phase 1B ...xlsx` | Filled submission | 13 sheets | SR99/120 Ph1B, District 10 |
| ↳ `Cal BC_8-1_SJCOG SR99-120 ...xlsm` | Benefit-cost | **Cal-B/C Corridor v8.1**, 18 sheets, 10,403 formulas | B/C=1.98 |
| ↳ `SR 99 SR120 Performance Metrics ...docx` | Metric backup | — | Metric → BCA-cell traceability |
| ↳ `SR 99-120 ITIP Performance Metrics ...docx` | Metric backup | — | VMT/safety metric derivations |

Extracted text dumps live in: `<scratchpad>/pf_dump/*.txt` (01–08 workbooks, D4/D10 docs).

---

## 2. Submission package = the AI's input

A district uploads a **package**, not one file. Composition:

1. **Program Fit workbook** (the 13-sheet Excel) — *primary structured source of truth*.
2. **Cal-B/C benefit-cost model** (`.xlsm`) — the quantitative engine; outputs (B/C, NPV, emissions) feed several metrics.
3. **Performance Measures Background Documentation** (Word/PDF) — shows how each submitted metric was derived (traces to BCA cells). *Required attachment.*
4. **Traffic Operations Analysis Report (TOAR)** or equivalent (PDF) — *required if available*; source for LOS/delay/speed/throughput.
5. **Project Factsheet / supporting docs** — *optional*.

Required vs optional and the exact document-type list are **configurable** per the requirements (programs evolve).

---

## 3. The Program Fit template (13 sheets)

Tab colors: **Orange** = main Program Fit form (required); **Green** = Project Funding Info #1 (required), #2–5 (optional, for bundled projects); **Blue** = Intent to Nominate (Round-1 form; only resubmit in Round 2 if not already sent).

### 3.1 Sheet inventory
1. **Form Instructions Program Fit** — field-by-field instructions (incl. embedded eligibility rules).
2. **SB1 Cycle 5 Program Fit** — THE orange form. Data model per row: `A`=Form ID (formula), `B`=Line ID, `C`=question text, `D`=answer; cols `G–M`=performance-measure data tables.
3–9. **Project Funding Info #1–5** — PPR (Project Programming Request) tabs: identity, milestone schedule, and 8 fund sub-tables (component × fiscal year, all $1,000s) that aggregate to Total Project Cost.
4. **Pull Down Data** — fund-code/program-category lookup tables.
5. **Appendix A - PROJECT A** — optional per-project funding/schedule breakdown for bundled nominations.
10–11. **Intent to Nominate** (+ instructions) — Round-1 form; has its OWN performance-measure tables (#28 TCEP, #31 SCCP) with *different* metrics than the Program Fit form.
12. **Drop Down Lists** — every picklist (see 3.5).
13. **Definitions** — 23-acronym glossary (CAPTI, CFMP, CRFC/CUFC, NHFP, PHFS, PA&ED, etc.).

### 3.2 Form sections (orange sheet)
- **I. General Info** — Project name (drives Form ID), District (pulled from Intent tab via `='SB1 Cycle 5 Intent to Nominate'!D24`), Intent-submitted flag.
- **II. Description** — 4a Project Description (≤3 sentences, auto-counted), 4b Outputs (≤5 sentences).
- **III. Schedule** — Current phase (pulled from Intent `!D36`), Target PA&ED complete, environmental doc type, Draft EIR, Target RTL, Target Begin Construction. *Eligibility rules embedded here — see §5.*
- **IV. Funding Plan** — SCCP request (Q10), TCEP request `D45=SUM(D46,D48)` = regional (11a) + state (11b); 12a–12d cost-overrun / uncommitted funding.
- **V. TCEP Evaluation** — 13a factor selection (up to 10 picks), 13b narrative (≤1,200 words, auto-counted), performance-measure data tables #14–18 + #20.
- **VI. SCCP Evaluation** — 19a–19k narrative questions + #20 air-quality table.
- **VII. Contact** / **VIII. Attachments & Document Controls**.

### 3.3 TCEP performance-measure tables (Section V, cols G–M)
Every metric block has rows **Build / FNB (Future No-Build) / Change / Incr-Decr** against columns **Measure | Metric | Project Type | Build | Future No Build | Change | Increase/Decrease**. Data-entry cells fall back to `"Fill in using data table #NN"` when blank.

- **#14 Congestion Reduction (Freight):** Person Hours of Travel Time Saved (All); Daily Truck Trips Due to Mode Shift (Rail, Sea Port); Daily Truck Miles Travelled Due to Mode Shift (Rail, Sea Port).
- **#15 Throughput (Freight):** Change in Cargo Volume (Sea port & airport / All).
- **#16 System Reliability (Freight):** Truck Travel Time Reliability Index (TTRI) — No-Build only (National & State Highway System only).
- **#17 Velocity (Freight):** Change in Avg Peak Wkdy Speed — Road; Avg Peak Wkdy Speed — Rail.
- **#18 Safety (Road and Land Port):** Number of Fatalities; Rate of Fatalities/100M VMT; Number of Serious Injuries; Serious Injuries/100M VMT; Non-Motorized Fatalities & Serious Injuries.

### 3.4 SCCP section (Section VI) + shared air-quality table
- **19a–19k** narrative: existing/no-build community & environmental impacts, corridor improvements, CMCP deficiencies, congestion reduction, multimodal, most-beneficial justification, **19g leveraging amount** + 19h sources, 19i existing AADT, 19j Year-20 AADT, 19k AQ/GHG.
- **#20 AQ & GHG (applies to BOTH programs), Project Type = All:** PM10, PM2.5, CO2, VOC, SOx, CO, NOx — Build/FNB/Change/Incr-Decr each.

### 3.5 Key picklists (Drop Down Lists)
- **13a TCEP factor list (11):** Advance Technology, Air Quality, Congestion Reduction/Mitigation, Interregional Benefits, Key Transportation Bottleneck Relief, Multi-Modal Strategy, Reliability, Safety, Throughput, Velocity, ZEV.
- District 1–12 / Rail / Freight / HQ; phases PID/PA&ED/PS&E/ROW; freight-corridor designations (NHFN, CFMP, SB 671 Clean Freight, etc.).
- **⚠ There is NO 1–5 rating list in the template.** The 1–5 ratings are the PRC reviewers' judgments (see §4), never part of what a district submits.

### 3.6 Notable template formulas
- **Form ID:** `="#SB1_FORM_ID_" & UPPER(LEFT(<project name>,10))`.
- **Sentence counter:** `=LEN(D)-LEN(SUBSTITUTE(D,".",""))+...(!,?)` — counts `. ! ?`.
- **Word counter:** `=IF(D="",0,LEN(D)-LEN(SUBSTITUTE(D," ",""))+1)`.
- **TCEP total:** `=SUM(D46,D48)`.
- **PPR funding aggregation:** Total-cost rows sum the same component across all 8 fund tables (`=AA21+AA32+...+AA98`).

---

## 4. The scoring rubric (recovered from PRC Results — build to this)

### 4.1 Structure
- **10 scored criteria, each 1–5 (integer):**
  - **Freight System (3):** 1 Throughput, 2 Velocity, 3 Reliability.
  - **Transportation System (7):** 4 Freight Safety, 5 Congestion Reduction/Mitigation, 6 Key Bottleneck Relief, 7 Multi-Modal Strategy, 8 Interregional Benefits, 9 Advanced Technology, 10 Freight Zero-Emission Infrastructure.
- **11th criterion — "Criteria 11 - Air Quality Impact"** (under group header **"Community Impact Factors"**) exists only in the `Potential Results-feedback loop` tab. ⚠ On that tab the **Consistency Review score (col D) averages all 11 criteria** (AQ included) while the **Potential score (col H) averages 10** (AQ excluded) — so whether AQ counts toward the official rating is genuinely ambiguous in the source and needs the §10 business decision.
- Each criterion = a **score cell + an adjacent justification-narrative cell** (the citation/rationale).
- Criterion questions are verbatim "How does the project…" prompts (full text in PRC sheet row 3).

### 4.2 The 1–5 anchors (legend at `Potential Results-feedback loop`!I48–I53)
- **High (→5/4):** factor is a **primary objective** (not ancillary); clear, **direct, data-driven, significant** benefits; aligns with ≥1 rubric element.
- **Medium (→3):** not a primary purpose, OR benefits don't meet a High requirement; often conceptual/qualitative with no supporting data.
- **Low (→2):** **insufficient information** to assess benefits.
- **Non-responsive (→1):** negatively impacts, or does not address the factor at all.

Empirical distinction from ~30 examples: **5** = primary objective + quantified/data-backed; **4** = strong but secondary/indirect axis; **3** = conceptual/incidental, no data; **1** = absent ("does not include…").

### 4.3 Aggregation + bucketing
- **Overall score = AVERAGE of the 10 criteria** (`=AVERAGE($W6,$Y6,...,$AO6)`).
- **Bucket by rounding to nearest integer:**

  | Average | Rating |
  |---|---|
  | ≥ 4.5 | HIGH |
  | 3.5 – 4.4 | MEDIUM-HIGH |
  | 2.5 – 3.4 | MEDIUM |
  | 1.5 – 2.4 | MEDIUM-LOW |
  | < 1.5 | LOW |

  (A flat 3.5 / 2.5 shows as a split label "MEDIUM/MEDIUM-HIGH" etc.)

  ⚠ **These thresholds are INFERRED, not sourced from a formula.** The PRC workbook contains **no** average→label formula or lookup anywhere — the rating labels are hand-typed text. The observed data is fully consistent with the table above (all 3.5s show split labels, all 2.5s split, 2.6–3.4 = MEDIUM, 1.6–2.4 = MEDIUM-LOW), but no project scored ≥3.6 or <1.5, so the ≥4.5 HIGH band and the <1.5 LOW band have never been observed. Business sign-off (§10) is required before treating these as official.

### 4.4 Authoritative qualitative definition (behind the number, `Potential Results-feedback loop`!I41–J45)
The number is a proxy; official rating is **factor-count based**:
- **HIGH:** strong/direct benefits for ≥3 Freight AND 7 Transportation factors.
- **MEDIUM-HIGH:** ≥3 Freight AND 5 Transportation.
- **MEDIUM:** moderate/co-benefits for 3 Freight AND 5 Transportation, plus strong benefit on ≥1 factor.
- **MEDIUM-LOW:** minor/indirect but somewhat measurable benefits; below Medium.
- **LOW:** no meaningful freight-movement benefit.

### 4.5 Per-criterion population stats (calibration reference)
**Actual before-feedback PRC averages** (`PRC Results (before feedback)` row 40, 32 projects): Throughput 2.87 · Velocity 2.97 · Reliability 2.83 · Freight Safety 3.27 · Congestion 2.97 · Bottleneck 2.97 · Multi-Modal 2.83 · Interregional 3.20 · Adv Tech 2.03 · **ZEV 1.13**. Freight Safety and Interregional are the easiest; ZEV is by far the hardest (most projects score 1). Overall pool: avg 2.725, median 2.8, max 3.5, min 1.6.

Rating distribution (after-feedback sheet, col F): **0 HIGH, 8 MEDIUM-HIGH, 20 MEDIUM, 4 MEDIUM-LOW.**

⚠ Do not confuse these with the **hypothetical "Potential (if responsive)" averages** on `Potential Results-feedback loop` row 39 (Throughput 3.23 · Velocity 3.70 · Reliability 3.40 · Safety 4.80 · Congestion 3.37 · Bottleneck 3.27 · Multi-Modal 3.27 · Interregional 3.67 · Adv Tech 2.80 · ZEV 3.90) — those model what projects *could* score if they addressed reviewer feedback, not what the PRC actually awarded. (An earlier draft of this spec cited them as the actual averages; corrected 2026-07-10.)

### 4.6 Human-in-the-loop (HIFL) — matches PDE V2
Three scores per project, side by side in the feedback-loop tab:
1. **B — Total Points Earned (from PRC)** — raw draft.
2. **D — Consistency Review score** — after human calibration (11 criteria).
3. **H — Potential score** — if the applicant is responsive to areas for improvement (10 criteria).

Between "before feedback" and "after feedback" sheets: narrative summaries rewritten (new "Revised Summaries" col), scores nudged up ~0.1–0.5, split ratings resolved to one bucket, one project dropped (Harbor Drive 2.0). **Move-up eligibility ("↑") is driven by above-average CAPTI score.**

---

## 5. Eligibility, ineligibility & cycle rules (codeable rule tables)

### 5.1 Schedule / cycle deadlines (embedded in template labels + Risk Ratings)
- **Environmental clearance (PA&ED):** within **6 months of program adoption (by Dec 2027)**.
- **RTL:** CON-funding projects Ready-to-List **no later than June 2029**.
- **Begin construction:** TCEP must commence R/W acquisition or actual construction **within 10 years of pre-construction funding**; CON-funding projects **ready to start construction by Dec 31, 2029**.
- **⚠ SOURCE CONTRADICTION:** Appendix A states SCCP construction-ready by **Dec 31, 2027**, conflicting with the main form's **Dec 31, 2029**. Business must resolve which governs.

### 5.2 Schedule risk tiers (Risk Ratings → Project Schedule sheet)
- **High risk:** RTL Jan–Jun 2029 / CON Jun–Dec 2029 (closest to deadline).
- **Medium:** RTL Jul–Dec 2028 / CON Dec 2028–May 2029.
- **Low:** RTL before Jul 2027–Jun 2028 / CON Jul 2027–Nov 2028.
- **Shovel Ready:** ready to list in the first 6 months of the first year of allocation.
- **Higher Priority** (separate row from Shovel Ready): PS&E + ROW complete by application submittal (Nov 2026).

### 5.3 Funding risk tiers (Risk Ratings → Funding Plan sheet)
- **High:** uncommitted > $60M, OR request > $90M, OR no leverage/match.
- **Medium:** $25–60M uncommitted, OR $75–89M request, OR leverage < 10%.
- **Low:** < $25M uncommitted, OR ≤ $75M request.
- **Match:** TCEP **30% minimum match** on regional request (`Required Local Match = regional × 0.3`); desirable **60/40 regional/state** split. SCCP no match required (leverage is criteria only, construction phase only, pre-allocation costs excluded). *(Sourcing note: the 30%-match rule text lives in the template form C46 and the PRC sheet's "Required Local Match" column `=N×0.3` — NOT in the Risk Ratings file, which only has generic Match Funding rows with SCCP-only footnotes.)*

### 5.4 Ineligibility conditions (verbatim, Risk Ratings B9)
1. Uncommitted funds only from: SCCP, TCEP, LPP, or a federal discretionary grant. 2. Construction phase of a capital project only.* 3. Environmental process complete ≤6 months after program adoption. 4. Must not supplant committed funds. 5. Fund cost increases (SCCP only).* 6. Must prove ability to absorb overruns w/ no additional SCCP/TCEP. 7. Independent utility / standalone corridor benefits. 8. Ready to start construction by Dec 31, 2029. 9. Caltrans doesn't pay SCCP overruns.* (*SCCP-only)

### 5.5 TCEP screening gate (Intent-to-Nominate Q20–24)
Must meet **ALL** of: primary purpose aligns with CFMP/NHFP; on a priority freight corridor (CRFC/CUFC/NHFN/SB671); direct freight benefit (not general-purpose traffic); measurable freight impact. District cap: **5 nominations per program**.

### 5.6 Program budget context
TCEP pool modeled at $1.0B (`PRC` L3 = 1,000,000 in $1,000s; $600M regional + $400M state); total requests **$1.92B** (`L39=SUM(L4:L37)` = $1,920,940K; regional $880.9M + state $998.1M) → **~192% oversubscribed** (adds urgency to accurate triage). *(An earlier draft said $1.84B/173% — corrected 2026-07-10; the only "173%" in the corpus is an unrelated speed-increase stat.)*

---

## 6. The Cal-B/C benefit-cost model (read, don't recompute)

### 6.1 Two different editions — do NOT assume one layout
| | D4 | D10 |
|---|---|---|
| Edition | **Cal-B/C Sketch v8.1** | **Cal-B/C Corridor v8.1** |
| Sheets | 12 | 18 (adds Input, Consumer Surplus, Performance Measures ×3, PM Backups) |
| B/C ratio cell | `3) Results!H17` = 6.71 | `3) Results!H18` = 1.98 |
| Payback | `H21` (number) | `H22` (text "9 years") |

### 6.2 Read strategy
- **Resolve outputs via workbook-defined NAMES, not fixed cells:** `LifeCycleCost`, `LifeCycleBene`, `NetPresentValue`, `ReturnOnInvest`, `Payback`, `DiscRate` — identical meaning in both files. ⚠ Exception: **`BeneCostRatio` exists only in the Sketch edition** — the Corridor file has no defined name for B/C, so the reader needs a label-scan fallback on the Results sheet (implemented).
- For itemized/emissions grids, **branch on model type detected from `Title!C2`** ("Sketch" vs "Corridor").
- Key outputs: Life-Cycle Cost (`H13`), Life-Cycle Benefits (`H14`), NPV (`H15`), B/C (H17/H18), Total Benefits (`Q19`), Person-Hours Saved, emissions by pollutant (tons + $).
- `PARAMETERS` sheet = monetization bank, **identical coordinates in both** (value of a fatality $11.8M, discount rate 4%, VOT auto/truck, emission $/ton tables).
- **No external workbook links.** Macro-enabled (`.xlsm`) but logic is all in-sheet.

### 6.3 Metric → BCA traceability (from the performance-metrics DOCX)
Each submitted metric documents its exact BCA source cell + arithmetic, e.g.:
- Fatalities/100M VMT = `3 / 254,161,535 × 100,000,000 = 1.16`, ×CMF 0.77 = 0.9089.
- VMT reduction = `195,651 − 167,103 = 28,548`.
- Truck % assumptions (5.2%, 15%, 15.85%) cited from Caltrans Truck Traffic Census.

This enables requirement **D.2 (validate Excel workbook against narrative docs)** — cross-check submitted metric values against the BCA model and backup docs.

---

## 7. Traffic study / OCR reality

- **Main TOAR + memo narrative extract cleanly** (2,000–2,600 chars/page). Summary tables (LOS by scenario, MOE: delay −64%, speed +54%, VMT +28%) recover well.
- **All figures/charts are image-only** (~20 numbered figures per report → OCR/vision to read).
- **The 234-page appendix is 100% scanned raster (0 extractable text)** — all granular per-segment LOS/density/volume tables need OCR/vision. Mirror the ROW module's **GPT-4o Vision fallback** pattern (fire vision only where text extraction fails, to control cost).
- Design/forecast years are **project-specific** (D4 = 2035/2050; D10 = 2033/2052) — the extractor must read them, not assume.

---

## 8. Real data-quality issues found in the two samples (the tool must catch these)

- **Cross-field contradiction (D.1/D.2):** D10 states TCEP request $65M in Section IV but its fund table shows only $60M CON (−$5M).
- **Units error (E):** D10 "12b uncommitted = 4.45" where every other cell says $4.45M / 4,450 ($1,000s).
- **Impossible dates:** both forms carry 1930s closeout milestone dates (template artifacts).
- **Weak/blank fields:** D4 "4b Outputs" is a 0-sentence stub ("One interchange Modfication"); D10 narrative has a verbatim-duplicated paragraph and discusses "Advanced Technology" without selecting it as a 13a factor.
- **Scale/unit divergence:** the "same" metric row comes back in wildly different units/magnitudes between projects (D4 safety in whole crash counts; D10 in fractional rates) — scorer must normalize.
- **Both samples are TCEP-only** (SCCP Section VI left blank) — the tool must handle single-program submissions gracefully.

---

## 9. Capability matrix disposition (v1 scope decided: "scoring core first")

| Cap group | v1? | Notes |
|---|---|---|
| A. Upload & intake | ✅ | Reuse PDE upload; configurable doc-type list |
| B. Scanning & extraction | ✅ (B.3 partial) | Workbook + PDF/DOCX extract; traffic-table OCR deferred |
| C. Rubric coverage & gap analysis | ✅ | Core PDE pattern |
| D. Contradiction checking | ✅ | Real examples in §8; workbook-vs-narrative (D.2) via §6.3 |
| E. Nonspecific-language detection | ⚠ partial | `flag_vague_language()` implemented but not yet wired into the UI flow |
| F. Ineligibility screening | ✅ | Rules in §5.4 |
| G. Cycle eligibility | ✅ | Date rules in §5.1–5.2 (mostly deterministic) |
| H. Rubric rating & scoring | ✅ | Rubric in §4; **H.4 official aggregation = AVERAGE→bucket, now known** |
| I. Traffic-study visualization | ⛔ defer | Depends on OCR of scanned tables; highest risk |
| J. Reporting & output | ✅ | Reuse `build_evaluation_excel_v2` pattern |
| K. Role-based access / SSO | ✅ (infra) | Same as existing app |
| L. Admin & config | ⚠ partial | Cycle dates/thresholds configurable; rubric edits later |

**Deferred / blocked:** traffic-study auto-charting (I); **wiring `flag_vague_language()` into the UI results view (E — agreed pending work, 2026-07-10; the function is implemented and tested, just not surfaced)**; and formal business sign-off on the reverse-engineered rubric.

---

## 10. Gaps — what's genuinely left for business

Everything needed to **build** is in hand. Remaining items are **confirmations**, not blockers:
1. **Sign off the rubric** — confirm the recovered 1–5 anchors + factor-count definitions + AVERAGE→bucket thresholds (§4) match current intent.
2. **Resolve the Dec-2027 vs Dec-2029 construction-deadline contradiction** (§5.1).
3. **Confirm document-type list** (required vs optional) and whether the Cal-B/C `.xlsm` is validated or just read.
4. Confirm whether the **11th Air Quality criterion** counts toward the overall rating or stays separate (currently separate).

---

## 11. Build plan (agreed direction)

- **Location:** new use case in `app.py` (sidebar selectbox + `elif` branch) with logic in **`src/program_fit_evaluator.py`**, mirroring PDE. Reuse `_get_client` (LLM switcher), `_extract_json_object`/`_safe_json_parse`, Excel styling helpers, SSO.
- **Shape:**
  - `RUBRIC_TCEP` (10 criteria + anchors), `RUBRIC_SCCP`, `RATING_BUCKETS`.
  - `run_program_fit_evaluation(package)` → `extract_package()` → `score_all_factors()` → `screen_eligibility()` → `detect_contradictions()` → `compute_program_fit_rating()` → `build_program_fit_excel()`.
  - Cal-B/C reader keyed on defined names + `Title!C2` edition branch.
  - HIFL review flow reusing the PDE V2 pattern.
- **Mirror to `databricks/`** if shared logic changes (two copies of the app exist).

---

*Last updated: 2026-07-10. Derived entirely from the provided source files; no external assumptions. Audited 2026-07-10 against every source workbook cell-by-cell: corrected §4.5 (actual vs "Potential" averages, MEDIUM=20), §5.6 ($1.92B/192%), §5.2 (Shovel Ready vs Higher Priority), §5.3 (match-rule sourcing), §4.2/4.4 (legend location), and added the §4.3 inferred-thresholds and §6.2 BeneCostRatio caveats.*
