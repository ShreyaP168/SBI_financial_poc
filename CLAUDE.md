# Working instructions

Read `docs/ARCHITECTURE.md` for the implemented flow. Historical design belongs
in `docs/history`; do not describe it as current behavior.

- Code is namespaced under `src/underwriting_platform`; keep application ports,
  domain policy, infrastructure adapters and HTTP services separate.
- Extraction defaults to `domain` for all clients. Experimental `source_once`
  requires explicit configuration and must retain domain interpretation rules.
  Domain views of one reader are not independent autonomous reasoners.
- LLMs extract facts; the deterministic engine applies carrier rules. Retain raw
  conflicting declarations and source provenance. Reconciler overrides are advisory.
- Classification uses local native text/OCR and versioned content rules. Never
  add a cloud fallback or treat a label as verified KYC. Unknown/mixed content
  requires review and must remain in the routing plan.
- Canonical contracts validate data; shared completion/delivery policy decides
  whether a decision can be released. QC PASS is not decision completeness.
- Never bypass QC/completeness for downloads. Diagnostic HTTP routes are removed;
  separate reviewer credentials permit watermarked review-only sheets through
  `/v1/runs/{job_id}/review-case-sheet`. Client delivery gates remain enforced.
- Preserve successful stage checkpoints, paid-spend accounting and the approved
  200,000-token cap across revisions and retries. Never reset the case ledger
  during deploys; exhausted budgets require explicit additional authorization.
  Never claim estimated savings are measurements.
- Never log applicant content or provider exception bodies. Secrets must remain
  outside Git; production credentials require a managed secret store.
- Extraction and artifact storage are independent with a bounded internal HTTP
  evidence handoff, not a shared volume. Keep these services single-instance.
- Read `docs/PRODUCTION_PREREQUISITES.md` before deployment changes. Shared tokens
  provide no tenant isolation. Production mode remains blocked.
- Test both extraction modes explicitly when changing extraction behavior. Live
  field/provenance/gate comparisons are required before promoting source-once.
  Do not claim local tests prove hosted CI ran or missing-source cases passed.
