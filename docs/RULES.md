# TALIC UW Engine v9 — Authoritative Rules & SOP

**Audience:** Reviewers (codex, internal UW SMEs, Datamatics QA).
**Purpose:** A single document that lists every financial, medical, SOP, and compliance rule the v9 pipeline applies to a case, with the exact file/path the rule lives in. Use this to confirm the pipeline matches TALIC intent before the next batch.

> All paths are relative to the repo root: `d:/107_APA/TALIC/ITR-3/uw_engine_v9/`.
> All rule constants are in `config/uw_rules.json` and `config/medical_rules.json` unless noted. Numeric values shown here are the **defaults the engine uses today** — change them in config, not in code.

---

## 1. Pipeline & SOP

### 1.1 What runs, in order

For every case folder, the orchestrator (`orchestrator/master.py`):

1. **Intake** — walks the case folder, extracts ZIP archives into a temp dir, builds the per-agent doc routing.
2. **Identity agent** → `applicant.*`, `anti_fraud_markers.*`.
3. **Income agent** → `income.*` (+ full `bank_transactions[]`).
4. **Assets & liabilities agent** → `assets_liabilities.*` (loans, EMI, bank statements).
5. **Compliance agent** → `compliance.*` (KYC + AML + existing-cover declarations).
6. **Business/Surrogate agents** → `business_insurance.*`, `credit_protect.*`, `surrogate.*` when product applies.
7. **Medical agent (primary + secondary, dual-vendor)** → `medical.*` + `medical.dual_vendor_diff.*`.
8. **Focused re-OCR passes** (handwritten vitals, family history) and **template-anchored MER extraction** if atlas is available.
9. **Policy_Data join** (TALIC system-of-record xlsx) → `policy_data.*`.
10. **IIB / Alpha Policy joins** (currently stub — feed not on file).
11. **Identity verification** — DC distance via Google Maps; KYC↔live photo Gemini face match.
12. **Reconciler agent** → `reconciliation.*` (contradictions + risk_signals + STP rec).
13. **Engine layer** (`engine/run.py`) — deterministic calculations, gating, flags, ACM, loading, verdict.
14. **Renderer** (`renderer/pdf.py`) — 28-section + 9-conditional case sheet.
15. **QC** (`qc/`) — coherence checks + leak detection.

### 1.2 Hard invariants (from `CLAUDE.md`)

1. Canonical case JSON shape is the contract — agents emit under `applicant.*`, `income.*`, `medical.*`, etc.; the renderer's `field_map.py` reads those paths verbatim.
2. Every agent emits an `AgentResult` with `confidence` + `notes` + `contradictions[]`.
3. Only the master orchestrator decides which agents run — agents do not trigger each other.
4. All LLM calls go through `llm/client.py` (backend toggled by `LLM_BACKEND` env var: `claude` | `azure_claude` | `gemini`).
5. **Don't add a classifier** — the master orchestrator's plan IS the classification.
6. **Don't merge agent outputs by dict-merge** — use the reconciler so contradictions can be flagged.

### 1.3 Reproducibility

* Every numeric value is wrapped in a `Calculation` dataclass with `function`, `formula`, `inputs`, `sources`, `rule` — full provenance from raw doc → final number.
* Engine layer is deterministic. The same case_json run through `engine/run.py` twice produces byte-identical `decision_json` (modulo Gemini face-match outputs).
* Vendor/brand names ("Claude", "Gemini", "Anthropic", "OpenAI") **must not** appear in any output text shown to the underwriter — see `agents/prompts/reconciler.txt` rule 9.

---

## 2. Identity & Demographics

### 2.1 Identity agent (`agents/prompts/identity.txt`)

| Field | Rule |
|---|---|
| `applicant_name` | Prefer the doc the rest of the case is consistent with (PAN/CAF). Resolution recorded in `contradictions`. |
| `pan` | 5 letters + 4 digits + 1 letter; 4th letter = holder type ('P' = individual). |
| `aadhaar` | Keep masking exactly as printed; never reveal masked digits. |
| `dob` → `age` | `today − dob`. Today's date hard-coded in the prompt; engine recomputes via `engine.calculator.compute_age`. |
| `occupation` | Canonical: Salaried / Self-Employed Professional / Self-Employed Business / Homemaker / Student / Agriculturist / Retired. |
| `residential_status` | Resident / NRI / PIO / OCI / Foreign National. |
| `country_of_residence` | Required for FX gating on NRI cases. Source priority: passport stamp → employer addr → VMER prose → NRO/NRE bank. |
| `product_code` | Looked up in `config/products.json` aliases array. Examples: SML_GOLD, MRSS, SARAL_JEEVAN, CREDIT_PROTECT, SEP, PR_LIFE_PRO_PLUS. |
| `channel` | Direct / Websales / PolicyBazaar / PB Partners / Digital Partners / TDL / PhonePe / Fronzo Solution / IndMoney / Bancassurance / Agency / Broker. Drives CPR exemption and VMER skip rules downstream. |
| `dc_distance_km`, `dc_distance_band` | **Agent must leave these null** — engine computes them via Google Maps geocoding in `engine/identity_verification.py`. |

### 2.2 Identity verification (engine layer)

`engine/identity_verification.py`:

* **DC distance band** (`engine/geo_utils.distance_band_km`): ≤10 km / 10-25 km / 25-50 km / 50-100 km / >100 km / Unknown.
* **DC_DISTANCE_HIGH** flag fires at ≥50 km (MEDIUM severity).
* **Live photo discovery** — searches for `live photo`, `tpa photo`, `photo identification`, `geotag`, `insta piwc`, `selfie`, `applicant photo`, `patient photo` keyword in filenames (loose + inside ZIPs).
* **KYC photo discovery** — keywords: `aadhaar/aadhar`, `pan card`, `passport`, `voter`, `driving licence`, `kyc`.
* **Face match** (`engine/photo_match.py` → Gemini): returns `match | no-match | partial-match | undetectable | skipped` with confidence.
  * `no-match` → `PHOTO_MATCH_FAIL` (HIGH).
  * `partial-match | undetectable` → `PHOTO_MATCH_REVIEW` (MEDIUM).
  * `LIVE_PHOTO_MISSING` (MEDIUM) when no live-photo file present.

### 2.3 Geocoding cache

`engine/.geocode_cache.json` — per-address LRU cache to avoid re-billing Google Maps. Cleared by deleting the file.

---

## 3. Income — Cascade, FX, Sanity

### 3.1 Cascade priority (`engine/calculator._PRIMARY_PRIORITY`)

Lower index = stronger evidence:

| Rung | Source | Gating consequence |
|---|---|---|
| 1 | `salary_slip` | PASS |
| 2 | `form_16` | PASS |
| 3 | `ca_computation` | PASS |
| 4 | `audited_financials` | PASS |
| 5 | `itr` | PASS |
| 6 | `bank_credits` | **WARN** — bank-credits only |
| 7 | `declared` (CAF only) | **FAIL** — `INCOME_NOT_ESTABLISHED` if no other path |

`income.annual_income` **must equal what the chosen `primary_source` evaluates to** — the CAF / declared figure goes into `declared_annual_income` for cross-check only (agent prompt rule).

### 3.2 Deterministic recomputes (engine overrides agent)

* **`salary_slip`** → `engine.calculator._salary_slip_mean_annual`:
  * Compute median gross across submitted slips.
  * Exclude any slip whose gross > **1.5 × median** (treated as arrears/back-pay outlier).
  * `annual = mean(kept) × 12`.
  * Fallback: if all slips are flagged outlier, `annual = median × 12`.
