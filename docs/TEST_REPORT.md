# Test report: OCR-first extraction

## Summary

- Full suite: 404 tests, all passing (316 existed before this work, 88 were added).
- Lint (`ruff`) is clean on `src` and the project's lint set.
- Test data is synthetic. No applicant names, PAN, Aadhaar or policy numbers appear in `tests/`.
- Run: `python -m pytest -q` and `python -m ruff check src tests`.

## What the added tests cover

| File | Tests | Covers |
|---|---|---|
| `test_document_fields.py` | 45 | Proposal form layouts (three), PAN card (value after label and label after value, header text rejected), Aadhaar letter, passport MRZ, ITR computation layouts (assessee style, full ITR-4 form, bundled multi-year PDF), nominees, riders, declared policies, health declarations, KYC evidence, qualification mapping to guideline categories, name matching |
| `test_income_documents.py` | 19 | OCR row reconstruction, salary slip month and gross, ITR computation rows, business description, recurring pension credits, declared-only income, colon-labelled proposal, image read validation (fake client), single-slip handling, declared versus filed business nature |
| `test_identity_gate.py` | 5 | Identity verification runs unless extraction genuinely failed |
| `test_ocr_trigger.py` | 2 | OCR on pages with images and little native text |
| Existing files extended | 17 | Classification (photograph, other, continuation pages, new rules), local-first coordinator (unclassified documents) |

## What the tests do not prove

- The image (IDP) path is tested with a fake client. Real calls were exercised only by running the pipeline on two cases.
- Readers are pattern based and were written from ten observed cases. Other layouts may need new patterns.
- End-to-end runs need the `Input` folder and credentials, so they are not part of the automated suite.

## Evaluation against the corrected case sheets

Scripts: `scripts/sheet_truth.py` derives expected values from the corrected sheets, and `scripts/evaluate_cases.py` reruns each case and compares.

| Case | Sheet decision | Our decision | Compared fields correct |
|---|---|---|---|
| 4CCZ525812 | FURTHER REQUIREMENT | match | 5 of 7 |
| 4CCZ525814 | DECLINE | match | 7 of 7 |
| 4CCZ526214 | DECLINE | match | 7 of 7 |
| 4CCZ526688 | DECLINE | match | 7 of 7 |
| 4CPB014070 | FURTHER REQUIREMENT | match | 6 of 6 |
| 4CYL218379 | FURTHER REQUIREMENT | match | 6 of 7 |
| 4CYL294940 | DECLINE | match | 7 of 7 |

- Decisions matching: 7 of 7. Fields correct: 45 of 48.
- Every case still ends INCOMPLETE and not releasable: open review flags and unresolved identity fields remain.
- The seven cases were used to build the readers. Accuracy on other cases is not measured.

## Where extracted values came from

Of 118 field values with a source citation across the seven cases: native PDF text 83.9%, local OCR 12.7%, image read (IDP) 3.4%. The image read covered 2 of 418 pages. A separate bounded text fallback spent about 28,600 tokens and no final field cites it.

## Decisions depend on configuration

The petty-business decline for the kirana and sweet-business cases relies on entries in `sbi_smart_shield_rules.json`. The configuration itself notes these are client-confirmed equivalents and not literal text in the V02 guideline.
