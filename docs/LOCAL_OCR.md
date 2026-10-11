# Local OCR

The adapters are adapted from the user's SBI Bond `sbil_qc_service`, using its
Tesseract TSV line grouping and CPU RapidOCR/ONNX full-page reader. Source hashes
are recorded in `ocr/PROVENANCE.txt`. There are no cloud OCR calls.

Configuration belongs in the external provider-settings file:

```dotenv
UW_LOCAL_OCR_PROVIDER=tesseract
OCR_LANGUAGE=eng
OCR_PSM=6
OCR_TIMEOUT_SECONDS=45
# Optional Windows override:
# TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
```

The default is `auto`: installed RapidOCR first, then local Tesseract. Docker
includes both providers and Tesseract English data. Optional Paddle structure
models require separate provisioning; see [local-first extraction](LOCAL_FIRST_EXTRACTION.md).
Both classification stages consume local text/OCR; see
[local classification](LOCAL_CLASSIFICATION.md) for routing and review rules.
SBI Bond's RapidOCR 1.4.4 requires Python 3.11/3.12; Python 3.13 supports the
Tesseract path. Containers use Python 3.12 to support both providers.
For RapidOCR on Python 3.11/3.12 install `pip install '.[local-ocr]'` and select
`UW_LOCAL_OCR_PROVIDER=rapidocr`. Use `off` to disable local OCR. Additional
Tesseract languages require installed `.traineddata` files; no models download
during applicant processing. RapidOCR uses the package's bundled ONNX models.

Native PDF text is retained. Pages with less than 80 characters of native text
are rendered at 150 DPI for local OCR, including pages beyond the initial vision
image limit. Each page is bounded to 25 million pixels. OCR candidates are page
tagged and carry engine/model, confidence, pixel boxes and image dimensions in
the private extraction report. They are never marked as native text or used for
automatic native-business evidence repair. The report records local pages and
zero paid provider tokens. OCR failure metadata excludes stderr and applicant text.

Vision is retained: local confidence alone does not prove that handwriting,
checkboxes or table structure were read correctly. A failed OCR page beyond the
available vision images blocks extraction rather than silently disappearing.
Local OCR is source transcription; canonical extraction, carrier rules and QC
still run as before. No token-saving estimate or new decision accuracy claim is
made for this integration.

The extraction revision covers OCR implementation, provider/language/PSM,
Tesseract executable and language assets, or bundled RapidOCR model bytes.
Changes invalidate extraction appropriately. Local OCR lines contain applicant
data; retain them only in the private evidence/extraction stores.

Tests cover TSV geometry/confidence, safe failures with retained vision,
render limits, page-number gaps, cache invalidation and a real scanned PDF.
The real-case local smoke output is stored outside Git under
`data/results/local_ocr_smoke`; it does not rerun paid extraction or underwriting.

Validation on 11 October 2026: 278 local regression tests passed. Both engines
recognized the synthetic income value in a network-disabled Python 3.12 container
(Tesseract 5.5.0 and RapidOCR 1.4.4); the Docker/PostgreSQL workflow passed.
The real Aadhaar-image smoke completed with both local providers and zero cloud
calls. These checks verify operation, not full transcription or decision accuracy.
CI now includes Python 3.12 and the offline engine check; this is not a claim that
the updated hosted workflow has run.
