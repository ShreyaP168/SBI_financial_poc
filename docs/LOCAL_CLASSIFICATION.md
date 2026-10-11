# Local document classification

The master survey and identity-document classifier use native PDF text or local
Tesseract/RapidOCR output with versioned rules in
`resources/config/document_classification.json`. Neither classification path
calls Azure Claude or Gemini. The obsolete cloud survey prompt is removed.

Documents retain per-page kind, identity label, evidence basis, rule version and
review flag. Known headers select relevant extraction domains. Conflicting types,
insufficient text and unsupported content remain unknown and eligible for
conservative routing; documents are never silently dropped. Filename hints do
not establish the local content classification.

Classification uses line mean OCR confidence when available; conservative
minimum-word confidence remains in the source evidence. Prepared labels are
reused by identity processing to avoid duplicate OCR.

Classification does not verify authenticity, clear KYC or upgrade
`compliance.kyc_status`. Unresolved identity classification blocks standard
delivery unless an explicit named identity-review approval exists. Delivery
policy is `delivery-3.1`. Photos without useful text remain unknown; photo
discovery and Gemini photo matching are separate.

Azure structured extraction/reconciliation and Gemini photo matching still
consume paid tokens. This removes paid classification calls, not all paid
pipeline calls. Cumulative case budgets remain unchanged.

The offline 15-file folder check made zero provider calls/tokens. Four document
categories were recognized: one benefit illustration, one proposal/form and two
video-verification reports. Eleven remained unknown, including photos and poor
or sparse scans. All 15 had at least one aspect requiring review. This is a
conservative routing check, not a classification-accuracy score or completed
decision. Private results are in `data/results/local_classification_smoke` outside Git.

Tests cover ID/financial/medical text, ID references in proposals, misleading
filenames, contradictory content, low-confidence OCR, absence of cloud calls,
OCR reuse, unchanged KYC and the standard-delivery review guard.

Validation: 302 full-suite tests passed, followed by 31 OCR/cache checks and 39
classification/delivery checks after the final cleanup and review propagation.
The rebuilt Docker service workflow passed with zero provider calls.
