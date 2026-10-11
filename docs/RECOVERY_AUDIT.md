# Recovery and deployment follow-up — 11 October 2026

| Finding | Correction / current limit |
| --- | --- |
| Default-domain live cost unmeasured | Still unmeasured after local OCR. All four available cases have prior accounted spend above the approved cap. A fresh paid measurement requires separate authorization; historical spend is retained. No completion or savings claim is made. |
| Uncited checkpoints repeat forever | Known applicant facts without valid citations reject legacy/new stage reuse and successful full-response caching. Evidence can be reread within an authorized allowance; no source citation is fabricated. |
| Code edits reset spend | Locked case-keyed ledgers are outside revision directories. Identifiable legacy revision balances are summed, including ambiguous reservations. The same ledger persists identity usage. |
| Oversized evidence fails after payment | Extraction intake and the CLI orchestrator check case plus configured reference bytes before model calls. Oversized evidence is a deterministic 413/EVIDENCE_TOO_LARGE, preserved across HTTP boundaries. |
| No API review channel | A separate reviewer credential permits `/v1/runs/{id}/review-case-sheet`. Normal credentials are denied. The copy is marked REVIEW ONLY / UNVALIDATED / NOT FOR DECISION DELIVERY; ordinary delivery remains blocked. |
| Identity network errors never retry | Retry policy follows exception causes and explicit status/retryability. Timeouts, transport failures, throttling and server errors retry; evidence/schema errors and budget exhaustion do not. |
| Retry attempts copy/render artifacts | Retryable intermediate attempts retain extraction checkpoints and stop before engine, artifact import, rendering and QC. Final exhausted attempts generate one incomplete review artifact. |
| Hosted CI never ran | Incorrect: run 38150334136 executed for f04dfe9. Python 3.11 and 3.13 passed; services failed because the worker had no health check. A real worker readiness probe corrects that failure. |

Original hosted run: https://github.com/ShreyaP168/SBI_financial_poc/actions/runs/38150334136

Recovered local accounting is in the private workspace file
`data/results/recovery_budget_audit.json`. These figures combine prior charged
and conservatively reserved tokens across revisions, not a new live measurement:
The four cases account for 887,839; 264,197; 375,309; and 352,075 tokens. They need not
equal provider invoices; pre-ledger calls may be absent and interrupted calls are
conservatively reserved. No new paid calls were made during this audit.

The role token is a controlled-POC reviewer separation, not tenant/user production
authorization. Configure a distinct `UW_REVIEWER_TOKEN` in the external Compose
configuration; gateway and artifacts receive it, while extraction/worker do not.
Do not reuse client/service credentials. Review access is correlated in HTTP logs,
but named user auditing and the broader production prerequisites remain open.

Tests: `tests/test_recovery_policy.py` covers legacy migration across code/prompt
revisions, uncited checkpoint repair, pre-payment size rejection, transient identity
causes, final-only artifact creation and reviewer-only watermarked access.
Docker smoke also verifies normal-client denial and reviewer success for an
INCOMPLETE case. Three reviewed case folders still lack source documents; this
work does not constitute a seven-case live accuracy pass.

Local validation: 284 full-suite tests passed, followed by 46 focused tests after
the final successful-cache publication guard. Ruff passed. The rebuilt Docker
workflow passed with a healthy worker, both offline OCR providers and the new
reviewer-only incomplete-sheet checks; provider calls were zero.
