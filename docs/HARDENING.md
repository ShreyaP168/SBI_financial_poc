# Release 1.2.0 hardening

The controlled POC now uses namespaced packages, canonical audit validation and
delivery policy `delivery-3.0`. Required gate names and routing are shared domain
definitions. Root shims and TruCap remain removed.

Extraction revisions include full case-file hashes, packaged implementation,
prompts, recursive JSON configuration, provider settings and all declared external
sources: policy workbook, bureau document, both score workbooks, IIB workbook,
SBI bureau JSON, IIB JSON and alpha-policy JSON. Optional sources contribute their
configured path and presence/absence as well as contents. Reference indexes are
refreshed on cache misses. Inputs changing during processing prevent cache
publication. Successful cache records retain frozen source/reference evidence and
raw reports; failed extractions are never cached. Transient extraction errors are
retryable; deterministic provider rejection requires corrected evidence or review.

Model parse/retry logs contain safe categories and correlation metadata. Refusal,
truncation, authentication and invalid-request failures do not repeat the same
request. Only rate limits, network failures and retryable HTTP statuses retry.
Reconciler suggestions preserve every extracted fact and its original provenance.

Malformed canonical fields, audit structures, missing calculations/gates/citations
and issuance with a failed gate block delivery. Services recompute completeness
instead of trusting transported `_completion`. The shared delivery adapter handles
CLI and service rendering, QC, retention and diagnostic masking. Service artifacts
snapshot frozen extraction evidence and preserve the input fingerprint.

Checkpoint JSON stores only small stage references and completion summaries.
`workflow_payloads` holds stage payloads and is loaded only when a worker claims
the job. Raw agent reports stay in extraction storage, outside queue checkpoints.
Existing inline checkpoints migrate transactionally during initialization; old data remains in
retention plans. Reproduction bundles hardlink content-addressed assets where the
filesystem supports it; copy fallback is explicit. All references must be included
in object garbage collection.

Heartbeat dependency errors requeue with bounded retries. If the database cannot
persist failure, the lease expires and is reclaimed. Lost ownership cannot publish
checkpoints/results. Gateway submission requires a stable idempotency key, and
artifact delivery rejection remains HTTP 409. Queue admission is bounded; safe
stage-duration, lock-wait and queue-age metrics support capacity measurements.

See [production security, lifecycle and scaling prerequisites](PRODUCTION_PREREQUISITES.md).
Local synthetic regressions exercise behavior; production provider throughput,
tenant enforcement, encrypted storage and lifecycle execution require deployment
evidence. No hosted CI execution is claimed by a local build.