* **`bank_credits`** → `engine.calculator._bank_credits_modal_annual`:
  * Group credits by employer; pick employer with most credits (≥ 2 occurrences) as "primary employer".
  * Pick **modal** credit amount (most repeated) from that employer.
  * `annual = modal × 12`. Fallback: median of primary credits if no mode.
* Audit dict + agent's original figure preserved in `selected_income.inputs.{salary_slip_audit | bank_credits_audit}`.

### 3.3 FX conversion (`config/fx_rates.json`)

Triggered when `income.currency != "INR"` OR the engine infers a non-INR currency by:

1. Text-scan of `income_sources[]` for ISO codes + aliases (`Dh`/`KD`/`S$`/`RO`/`AED-DUBAI`/etc.).
2. `bank_statements[*].currency` (any non-INR account).
3. `applicant.country_of_residence` → currency map.
4. `applicant.employer_name` suffix scan (e.g. `… LLC Dubai → AED`).

Current rate file (`_updated: 2026-05-06`) supports INR, AED, USD, GBP, EUR, SGD, QAR, SAR, OMR, KWD, BHD, AUD, NZD, CAD, CHF, HKD, JPY, MYR, THB, ZAR, NPR, LKR.

`FX-STALE` flag (MEDIUM) fires when `_updated` > 7 days old (`uw_rules.fx.staleness_warn_days`).

### 3.4 Sanity flags

* **`INCOME_SANITY_HIGH`** — fires when `selected_income ≥ ₹2 Cr` AND method is not FX-converted AND not from audited P&L. Severity: HIGH at ≥ ₹5 Cr, MEDIUM at ₹2-5 Cr. Aim: catch Indian-comma misreads (₹29,10,610 misread as ₹2.91 Cr — 10× error).
* **Indian comma format guidance** is in the income prompt — 2,2,3-grouped lakh/crore, not Western 3,3,3 thousand.

### 3.5 Income consistency

Per `agents/prompts/income.txt`:
* `ratio = declared / verified`
* `≤ 1.05` → `consistent`
* `1.05 < ratio ≤ 1.40` → `declared_above_actual` (typical net-vs-gross spread; not a contradiction).
* `> 1.40` OR `< 0.95` → `declared_above_actual` + contradiction logged.

### 3.6 Earned-income heads (`uw_rules.json`)

Included in HLV: `salary`, `business_profession`, `firm_remuneration`, `firm_interest_on_capital`, `firm_profit_share_10_2a`, `pension_retirement`.
Excluded: `house_property`, `capital_gains_*`, `other_sources_*`, `family_pension`, `agricultural_income`, `other_exempt`. Threshold for flagging an excluded-head dependence: 20 % (`excluded_income_flag_threshold_pct`).

### 3.7 Bank statement (Perfios-style) analysis

`engine/bank_statement_analyzer.py` runs deterministically over `income.bank_transactions[]`:

* Detects **EMIs** — counterparty with ≥ 3 month-distinct debits, either matching `emi|loan|home loan|auto loan|car loan|personal loan|mortgage` keywords OR with stable amount (CV < 20 %).
* Detects **bounces** — narration matches `cheque return | chq return | i/w chq return | insufficient funds | ecs return | nach return | ach return | bounce charge | bounce penal`. `BOUNCE_RATE_HIGH` note when ≥ 3 bounces in window.
* Detects **recurring credits** by counterparty (≥ 3 month-distinct), tagging `is_salary_like` for salary keywords OR avg amount in ₹10K-₹50L band.
* `counterparty_top` = top 5 credit counterparties by share %.
* Monthly aggregates → `bank_analysis.monthly_summary[]`.

### 3.8 Top-company list (`uw_rules.top_companies`)

When the applicant's employer matches one of the 31 listed companies (HCL, Infosys, TCS, Wipro, … HDFC Bank, ICICI Bank, …), 1-month salary slip + ID proof suffices up to **SA ₹3 Cr**. Master 52K-companies xlsx is at `config/top_52k_companies.json` for broader matching.

---

## 4. HLV Grids & Sum-Assured Ceilings

### 4.1 Base HLV (`uw_rules.hlv_grids.base`)

| Age band | Multiplier |
|---|---|
| 18-30 | 35 × |
| 31-35 | 30 × |
| 36-40 | 25 × |
| 41-45 | 20 × |
| 46-50 | 15 × |
| 51-55 | 10 × |
| 56-60 | 5 × |
| 61-65 | 5 × |

`hlv_base = annual_income × multiplier(age_band)`.

### 4.2 ADB / ATPD rider HLV

* SA ≤ ₹3 Cr → use base grid.
* SA > ₹3 Cr → fixed multiplier: **15 × for 18-60, 5 × for 61-65**.

### 4.3 CI / HC rider HLV

| Age band | Multiplier |
|---|---|
| 18-30 | 12 × |
| 31-35 | 12 × |
| 36-40 | 10 × |
| 41-45 | 10 × |
| 46-50 | 7.5 × |
| 51-55 | 5 × |
| 56-60 | 0 × |
| 61-65 | 0 × |

### 4.4 Max SA eligible

```
max_sa_eligible = min(hlv_remaining, per_life_cap, proposed_sa)
hlv_remaining   = max(0, hlv_base − existing_cover_declared.total_life_cover_other)
per_life_cap    = uw_rules.max_per_life_crore × 1e7  (default = ₹100 Cr)
```

Existing cover is currently the declared figure from `compliance.existing_cover_declared.total_life_cover_other`. When the IIB feed lands, that aggregate replaces this.

### 4.5 SA-tier income floor (`uw_rules.gating.sa_tier_min_income`)

| Proposed SA | Min verified income |
|---|---|
| Up to ₹1 Cr | ₹5 L |
| ₹1-2 Cr | ₹15 L |
| ₹2-5 Cr | ₹15 L |
| ₹5-10 Cr | ₹25 L |
| ₹10-25 Cr | ₹50 L |
| Above ₹25 Cr | ₹1 Cr |

