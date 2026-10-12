# Local PaddleOCR and bounded cloud fallback

Use an isolated Python 3.12 CPU environment. Install the document-parser extra:

```powershell
python -m venv ../.tools/paddle-venv
../.tools/paddle-venv/Scripts/python.exe -m pip install -e '.[paddle-ocr]'
../.tools/paddle-venv/Scripts/python.exe -m underwriting_platform.ocr.provision_paddle
```

Provisioning explicitly downloads official OCR, layout and table models; it
does not read applicant documents. Formula, chart, seal and region models are
disabled. Model directories are written into `data/runtime/paddle/pipeline.yaml`;
the manifest records package versions and SHA-256 model file hashes. Preserve
these files when reproducing extraction. Provisioning must finish before use.

Set these nonsecret values in the external provider-settings file, outside Git:

```dotenv
UW_EXTRACTION_MODE=local_first
UW_LOCAL_OCR_PROVIDER=auto
UW_PADDLE_CONFIG=C:/absolute/workspace/data/runtime/paddle/pipeline.yaml
UW_TARGETED_LLM_FALLBACK=true
UW_TARGETED_VISION_FALLBACK=false
```

Run cases with the environment containing PaddleOCR and the application
dependencies. Setting `UW_LOCAL_OCR_PROVIDER=paddle_structure` forces Paddle
on scanned pages; `auto` retains the cheaper lightweight OCR path and uses
Paddle for numeric/table-rich scans. Native PDF text remains preferred.

Fallback sends one bounded request per extraction invocation for unresolved
supported scalar fields, including employer/trade evidence. It sends cited text
windows (at most 18,000 characters), permits at most 3,500 output tokens, and
never overwrites local facts. Incorrect citations, malformed fields and invalid
normalizations are rejected. Existing cumulative case budgets still apply,
including provider transport retries. Enabling fallback grants no extra budget.
Complex medical/financial interpretations remain pending review when unsupported;
cloud fallback does not guarantee a decision-complete case.

Official pipeline reference:
[PaddleOCR PP-StructureV3](https://paddlepaddle.github.io/PaddleOCR/main/en/version3.x/pipeline_usage/PP-StructureV3.html).

## Checkout validation, 12 October 2026

The isolated Python 3.12 CPU environment installed PaddleOCR 3.7.0,
PaddlePaddle 3.3.1 and PaddleX 3.7.2. Official models were provisioned locally.
A synthetic applicant/salary-table image returned nine text lines and one table
with external network connections blocked and zero cloud tokens. Windows
oneDNN produced an unsupported PIR attribute error; the adapter now explicitly
disables oneDNN. This validates local OCR/table inference, not a complete real
underwriting case. Cloud fallback is configured but no additional paid run was
performed.
