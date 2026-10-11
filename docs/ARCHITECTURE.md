# Underwriting platform architecture

## Scope and deployment status

The implemented SBI pipeline is a controlled POC with human review. Production
mode is blocked pending tenant authorization, managed secrets, verified encrypted
storage, access auditing, retention/deletion and measured capacity. Configuration
and carrier rules live under `src/underwriting_platform/configuration` and
`resources/config`. An explicit development secret file must be outside Git.
Key Vault support exists; this checkout has no configured vault or administrator.

## Processing flow

CLI and batch call the master orchestrator, deterministic engine and shared
`infrastructure/delivery.py` boundary. The gateway enqueues an idempotent request
in PostgreSQL; the leased worker calls extraction, underwriting and artifact HTTP
services using versioned envelopes and canonical boundary checks. Job status,
compact checkpoints and result references are durable. Status polling does not
reload full extraction payloads. Lease renewal and transient failures are handled
by the worker; deterministic incomplete cases retain review artifacts.

Extraction collects sources, hashes full files, conservatively deduplicates copies,
prepares native text and bounded vision pages, and surveys document relevance.
Both classification stages use [local OCR/content rules](LOCAL_CLASSIFICATION.md)
without paid classification calls. Unknown content remains explicit for review;
document labels do not verify KYC. Extraction and photo matching remain separate.
Scanned pages also pass through the [SBI Bond local OCR adapters](LOCAL_OCR.md)
(Tesseract by default or optional RapidOCR/ONNX), retaining page/line confidence
and geometry. OCR candidates are distinct from native text; vision is retained.
`UW_EXTRACTION_MODE` defaults to [local-first extraction](LOCAL_FIRST_EXTRACTION.md)
for every client, including test doubles. Shared local facts replace repeated
cloud document reads; optional cloud fallback is bounded to unresolved evidence.
Domain mode uses domain prompts and owned blocks; compliance can consume shared
structured context. `source_once` is explicitly experimental: one reader owns each
selected document and emits the applicable canonical domain blocks, which are
partitioned into domain views. It retains the complete selected domain prompts,
not just their JSON examples. It skips the paid cross-agent reconciler because
those views are not independent extractions. Neither mode makes underwriting
decisions in the LLM. Domain-mode reconciler overrides cannot rewrite facts.

Structured workbook and bureau joins run with configured external inputs. Identity
classification and photo matching are separate mandatory/advisory checks according
to delivery policy. No TruCap integration remains. Missing or conflicting source
evidence stays explicit; native evidence repairs require an exact labeled match.

## Budgets, caching and recovery

Each case has a 200,000-token default input/output/cache budget shared by Azure
and Gemini. Counting and reservations happen before paid generation. Actual usage
replaces reservations; explicit HTTP rejections release reservations, while
ambiguous interruptions remain conservatively charged. Spend lives in a locked,
case-keyed ledger outside extraction revisions. Identifiable legacy revision
balances are migrated conservatively; code/prompt edits and new submissions do
not reset spend. Identity usage is persisted in the same ledger. Increasing a limit
does not change the extraction revision or throw away successful stage checkpoints.
The user-approved limit remains 200,000; budget-limited cases are INCOMPLETE.

Extraction revisions cover source bytes, extraction code, prompts, relevant
configuration and all configured external evidence/reference files. Renderer and
gateway edits do not invalidate extraction. Successful source/agent batches are
integrity-checked stage checkpoints. Missing citations on known critical applicant
facts reject both new and legacy checkpoint reuse. Failed results are not durable successful
cache entries. Processing errors, source findings or contract issues prevent
publication in the successful extraction cache. A partial result retains useful
facts and diagnostic artifacts; it is not decision-complete. In experimental
source mode one failed batch may affect all represented domains. Transient errors
are retryable, including wrapped identity network errors; truncation/refusal is not
retried with an identical request. Intermediate retryable attempts stop before
engine/render/QC/evidence import; final exhausted attempts retain one review artifact.

## Decisions, QC and delivery

The deterministic engine applies carrier rules, gates and calculations.
`application/completion.py` requires critical typed facts, source provenance,
required gates and rule citations. DOB-derived entry age is reconciled without
discarding the diagnostic result for ordinary age drift. Hard eligibility failures
block issuance; missing documentation is distinct from confirmed adverse facts.
Contract validators validate data and do not implicitly rewrite decisions.

Shared delivery retains inputs, renders the supplied reference-format sheet,
checks QC and writes a release marker. Decision completeness and PDF QC are
separate: a QC PASS alone does not authorize delivery. Infrastructure errors and
mandatory failures fail closed and produce non-zero CLI status. Normal downloads
enforce completion and QC at both gateway and artifact boundaries. Diagnostic
HTTP download routes are removed. Public status hides diagnostic verdicts/paths;
reviewers use a separate `UW_REVIEWER_TOKEN` at
`GET /v1/runs/{job_id}/review-case-sheet`. The artifact service requires the same
separate reviewer credential and returns a watermarked, non-deliverable copy.
Normal client/service tokens cannot use this review route. It is disabled when
the reviewer credential is unset; this POC role token is not tenant authorization.

## Evidence and storage

Extraction freezes source and reference evidence under its revision. Artifacts
import it through an authenticated internal HTTP handoff into independent storage;
there is no shared extraction/artifact volume. The POC handoff still uses base64
JSON and is limited to 64 MiB of source/reference bytes. Larger cases require a
pre-extraction size rejection with `EVIDENCE_TOO_LARGE` (413), not a retryable
artifact dependency failure. Intake counts configured reference files as well as
case source documents. Larger-scale cases require a
streaming/object-store design before deployment; this is not horizontally scalable
storage. Reproduction bundles retain normalized/raw inputs and versioned model,
prompt, parser, rules and QC metadata. Content-addressed retained objects reduce
duplicate snapshots. Lifecycle and encryption remain deployment prerequisites.

## Capacity and validation

Extraction/artifacts run one process and one instance with process-local locks.
More workers do not increase extraction capacity; queue age and stage throughput
must be measured before changing replica count. Shared service/client bearer
tokens do not implement user or tenant isolation.

Local regression and Docker tests exercise service and failure paths; they do not
establish live extraction accuracy or prove hosted CI ran. Four SBI cases have
source folders here; three reviewed cases do not. The live source-once run produced
one decision-complete case, one evidence-blocked case and two budget-limited cases.
A seven-case paired live comparison has not passed. Keep source-once opt-in until
the full source set and both extraction modes meet field/provenance/gate/verdict
criteria within the agreed budget. See `TOKEN_USAGE.md` and `FOLLOWUP_AUDIT.md`.

The published `f04dfe9` hosted run did execute: Python 3.11/3.13 passed and the
service job failed because Compose rejected a worker with its health check disabled.
The worker now maintains a database-backed readiness marker for Compose's real
health check. See `RECOVERY_AUDIT.md` for current validation and outstanding measurement.

See [services](SERVICES.md), [coding standards](CODING_STANDARDS.md),
[production prerequisites](PRODUCTION_PREREQUISITES.md) and the
[April historical design](history/ARCHITECTURE_2026-04.md).