Mirrors Term UW Feb-2026 p.4 (Non-Grad column — conservative ceiling so we don't under-gate when education is unknown).

### 4.6 Cover-to-loan ratio (Credit Protect)

`credit_protect.cover_to_loan_ratio = proposed_sa / loan_amount_sanctioned`. Term UW Feb 2026 p.32 requires **≤ 1.05**.

---

## 5. FOIR & Affordability

* `foir = (monthly_emi_total / monthly_gross) × 100`.
* Threshold (`uw_rules.foir_threshold_pct`) = **55 %**.
* Gating:
  * FOIR ≤ threshold → PASS.
  * threshold < FOIR ≤ threshold + 10 → WARN.
  * FOIR > threshold + 10 → FAIL.
  * FOIR ≥ 100 % → `FOIR_OVER_100` CRITICAL flag (total EMIs exceed monthly gross).
* `monthly_emi_total` is the assets_liabilities agent's aggregate of bank-narration EMI debits + loan sanction EMIs.

---

## 6. Medical Extraction Rules

### 6.1 Medical agent (`agents/prompts/medical.txt`) — extraction expectations

* **Every lab row** must carry `unit`, `reference_range`, `source_doc`, `source_page`. Never invent a range.
* **BMI** computed only when both height and weight present: `bmi = weight_kg / (height_cm/100)^2`. Boundaries: <18.5 / 18.5–24.9 / 25-29.9 / ≥30.
* **BP** emitted as both `bp_systolic_mmhg` + `bp_diastolic_mmhg` integers AND `bp_text` for traceability.
* **Family history** — do **not** flag a printed checkbox row (e.g. "No impairment / Diabetes / HTN / Stroke / Cancer / Other") as a condition unless Yes is marked, hand-written detail present, OR free-text remark names the condition.
* **Personal history** — scan MER + VMER + every personal-submitted report (2D Echo, USG, CXR, biopsy, discharge summary, etc.). Each finding goes in `personal_medical_history[]` with `condition`, `status` (active/resolved/post-procedure), `first_diagnosed`. Set `documents_present.personal_<slug>: true`. Add a one-line opinion in `agent_meta.notes` per personal report.
* **Medication-without-diagnosis** — drug name on the MER comment (iron, statin, antihypertensive) gets a `personal_medical_history` row even if no formal diagnosis is named.
* **Polio** — only record polio when explicitly named. The MER Q8 musculoskeletal box mentioning polio AS AN EXAMPLE is **not** evidence (C312315043 regression).
* **LFT interpretation** — when AST/ALT/ALP/GGT/Bilirubin are out-of-range AND personal history shows fatty liver, the agent must note this combo prominently with a rate-up recommendation in `agent_meta.notes` (C305285193 regression).
* **UCT mention** — when the medical grid requires Urinary Cotinine and a value is on file, always populate `fraud_signals.urine_cotinine_ng_ml` and call it out in `notes` (Med #1/#6 regression).
* **Questionnaire responses** — every `<X> Questionnaire.pdf` produces a row in `questionnaire_responses[]`, even if all-No.

### 6.2 ECG override (`engine.medical.apply_medical_overrides`)

If `ecg.status ∈ {abnormal, borderline}` AND the interpretation text matches any of:
* `overwritten as normal` | `overridden/overrided to normal` | `reclassified as normal` | `final: normal ecg` | `classified as normal ecg by dr` | `normal ecg by (examining) phys`,

→ engine forces `ecg.status = "normal"` and logs the override in `medical._engine_overrides[]`.

### 6.3 TMT override

* If `tmt.performed == true` AND `thr_achieved_pct < 85` AND `recommendation ∈ {"", "Negative", "Null", "None"}`:
  * Force `tmt.recommendation = "Postponed"` with reason `THR achieved X% < 85% (study not diagnostic)`.
* Agent prompt rule 12 (`agents/prompts/medical.txt`) restates this so the agent emits "Postponed" even when the lab's first impression said "Negative":
  * `thr_achieved_pct < 85` → Postponed.
  * `ectopic_beats != "none"` AND would-be-Negative → Postponed.
  * Positive ST changes / angina / hypotensive response → Positive.
  * Otherwise → Negative.

### 6.4 Template-anchored MER extraction (`engine/mer_template_processor.py`)

For the standard TATA AIA MER form:

* Render PDF at 300 DPI.
* Phase-correlation alignment to the blank template (`config/mer_template_atlas.json` references the template PDF via `_template_pdf`).
* Differential ink: only ink present in the filled form but **absent** in the blank template counts.
* For binary checkbox pairs (`.yes/.no`, `.living/.deceased`, `.gender_m/.gender_f`):
  * Winner-takes-all on raw ink ratio, with `_PAIR_MIN_DIFF = 0.05` and `_PAIR_MIN_INK = 0.05`. Tie → neither marked (explicit ambiguity).
* Standalone checkboxes use a per-page noise-floor threshold (`_MARK_THRESHOLD = 0.05`).
* Sibling-row sanity: a row where **neither** Living NOR Deceased resolved is treated as unfilled — every box in that row cleared (catches stamps/seals bleeding into checkbox-sized regions).

### 6.5 Family-history per-cell isolation (`engine/family_checkbox.py`)

When dual-vendor and full-page reads both flip M↔F on the Sibling row (verified on C308394654):

* Re-render the located table at 600 DPI.
* Detect row strips via per-row dark-pixel density.
* For each Sibling row, crop the leftmost 13 % of width (relation cell), then bottom half, then split L/R into M-cell and F-cell.
* Send each tiny PNG to Gemini ALONE asking "is this single checkbox filled?".
* Outcomes: M only → `brother`; F only → `sister`; both → `ambiguous_both`; neither → `ambiguous_neither`; unread → `ambiguous_unread`.
* Original member dict preserved in `_pre_isolation_relationship`.

### 6.6 Dual-vendor medical reconciliation (`engine/medical_dual_vendor.py`)

High-signal fields compared (`_FIELD_SPECS`):

| Field | Comparator | Severity if disagreed |
|---|---|---|
| `vitals.height_cm` | numeric ±2 % | MEDIUM |
| `vitals.weight_kg` | numeric ±2 % | MEDIUM |
| `vitals.bp_systolic_mmhg` | numeric ±2 % | MEDIUM |
| `vitals.bp_diastolic_mmhg` | numeric ±2 % | MEDIUM |
| `vitals.pulse_bpm` | numeric ±2 % | LOW |
| `vitals.build` | exact | LOW |
| `habits_declared.smoker` | exact | **HIGH** |
| `habits_declared.alcohol` | exact | MEDIUM |
| `habits_declared.tobacco_other_forms` | exact | MEDIUM |
| `fraud_signals.declared_smoker` | exact | **HIGH** |
| `fraud_signals.cotinine_smoker_mismatch` | bool | **HIGH** |
| `ecg.performed` | bool | MEDIUM |
| `ecg.status` | exact | **HIGH** |
| `tmt.performed` | bool | MEDIUM |
| `tmt.recommendation` | exact | **HIGH** |
| `personal_medical_history.condition_set` | set diff | **HIGH** |
| `family_history.condition_set` | set diff | MEDIUM |

Each disagreement produces a `MEDICAL_DUAL_VENDOR_DISAGREEMENT__<FIELD>` flag.

### 6.7 Re-OCR cross-check on vitals

`engine/flags._flags_from_vitals_reocr`:
* Compare primary `height_cm/weight_kg` against `medical.vitals_reocr.values.*`.
* `max(Δh%, Δw%) ≥ 2 %` → `VITALS_REOCR_DISAGREEMENT` MEDIUM.
* `max(Δh%, Δw%) ≥ 10 %` → HIGH.
* Advisory `BMI_HANDWRITTEN_DIGITS` always emitted (LOW) so UW eyeballs the source; promoted note when BMI sits near a band boundary (17.5-19, 23.5-26, 28.5-30.5).

### 6.8 Family-history re-OCR cross-check

`engine.flags._flags_from_family_history_reocr` — compare `medical.family_history` against `medical.family_history_reocr.values.members` by `(relationship, age)` tuple set. Any mismatch → `FAMILY_HISTORY_REOCR_DISAGREEMENT` MEDIUM.

---

## 7. Medical Grid (Required Tests)

### 7.1 Grid lookup (`config/medical_rules.json` — `MEDICAL_GRID`)

Keyed by **MSAR** (Medical Sum At Risk) × **age band**:

* MSAR bands: `0-50L`, `50L-2Cr (- ₹1)`, `2-3 Cr`, `3-10 Cr`, `10-15 Cr`, `15-25 Cr`, `>25 Cr`.
* Age bands: `18-35`, `36-45`, `46-55`, `Above 55`.
* Each cell lists tests by abbreviation: MER, MSU, BPB, BPB-F, CBC, HBA1C, ECG, TMT, PSA, Anti HCV, Chest X-Ray, Urine Microalbumin, USG abdomen, 2D echo, Stress Echo, PFT, Fundoscopy, Thyroid function tests, Tumor markers.
* BPB / BPB-F components are spelled out in `TEST_DEFINITIONS.BPB_components` / `BPB_F_components`.

### 7.2 Special rules layered on top of the grid (`engine.medical.lookup_required_tests`)

| Rule | Logic |
|---|---|
| Urine Cotinine | Add when `habits_declared.smoker == "no"` AND cotinine not already in tests. |
| PSA (age) | Add for male age > 55 if not already in tests. |
| PSA (SA) | Add when MSAR > ₹25 Cr (`250_000_000`) — part of tumor markers panel. |
| TMT (Layout #2) | **Mandatory** at age 36-45 with SA > ₹3 Cr, even when grid lists it as optional. |
| Fundoscopy | Applicable only for age > 50 OR diabetic applicants (`medical_rules.MEDICAL_GRID.special_rules`). |
| Live photo | Required at time of medicals for **all** cases. |
| PB-channel VMER skip | If channel ∈ {PB, Policy Bazaar, PolicyBazaar} AND (`vpsc_present` OR `piv_present`) AND MSAR ≤ ₹2.25 Cr → drop VMER from the required list (Layout Other #5 + `VMER_GRID_RI.policy_bazaar_special`). |

### 7.3 Test compliance

`test_compliance_pct = 100 × |received| / |required|` where `received` is a positive flag in `medical.documents_present`.

Gating:
* `≥ 100 %` → PASS.
* `≥ 75 %` → WARN (lists missing tests).
* `< 75 %` → FAIL.

`MEDICAL_DOCS_INCOMPLETE` HIGH flag fires when compliance < 50 %.

### 7.4 VMER eligibility (`config/medical_rules.json` — VMER grids)

* **`VMER_GRID_RI`** (Resident Indian, graduate+, white-collar salaried only — off-roll/consultants ineligible):
  * Mandatory VMER irrespective of TSAR: Business Insurance, Age > 60, Transgender.
  * Salaried grid by age × income × MSAR band — see config.
  * Policy Bazaar special: Up to ₹2.25 Cr → VPSC + TELE considered as VMER.
* **`VMER_GRID_NRI`** — graduate+, standard countries. GCC countries: Saudi Arabia, Kuwait, UAE, Qatar, Bahrain, Oman.
* **`VMER_TATA_GROUP_EMPLOYEE`** — separate, more generous grid.
* **`VMER_CHANNEL_SPECIFIC`** — IND Money, American Express (card-tier based), PhonePe (Affluence Score ≥ 75 + Financial Waiver = Yes, max age 45, max MSAR ₹1 Cr).

### 7.5 Medical report validity (`MEDICAL_REPORT_VALIDITY`)

| Type | Standard | Substandard |
|---|---|---|
| Physical, MSAR ≤ ₹25 Cr | 1 year | 6 months |
| Physical, MSAR > ₹25 Cr | 3 months | No validity |
| Tele/VMER, ≤ ₹2 Cr | 90 days | No validity |

### 7.6 `engine/medicals` rules (`uw_rules.medicals`)

| Rule | Value |
|---|---|
| `vmer_max_age` | 50 |
| `physical_mandatory_above_age` | 50 |
| `vmer_max_sa_crore` | 10 |

### 7.7 Replacement cover

`REPLACEMENT_COVER`: must be disclosed upfront with status (in force / surrendered / lapsed / under process). Only after policy anniversary or premium due date. Affidavit on plain paper up to ₹10 Cr; on ₹100 stamp paper above ₹10 Cr.

---

## 8. Lab Interpretation Overlay

`engine/lab_ranges.py` reads `config/medical_lab_ranges.json` (sourced from Master-File `Medical Data ranges.xlsx`) and **re-classifies every `medical.lab_results[].status`** against TALIC's master ranges:

* Each lab matched by normalised name + alias map (e.g. `SGOT → AST`, `ALP → ALKALINE PHOSPHATE`, `Hb → HAEMOGLOBIN`).
* Bands picked by applicant gender: `male_min/male_max` vs `female_min/female_max`.
* `value < min → LOW`, `value > max → HIGH`, in-range → `NORMAL`.
* Qualitative tests (HIV / HBsAg / Cotinine): `non-reactive / negative / not detected / absent / nil → NORMAL`; `reactive / positive / detected / present → HIGH` (abnormal bucket).
* **Unit mismatch safety** — if master units don't match the lab's units (e.g. SI vs conventional), the agent's classification is kept and the row notes `TALIC master unit mismatch`.
* Agent's original reads preserved in `agent_status` / `agent_reference_range`.

---

## 9. Health Score & Medical Risk

### 9.1 Health score (`engine.medical.compute_health_score`) — start at 10

| Component | Penalty |
|---|---|
| Lab abnormalities | −0.3 per row, capped at −3.0 |
| BMI underweight | −0.5 |
| BMI normal | 0 |
| BMI overweight | −0.5 |
| BMI obese | −1.0 |
| Each active **ratable** condition | −0.7 |
| Active **standard-rated** condition | 0 (recorded, not penalised) |
| Declared smoker | −1.5 |
| Family-history first-degree + early-onset (per relative-condition) | −0.5 |
| BP stage 1 (≥130/85, < 140/90) | −0.5 |
| BP stage 2 (≥140/90) | −1.0 |

Final score clamped to `[0.0, 10.0]`, rounded to 1 decimal.

#### Standard-rated conditions (recorded but **not** penalised)

`engine/medical._STANDARD_RATED_CONDITIONS`: fatty liver, hyperlipidemia, raised cholesterol, vitamin D deficiency, iron deficiency, low haemoglobin (mild), calcified granuloma, non-obstructive / renal / calyceal calculus, hyperopia, myopia, hair loss, alopecia, hair transplant, obesity (BMI handles it separately), morbid obesity, COVID-19 (resolved), shoulder dislocation (history), hernio*, appendec*, tonsillec*, vaginal delivery, ligation, ovarian, cyst, fibroid, cholelithiasis (uncomplicated gallstones).

### 9.2 Medical risk band

Primary classification by score:
* `≥ 8.5` → **LOW**.
* `6.0 ≤ score < 8.5` → **MODERATE**.
* `< 6.0` → **HIGH**.

Hard overrides:
* Active serious condition (heart disease / cardiovascular / stroke / cancer / kidney disease / renal failure / uncontrolled diabetes) → at least HIGH.
* Age > 60 with > 3 abnormal labs → HIGH.
* BMI obese → at minimum MODERATE.

### 9.3 Medical UW Terminal Decision (Datamatics LAYOUT row 70)

`engine.medical.compute_medical_uw_terminal_decision` → one of:

| Outcome | Triggers |
|---|---|
| Decline | Active serious condition + ≥ 4 abnormal labs. |
| Postpone | Active serious condition (alone); OR medical_risk = HIGH; OR ≥ 3 abnormal labs at HIGH risk. |
| Additional Test | Test compliance < 100 % (lists what's outstanding). |
| Rateable | All tests received, ≥ 3 lab abnormalities. |
| Repeat Test | Medical risk = MODERATE (recheck borderline values). |
| Medically Std | All tests received, all normal/borderline, no adverse findings. |

---

## 10. Loading (EMR) Recommendations

`engine/loading.py` — runs deterministically; each rule emits a `LoadingItem(code, severity, emr_delta, driver, source, suggestion)`. Recommended EMR is the **sum** of items, capped at +200. Sum > 200 OR any `critical` item → `decline_recommended = True`.

| Code | Trigger | EMR |
|---|---|---|
| `HBSAG_POSITIVE_WITH_RAISED_LFT` | HBsAg+ AND (SGOT > 40 OR SGPT > 40) | +100, severity HIGH |
| `HBSAG_POSITIVE` | HBsAg+ but LFTs not raised / unavailable | +50, HIGH |
| `METABOLIC_SYNDROME_RISK` | ≥ 2 concurrent of: BMI≥35, BP≥140/90, total chol ≥240, LDL ≥160, HDL <35 | +50, MEDIUM |
| `BMI_OBESE_CLASS2_PLUS` | BMI ≥ 35 standalone | +25, MEDIUM |
| `HBA1C_UNCONTROLLED` | HbA1c ≥ 7.5 % | +100, HIGH |
| `HBA1C_DIABETIC` | HbA1c in [6.5 %, 7.5 %) | +50, MEDIUM |
| `COTININE_SMOKER_MISMATCH` | `fraud.cotinine_smoker_mismatch = true` | **0 (DECLINE)**, critical |
| `CAD_IHD_ACTIVE` | Active ischaemic/CAD/myocardial/angina/stent/bypass/CABG | **0 (DECLINE)**, critical |
| `ACTIVE_MALIGNANCY` | Active cancer/malignancy/carcinoma/lymphoma/leukaemia | **0 (DECLINE)**, critical |
| `FATTY_LIVER_GRADE3` | Grade III / severe steatosis (LFTs normal) | +50, MEDIUM |
| `FATTY_LIVER_GRADE2` | Grade II (LFTs normal) | +25, LOW |
| `FATTY_LIVER_GRADE1` | Grade I (LFTs normal) | 0, info |
| Fatty liver undisclosed | + add `+25` to any non-zero fatty-liver EMR when MER non-disclosure flagged | +25 |
| `RENAL_CYST_PROGRESSION` | Cyst documented as growing / new finding | +50, MEDIUM |
| `RENAL_CYST_COMPLEX` | Complex / multilocular / Bosniak / large / multiple / polycystic | +25, LOW |
| `RENAL_CYST_SIMPLE` | Simple cortical cyst, no progression markers | 0, info |
| `RENAL_CALCULUS_RECURRENT` | Multiple episodes documented | +50, MEDIUM |
| `RENAL_CALCULUS_LARGE_OR_MULTIPLE` | Staghorn / >10 mm / multiple | +25, LOW |
| `RENAL_CALCULUS_PRESENT` | Single small calculus, no progression | 0, info |
| `HYPOPROTEINEMIA` | Total protein < 6.0 g/dL **AND** albumin < 3.5 g/dL | +25, LOW |
| `HYPOPROTEINEMIA_BORDERLINE` | One low, the other normal/missing | 0, info |

---

## 11. Fraud Signals & Anti-Fraud

### 11.1 Cotinine

* `medical_rules.MEDICAL_GRID.special_rules`: cotinine threshold = **10 ng/mL**.
* `cotinine_smoker_mismatch = true` ⇔ cotinine ≥ 10 ng/mL AND `habits_declared.smoker == "No"`.
* Mismatch → fraud_risk = HIGH → `COTININE_SMOKER_MISMATCH` CRITICAL flag → verdict = **DECLINE** (`engine/verdict.py:make_verdict` rule).
* Borderline (cotinine ≥ 5 ng/mL AND non-smoker declared) → fraud_risk = MODERATE.

### 11.2 Other fraud bands

`engine.medical.compute_fraud_risk`:
* MER ↔ VMER contradiction count > 0 → fraud_risk ≥ MODERATE.

---

## 12. Compliance, KYC, AML, NRI

### 12.1 KYC status (`agents/prompts/compliance.txt` rule 1)

| Status | Condition |
|---|---|
| Verified | PAN + (Aadhaar OR Passport) present AND name matches across IDs. |
| Partial | PAN present, but Aadhaar/Passport absent OR name mismatch. |
| Pending | PAN absent. |
| Failed | Forged / DOB grossly mismatched. |

**Passport NOT mandatory for Resident Indian.** Do not flag "passport missing" for RI in `compliance_concerns`. Only flag for NRI / foreign national.

### 12.2 VPSC / PIV detection

* `vpsc_present = true` → files titled `VPSC`, `Video PSC`, `PSC Video Verification`.
* `piv_present = true` → Personal Identity Verification report.
* Drives the PB-channel VMER-skip rule (§ 7.2).

### 12.3 AML status

| Status | Condition |
|---|---|
| Compliant | Sanctions clear AND PEP = No AND no high-risk occupation/industry/country. |
| Concerns Raised | Any of: PEP self-disclosure / government servant / NRI from sensitive country / business in defence or cash-heavy industry. |
| Non-Compliant | Sanctions hit OR documented PEP without disclosure. |

### 12.4 Pre-login approval (`compliance.pre_login_approval_required = true`)

Triggered when **any** of:
* `applicant.age > 60`,
* declared SA > ₹25 Cr,
* PEP self-declaration = Yes.

(Also `GUIDELINES_AGE_61_65.pre_login_approval = "Mandatory for any sum assured"`.)

### 12.5 Country master (`config/nri_countries.json`)

Indexed from TALIC master `NBRCOUNTRY_01122025.xls`. Three buckets used by the renderer:

* **`decline.countries`** (≈ 48 countries) — FATF flagged OR (TERM=D and ATPD=D). Includes Afghanistan, Iran, North Korea (implied via Tier-3), Syria, Cuba, Yemen, Russia, Pakistan-adjacent jurisdictions, etc.
* **`acceptable.countries`** — STD=Y AND TERM=Y.
* **`standard_with_caveat.countries`** — country-specific caveats.
* Full per-country FATF + per-product (TERM/NON_TERM/ATPD/CI/HCB/SURGI) flags in `countries[]`.
* GCC list (per medical rules): Saudi Arabia, Kuwait, UAE, Qatar, Bahrain, Oman.

### 12.6 Transgender (`TRANSGENDER_GUIDELINES`)

* Mandatory docs: District Magistrate certificate of identity + Medical Superintendent/CMO certificate.
* Within NML: VMER + MER + BPB required.
* Above NML: VMER + grid medicals as per male life + HIV (if not already in grid).

### 12.7 Pincode master (`config/pincode_top_locations.json`)

Top 7K Indian pincodes with Tier-1/Tier-2/Tier-3 city ranking. Used by the engine to set `dc_in_top_location` and Tier classification (renderer-only — agent must leave city + pin_code populated and NOT try to compute the flag).

---

## 13. ACM Action Matrix

`engine/acm.py` — per Term Underwriting Guidelines for UW V5 — Feb 26, page 6.

### 13.1 Band resolution

* `band = applicant.acm_band` if set; otherwise `"Pending"` (IIB feed not on file).
* Bands: PREFERRED / STANDARD / HIGH / CRITICAL / PENDING.

### 13.2 Product exemption

ACM not applicable for `product_code ∈ {SARAL_JEEVAN, SSR_11X, SSR_15X}`.

### 13.3 CPR-exempt channels

`WEBSALES`, `WEB-SALES`, `DIGITAL PARTNERS`, `DIGITAL`, `TDL`, `PHONEPE`, `FRONZO SOLUTION`, `INDMONEY`, `POLICY BAZAAR`, `POLICYBAZAAR`, `PB`.
**Note:** `PB PARTNERS` is **NOT** exempt (per page-6 footnote).

### 13.4 Five actions (per page-6 table)

| # | Action | PREFERRED/STANDARD | HIGH/CRITICAL |
|---|---|---|---|
| 1 | Income proof submission | Mandatory for all SA (salaried: latest Form 16 or bank statement; SE/Business: ITR with COI mandatory last 3 yrs) | Same, no bridging on surrogate. |
| 2 | RVMER (Regular Video MER) | No | Yes for CRITICAL only. |
| 3 | Detailed Sales Report from Branch Manager (CPR) | No | Yes — unless channel is CPR-exempt. |
| 4 | Tele / Video MER | Existing EODB Tele/VMER applies | **Full Grid Medicals** (Tele/Video not allowed) |
| 5 | Senior UW approval | No | Yes for CRITICAL only. |

---

## 14. Business Insurance / Keyman / EE / Partnership

From `config/medical_rules.json` (`BUSINESS_INSURANCE`):

### 14.1 Allowed plans

Term plan under Keyperson/Keyman Insurance, Partnership Insurance, Employer-Employee Schemes, HUF Cover on Karta.

### 14.2 Keyman cover

* **Financial assessment** = lower of:
  1. Avg Directors remuneration / Salary × 10
  2. Avg Gross profit × 3 × shareholding
  3. (Avg Net profit after tax + 30 % × Avg Depreciation) × 5 × shareholding − existing Keyperson cover.
* Max tenure: 65 yrs (cap ₹100 Cr). Extended to 75 for family-owned cos with affidavit.
* Max entry age: 55.
* **Loss rule:** if company has losses in latest year or last two years → **no KMI cover**.
* Only Basic Death Cover (Life Option). Riders not allowed except TTI.
* VMER mandatory.

### 14.3 Partnership cover

* Financial assessment = lower of: Avg Gross profit × 3 × shareholding, OR (Avg Net profit after tax + 30 % × Avg Depreciation) × 5 × shareholding − existing partnership cover.
* Requirements: latest partnership deed + audited financials (BS, P&L) for last 3 yrs.
* All partners must be insured. Nomination not allowed.

### 14.4 Employer-Employee

* Financial assessment: latest 2 yrs salary income from standard proof × age income multiple. If stakeholder in proposing firm, can add `Avg 3yr Net profit after tax × shareholding`.
* Assignment mandatory.
* Only Non-ROP option. LSPO & Top Up not available.
* `uw_rules.employer_employee.mandatory_employer_docs = ["company_financial"]`; missing → `EE-COMPANY-MISSING` HIGH flag.

### 14.5 LSPO Benefit

`LSPO_BENEFIT`: max entry age 50. Not available for Housewife, Agriculture, Business Insurance. Self-proposed only. UW done when customer exercises.

### 14.6 Self-employed minimums (`uw_rules.self_employed`)

* `itr_years_required: 3`, `average_years: 2`, `ca_coi_mandatory: true`, `firm_pl_required: true`.

### 14.7 MRSS (Maha Raksha Supreme Select) gating (`uw_rules.mrss`)

* Min income ₹15 L, min SA ₹2 Cr, min education HSC, age 21-50.

### 14.8 Profile classifier (`uw_rules.profile_classifier`)

11 profiles drive conditional sections — NRI / Senior 61-65 / Homemaker / Student / Agriculturist / Transgender / Business Insurance (product type contains "Keyman") / Credit Protect / SEP-VTU (Shubh Employee Protect) / Standard Self-Employed / Standard Salaried (default).

---

## 15. Reconciler & Risk Signals

`agents/prompts/reconciler.txt` — runs across all agent outputs, does **not** re-extract from docs.

### 15.1 Output

* `contradictions[]` — facts that disagree across agents (different DOB / name / employer / income).
* `risk_signals[]` — cross-block signals only the reconciler can see.
* `field_overrides{}` — which value the engine should use when two agents disagree.
* `overall_confidence` — 0.0-1.0.
* `stp_recommendation` — `STP_OK | STP_CONDITIONAL | STP_BLOCK`.
* Three short one-line rationales: `financial_recommendation`, `medical_recommendation`, `overall_recommendation` (≤180 chars each, no new facts, no band/decision keyword).

### 15.2 Risk signal codes — financial

| Code | Trigger |
|---|---|
| `UNDISCLOSED_EXISTING_COVER` | Bank statement shows insurance ACH debits not declared. |
| `INCOME_SOURCE_MISMATCH` | Declared occupation ≠ where the money actually comes from. |
| `BANK_CREDITS_BELOW_DECLARED_INCOME` | Annualised bank credits < 60 % of declared income (outside [0.60, 0.95]). |
| `FOIR_BREACH` | Total EMIs / annual income > 50 %. |
| `BUSINESS_LOSS_LATEST_YEAR` | Audited financials show net loss in latest FY. |
| `SELF_TRANSFER_INFLATION` | Large bank credits are self-transfers. |
| `CIRCULAR_BUSINESS_KEYMAN_LOOP` | Keyman proposed for a director with no audited financials. |

### 15.3 Risk signal codes — KYC / identity / AML

| Code | Trigger |
|---|---|
| `IDENTITY_DOC_MISMATCH` | PAN / name / DOB don't match across agents. |
| `KYC_INCOMPLETE_VS_HIGH_SA` | KYC partial/pending while case is >₹25 Cr. |
| `NRI_TIER3_COUNTRY` | Applicant is NRI in a Tier-3 jurisdiction. |

### 15.4 Risk signal codes — medical

`COTININE_SMOKER_MISMATCH`, `MEDICAL_DOC_MISSING`, `BMI_OUT_OF_RANGE`, `HBA1C_PREDIABETIC` (5.7-6.4 %), `HBA1C_DIABETIC` (≥6.5 % or RBS ≥200), `BP_STAGE1_OR_HIGHER` (≥140/90 not declared), `ANEMIA`, `LIVER_ENZYMES_ELEVATED`, `LIPID_PROFILE_ABNORMAL` (TC ≥240 / LDL ≥160 / HDL <40 M, <50 F), `KIDNEY_FUNCTION_IMPAIRED` (creatinine > ref, BUN >25), `FAMILY_HISTORY_HIGH_RISK` (first-degree relative, early-onset CAD/stroke/cancer; M<55 / F<65), `MER_VMER_CONTRADICTION`, `ECG_ABNORMAL`, `TMT_POSITIVE_FOR_ISCHEMIA`.

### 15.5 Severity scale

* `critical` — case must be DECLINED or sent back (PAN mismatch, undisclosed >₹1 Cr cover, FOIR > 100 %).
* `high` — gating concern (FOIR 50-100 %, undisclosed <₹1 Cr cover, name mismatch).
* `medium` — note for UW; doesn't block STP.
* `low` — informational.

### 15.6 Brand-name leak prevention

Reconciler rule 9: the words `Claude`, `Anthropic`, `Gemini`, `OpenAI`, `GPT` and any model version string **must not appear** in any output text. Dual-vendor reads referred to as **Primary** and **Secondary**. QC layer catches leaks post-hoc.

---

## 16. Gating

`engine/gating.py` — every check returns PASS / WARN / FAIL / NA:

| Criterion | Rule |
|---|---|
| `income_documented` | Rungs 1-5 PASS; rung 6 WARN; rung 7 FAIL; `annual ≤ 0` FAIL. |
| `min_income_for_sa` | SA-tier income floor (§ 4.5). |
| `residential_status` | RI variants → PASS; NRI → WARN; missing → WARN. |
| `kyc` | Verified → PASS; Partial/Pending → WARN; Failed → FAIL. |
| `foir` | (§ 5). |
| `sa_within_hlv` | proposed_sa ≤ hlv_base → PASS; > → FAIL; HLV = 0 → FAIL; SA = 0 → WARN. |
| `existing_cover_disclosed` | Reconciler `UNDISCLOSED_EXISTING_COVER` critical → FAIL, else WARN; none → PASS. |
| `medical_compliance` | ≥100 % PASS, ≥75 % WARN, else FAIL. (Skipped if no medical block.) |
| `fraud_signal` | LOW PASS, MODERATE WARN, HIGH FAIL. |

---

## 17. Flag Taxonomy

`engine/flags.py` (deterministic over calculations + reconciliation + identity-verification + dual-vendor diff). All flags also surface in the verdict's decision drivers.

### 17.1 Severity ranking

`CRITICAL → HIGH → MEDIUM → LOW`. Flags sorted by severity in `derive_all_flags`.

### 17.2 Engine-emitted flag codes

| Code | Severity | Trigger |
|---|---|---|
| `FOIR_OVER_100` | CRITICAL | FOIR ≥ 100 %. |
| `INCOME_NOT_ESTABLISHED` | CRITICAL | `selected_income ≤ 0`. |
| `INCOME_SANITY_HIGH` | HIGH @≥₹5Cr / MEDIUM @₹2-5Cr | Income ≥ ₹2 Cr and not from FX or audited P&L. |
| `COTININE_SMOKER_MISMATCH` | CRITICAL | Fraud risk = HIGH. |
| `BMI_OBESE` | HIGH | BMI ≥ 30. |
| `BMI_UNDERWEIGHT` | MEDIUM | BMI < 18.5. |
| `BMI_HANDWRITTEN_DIGITS` | LOW | Always when BMI > 0 and vitals available (with bonus note when near band boundary). |
| `VITALS_REOCR_DISAGREEMENT` | MEDIUM @≥2 % / HIGH @≥10 % | Vitals re-OCR delta. |
| `MULTIPLE_LAB_ABNORMALITIES` | HIGH | ≥ 5 lab abnormalities. |
| `MEDICAL_RISK_HIGH` | HIGH | medical_risk = HIGH. |
| `MEDICAL_DOCS_INCOMPLETE` | HIGH | Test compliance < 50 %. |
| `FAMILY_HISTORY_REOCR_DISAGREEMENT` | MEDIUM | Family-history set diff. |
| `MEDICAL_DUAL_VENDOR_DISAGREEMENT__<FIELD>` | per `_SEVERITY_BY_FIELD` | Per-field dual-vendor diff. |
| `DC_DISTANCE_HIGH` | MEDIUM | DC ≥ 50 km. |
| `DC_DISTANCE_NOT_DETERMINED` | LOW | Geocoding miss. |
| `LIVE_PHOTO_MISSING` | MEDIUM | No live-photo file located. |
| `PHOTO_MATCH_FAIL` | HIGH | Gemini face-match = no-match. |
| `PHOTO_MATCH_REVIEW` | MEDIUM | Partial-match / undetectable. |
| `GATE_<CRITERION>` | HIGH (FAIL) / MEDIUM (WARN) | Lifted from gating items. |
| `RECON_<CODE>` | CRITICAL/HIGH/MEDIUM/LOW | Lifted from reconciler `risk_signals[]`. |
| `FX-STALE` | MEDIUM | FX rates > 7 days old. |
| `EE-COMPANY-MISSING` | HIGH | EE policy with no company financials. |

---

## 18. Verdict (Decision Tree)

`engine/verdict.py:make_verdict` — deterministic.

### 18.1 Risk dials

* **Financial risk**: derived from gating + flag counts.
  * `gc.FAIL ≥ 1` OR `fc.CRITICAL ≥ 1` OR `fc.HIGH ≥ 4` → HIGH.
  * `fc.HIGH ≥ 1` OR `fc.MEDIUM ≥ 3` → MODERATE.
  * Else → LOW.
* **Medical risk**: see § 9.2.
* **Fraud risk**: see § 11.
* **Overall risk** = max of (financial, medical, fraud) by rank.

### 18.2 Verdict label (decision cascade — first match wins)

| Outcome | Trigger |
|---|---|
| **DECLINE** | fraud_risk = HIGH **OR** ≥ 1 CRITICAL flag **OR** `proposed_sa > hlv_base × 1.05` **OR** medical_risk = HIGH + ≥ 1 gating FAIL. |
| **REFER** | ≥ 2 gating FAILs **OR** medical_risk = HIGH **OR** ≥ 4 HIGH flags. |
| **CONDITIONAL** | 1 gating FAIL **OR** ≥ 1 HIGH flag **OR** `stp = STP_BLOCK` **OR** medical_risk = MODERATE **OR** fraud_risk = MODERATE. |
| **STANDARD_ACCEPTANCE** | No FAILs, no HIGH/CRITICAL, medical + fraud LOW. |

### 18.3 Confidence

Weighted by reconciler confidence and gating PASS-share:
* STANDARD: `0.5 × recon_conf + 0.5 × pass_share`.
* CONDITIONAL: `0.4 × recon_conf + 0.4 × pass_share + 0.14`.
* REFER: `0.3 × recon_conf + 0.3 × pass_share + 0.24`.
* DECLINE: `0.6 + 0.4 × (1 − recon_conf)`.

### 18.4 14-cell Final Decision Matrix (Datamatics LAYOUT rows 71-77)

`engine.verdict._classify_final_decision_cell` returns one of 14:

```
{Std Smoker, Std NSR, Sub-Std Rated Up Smoker, Sub-Std Rated Up NSR} ×
{full, restricted, stretched}
+ Postpone + Decline
```

* **Cover-level**:
  * `proposed_sa ≤ 0` OR `hlv_base ≤ 0` → `full`.
  * `proposed_sa > hlv_base × 1.00` → `stretched`.
  * `proposed_sa < hlv_base × 0.7` → `restricted`.
  * Else → `full`.
* **Smoker resolution**: `habits_declared.smoker = "yes"` OR cotinine positive.
* **Sub-Std (rated up)**: `loading.emr > 0`, or `verdict = CONDITIONAL` with medical_risk = MODERATE.

---

## 19. External Data Joins

### 19.1 Policy_Data.xlsx (`engine/policy_data.py`)

* Path: `POLICY_DATA_XLSX` env var or explicit arg.
* Indexed by `PLCY_NUM` (case-id format after stripping numeric folder prefix).
* Loads ~68 columns including nominee, sum-assured grid (SAR / MSAR / FSAR / TSAR / TRM_TSAR), 7 bureau ACM scores (ECM, PB, AUREUS, CRIF, EXP, TU, PAYU — each with income min/max/avg where present), `FINANCL_WAVER_FLG`, `Health_Rider_Flag`, `ADVRS_HLTH_FLG`, husband-housewife linkage, channel/agent, `APP_STATUS` (UW ground truth), annual premium/term, plan code, identity cross-check fields.
* Cached process-wide; 102 rows × 68 cols is tiny.

### 19.2 IIB / prior-policy ledger (`engine/iib.py`)

* **Currently a stub** — feed not on file (`project_iib_history_gap`).
* Path: `IIB_DATA_JSON` env var.
* Schema in `engine/iib.py` docstring.
* When feed lands: indexed by PAN (preferred) > policy_number; surfaces by-status counts (Inforce / Postpone / Decline / Void / Pending UW).

### 19.3 Alpha Policy ledger (`engine/alpha_policy.py`)

* **Currently a stub** — TALIC prior-policy ledger.
* Path: `ALPHA_POLICY_JSON` env var.
* Match priority: PAN > `HUSBAND_HOUSEWIFE_PLCY_NUM` (Policy_Data field) as a coarse signal.

### 19.4 Master files

`reference_master_files` memory note: TALIC Master-File at `D:\107_APA\TALIC\Master-File\` supplies:

* 52K-companies xlsx → `config/top_52k_companies.json`.
* 248 NRI countries with FATF → `config/nri_countries.json`.
* 7K pincode-rank lookup → `config/pincode_top_locations.json`.
* Medical lab reference ranges → `config/medical_lab_ranges.json`.

### 19.5 Datamatics LAYOUT spec

`Templates\feedback\DATAMATICS- LAYOUT.xlsx` — authoritative case-sheet format from TALIC operator.
* FORMAT sheet → 70-row layout spec.
* Sheet1 → 28 defects across 13 named cases.

---

## 20. Rate Limiting & Performance

`uw_rules.rate_limiting`:

| Setting | Default |
|---|---|
| `max_concurrent_requests` | 5 |
| `max_concurrent_per_case` | 3 |
| `max_retries` | 5 |
| `base_delay_sec` | 2.0 |
| `max_delay_sec` | 60.0 |
| `min_request_gap_ms` | 200 |
| `jitter` | true |
| `azure_tpm_limit` | 80 000 |
| `azure_rpm_limit` | 60 |

Tune against the live Azure deployment quota.

---

## 21. Known Data Gaps (Non-Goals — these are surfaces the engine **cannot** see today)

| Gap | Memory note | Impact |
|---|---|---|
| **IIB feed not on file** | `project_iib_history_gap` | Cross-insurer existing-cover, prior declines / postpones / voids invisible. ACM band defaults to "Pending". Verify before treating a discrepancy as a bug. |
| **Alpha Policy ledger not on file** | (stub) | Datamatics rows 20/36/53 render placeholder; HHW field used as coarse linked-policy signal. |
| **M/F checkbox flips persist on TATA AIA MER** | `feedback_handwritten_dual_vendor`, `project_c308394654_family_history` | All three current passes (Claude full, Gemini full, focused re-OCR) get the C308394654 case wrong. Mitigation: template-anchored extraction (§ 6.4) for the standard form; per-cell isolation (§ 6.5) for residual cases. |
| **MER template atlas** | `project_mer_template_atlas` | Atlas at `config/mer_template_atlas.json` is for the single TATA AIA MER variant. Other forms degrade to non-template extraction. |
| **MSAR not on Policy_Data sample** | (current screenshot) | Several column-name mismatches (FINANCL_WAIVER_FLG vs FINANCL_WAVER_FLG, ECR_SCORE vs ECM_SCORE, COUNT_COUNTRY vs CURR_COUNTRY) — verify Policy_Data column headers exactly match `engine/policy_data.py` before batch run. |

---

## 22. Where the Numbers Live (Tuning Map)

| Want to change… | Edit file |
|---|---|
| HLV multipliers | `config/uw_rules.json` → `hlv_grids.*` |
| FOIR threshold | `config/uw_rules.json` → `foir_threshold_pct` |
| Per-life cap | `config/uw_rules.json` → `max_per_life_crore` |
| SA-tier income floors | `config/uw_rules.json` → `gating.sa_tier_min_income` |
| Top-companies SA shortcut | `config/uw_rules.json` → `top_companies` |
| FX rates | `config/fx_rates.json` (refresh quarterly) |
| Required tests by SA×age | `config/medical_rules.json` → `MEDICAL_GRID.requirements` |
| VMER eligibility | `config/medical_rules.json` → `VMER_GRID_RI / NRI / TATA_GROUP_EMPLOYEE / CHANNEL_SPECIFIC` |
| Lab reference ranges | `config/medical_lab_ranges.json` |
| Country masters / FATF | `config/nri_countries.json` |
| Cotinine threshold | `agents/prompts/medical.txt` (10 ng/mL hard-coded as default in agent; `medical.fraud_signals.cotinine_threshold`) |
| BMI bands | `config/uw_rules.json` → `bmi_bands` |
| EMR rules | **Code** — `engine/loading.py` (no config — by design, rules are clinical) |
| Health-score weights | **Code** — `engine/medical.compute_health_score` |
| Verdict thresholds | **Code** — `engine/verdict.make_verdict` |
| Gating PASS/WARN/FAIL bands | **Code** — `engine/gating.py` |
| Standard-rated conditions list | **Code** — `engine/medical._STANDARD_RATED_CONDITIONS` |
| Photo-match severities | **Code** — `engine/flags._flags_from_identity_verification` |
| MER template atlas | `config/mer_template_atlas.json` (regenerate from blank form) |
| Product catalogue | `config/products.json` |

---

## 23. Review Checklist (use this when reviewing with codex / SMEs)

1. **HLV grids** — § 4.1-4.3. Confirm the three multiplier grids match the Term UW Feb 2026 booklet exactly.
2. **SA-tier income floor** — § 4.5. Confirm each band's income floor matches the latest underwriting circular.
3. **FOIR threshold** — § 5. Currently 55 % — is that right for the next batch's product mix?
4. **Income cascade rungs** — § 3.1. Should `bank_credits` stay at WARN, or move to PASS for the next batch?
5. **Income consistency bands** — § 3.5. Are the 0.95 / 1.05 / 1.40 boundaries correct?
6. **Medical grid** — § 7.1. Spot-check three MSAR × age cells against the underwriter's master.
7. **VMER skip-rule** (PB-channel) — § 7.2. Confirm `≤ ₹2.25 Cr` boundary.
8. **Cotinine threshold** — § 11.1. Currently 10 ng/mL — confirm.
9. **Health-score weights** — § 9.1. Are −0.7 per ratable condition and −1.5 for declared smoker correct?
10. **Verdict cascade** — § 18.2. Confirm the order (DECLINE → REFER → CONDITIONAL → STANDARD) matches Datamatics expectations.
11. **14-cell decision matrix** — § 18.4. Confirm the cover-level boundaries (`× 0.7` and `× 1.00`).
12. **ACM 5-actions** — § 13.4. Confirm CPR-exempt channel list (esp. "PB PARTNERS not exempt" footnote).
13. **NRI decline list** — § 12.5. Confirm the 48 countries in `config/nri_countries.json` against TALIC's latest country master.
14. **Loading rules** — § 10. Confirm EMR amounts (+25 / +50 / +100) for each rule.
15. **Standard-rated conditions** — § 9.1. The 30+ conditions in `_STANDARD_RATED_CONDITIONS` are not penalised in health score — confirm the list.
16. **Dual-vendor field list + severities** — § 6.6. Confirm which fields are HIGH (smoker, cotinine, ecg.status, tmt.recommendation, condition_set).
17. **Medical UW Terminal Decision matrix** — § 9.3. Six outcomes — confirm the trigger conditions match LAYOUT row 70.
18. **Brand-name leak prevention** — § 15.6. Confirm QC sanitiser covers `Claude`, `Anthropic`, `Gemini`, `OpenAI`, `GPT`.
19. **Policy_Data column-name mismatches** — § 19.1 + § 21 last row. Reconcile xlsx headers before batch.
20. **IIB feed status** — § 19.2 / § 21. Confirm we run without IIB and that the engine surfaces this gap visibly (it does — ACM band → "Pending"; reconciler still produces verdict without IIB).

---

*Last regenerated:* 2026-05-26 from the v9 codebase head.
*Single source of truth precedence:* code > config > this doc. When this doc disagrees with the code, the code wins — file a bug.
