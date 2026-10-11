# v9 PDF template — change spec

> Drop reference images / annotated PDFs into this folder and reference them
> by filename in the sections below. The renderer lives at
> `renderer/pdf.py`; each section listed here maps to one `_section_*`
> function inside that file.

## Reference files in this folder

- `current_v9.pdf`            — what we ship today (regenerate via `python -m underwriting_platform.renderer.pdf` after changes)
- `reference_datamatics.pdf`  — the look we want to reach
- `<your_screenshot>.png`     — drop screenshots/annotations here as you write rules below

## How to write a change rule

Each rule should answer three questions:

1. **Which section?**  Use the renderer function name when you can — e.g. `_section_risk_dials`, `_section_lab_results`, `_section_verdict`.
2. **What change?**     Be concrete. "Make dials circular, 14 mm diameter, ring-only when band = N/A" beats "make them prettier".
3. **What NOT to change?** Helps me avoid accidental drift.

Optionally cite a screenshot: `(see `dial_target.png`)`.

## Changes

### Section: header / title bar  (`_section_header`)
- TBD

### Section: Risk Assessment Summary  (`_section_risk_dials`)
- TBD

### Section: Verdict box  (`_section_verdict`)
- TBD

### Section: Applicant  (`_section_applicant`)
- TBD

### Section: Calculations & Provenance  (`_section_calculations`)
- TBD

### Section: Income detail  (`_section_income`)
- TBD

### Section: Assets & Liabilities  (`_section_assets`)
- TBD

### Section: Compliance & KYC  (`_section_compliance`)
- TBD

### Section: Medical Assessment  (`_section_medical`)
- TBD

### Section: Laboratory Results  (`_section_lab_results`)
- TBD

### Section: Disease-specific Questionnaires  (`_section_questionnaires`)
- TBD

### Section: Family Medical History  (`_section_family_history`)
- TBD

### Section: Personal Medical History  (`_section_personal_medical_history`)
- TBD

### Section: Reconciler — cross-agent findings  (`_section_reconciliation`)
- TBD

### Section: Gating  (`_section_gating`)
- TBD

### Section: Flags  (`_section_flags`)
- TBD

### Section: Provenance Appendix  (`_section_provenance_appendix`)
- TBD

## Don't change

- TBD — list anything here you want me to leave alone.

## Color palette in use

Defined in `renderer/pdf.py` as `_PAL` (Datamatics-style). Override here
if you want a different value:

| Token         | Current   | Used for                          |
|---------------|-----------|-----------------------------------|
| `success`     | `#22c55e` | LOW risk band, NORMAL lab status, PASS |
| `warning`     | `#f97316` | REFER verdict, HIGH severity      |
| `danger`      | `#ef4444` | DECLINE verdict, HIGH risk dial, FAIL, abnormal lab |
| `moderate`    | `#eab308` | CONDITIONAL verdict, MODERATE risk band |
| `primary`     | `#1a365d` | header / dark text                |
| `accent`      | `#3182ce` | accent fills                      |
| `bg`          | `#f8fafc` | section card backgrounds          |
| `border`      | `#e2e8f0` | grid borders                      |
| `gray1`       | `#f1f5f9` | table header rows                 |
| `muted`       | `#64748b` | meta-data                         |
