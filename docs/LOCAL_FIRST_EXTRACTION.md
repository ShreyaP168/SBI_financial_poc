# Local-first extraction

`UW_EXTRACTION_MODE=local_first` is now the consistent default. Legacy `domain`
and experimental `source_once` are explicit comparison modes, not automatic
fallbacks. Each document is read locally and feeds one shared fact/provenance
record; domain views consume that record without rereading it through separate LLMs.
Unknown classifications no longer activate assets/business agents without evidence.

Supported parsers: explicit applicant/business labels; full DOB; age/gender;
Indian currency units; dated gross salary slips; ITR-1 salary and GTI with
assessment-year deduplication; bank salary credits from explicit credit columns
with at least three distinct months. Income calculations reuse deterministic
engine functions. GTI is not treated as earned salary, balances are not credits,
and declared income is not verified income. Conflicting values stay unresolved.

Native PDF text/tables are preferred. OCR `auto` selects installed RapidOCR,
otherwise Tesseract, with a local Tesseract fallback. Optional PP-StructureV3
handles numerical/table-rich scans when `UW_PADDLE_CONFIG` is configured.
Install `.[paddle-ocr]` separately and provision models before processing. Its
Paddle pipeline YAML must specify existing local `model_dir` directories; missing
model configuration fails closed. The adapter is tested against representative
PP-Structure output; live Paddle inference has not been validated in this checkout.
It is not included in the default lightweight container dependency set.

```dotenv
UW_EXTRACTION_MODE=local_first
UW_LOCAL_OCR_PROVIDER=auto
UW_TARGETED_LLM_FALLBACK=false
UW_TARGETED_VISION_FALLBACK=false
# UW_PADDLE_CONFIG=/absolute/path/to/local-paddle-pipeline.yaml
```

Cloud clarification is opt-in. It sends only unresolved supported scalar fields,
at most 18,000 characters of cited section windows and 3,500 output tokens in one
request per extraction invocation. Optional vision adds at most two small label/
value crops (1000x250 maximum), not whole pages. Crop-only facts require review.
Quotes must match sent text; invented documents/pages, normalization mismatches,
overrides of local facts and contradictory responses cannot silently replace facts.
The cumulative token ledger still bounds all retries/invocations; no budget reset
or additional allowance is implicit in enabling fallback.

Unsupported medical, rider, nominee, insurance-purpose and cover declarations
are explicitly pending review. No screening or authenticity clearance is inferred.
Case delivery remains fail-closed; local extraction does not mean all case fields
are supported or that an underwriting decision is complete. Partial local cases
skip paid photo matching. Screenshot bureau extraction and paid reconciliation
are skipped in local-first mode; structured reference joins remain available.

Offline check on 4CPB014070: three local fields found, zero targeted/cloud calls,
and core/compliance/medical facts remain unresolved. Results are private under
`data/results/local_first_smoke`. This is not a passed case or a measured paid
end-to-end saving. Real source formats require additional parser/template coverage.

Validation: 313 regression tests passed and the rebuilt Docker/PostgreSQL
workflow passed, including both offline OCR engines and blocked/reviewer artifact
checks. No additional paid case run was performed. Paddle adapter output handling
is covered by a representative backend fixture, not live installed model inference.
