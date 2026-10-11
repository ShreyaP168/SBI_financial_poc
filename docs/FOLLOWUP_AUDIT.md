# Four-case follow-up — 11 October 2026

All four confirmed case folders exist. Earlier processing did not establish that
the outputs met the current evidence and delivery-completeness policy. Do not
interpret a diagnostic verdict or the absence of a QC finding as approval to deliver.

| Case | Latest outcome | Cause / diagnostic decision |
| --- | --- | --- |
| 4CCZ526688 | Decision-complete DECLINE | Previously verified native business-name evidence; 154,925 combined provider tokens. |
| 4CPB014070 | INCOMPLETE | Live extraction completed at 78,807 tokens, but employer/business-nature citations are absent. Corrected engine diagnostic: FURTHER_REQUIREMENT, matching UW. |
| 4CYL218379 | INCOMPLETE | 187,478 tokens spent; the next reserved request would exceed 200,000. Partial extraction and employer provenance prevent delivery. |
| 4CYL294940 | INCOMPLETE | 172,973 tokens spent; subsequent requests exceed the cap. Partial extraction and business provenance prevent delivery. Diagnostic DECLINE is not a validated decision. UW baseline is FURTHER_REQUIREMENT; the separate validation baseline says DECLINE. |

The final three live cases are in the workspace's `data/results/four_case_followup`.
Deterministic engine/render/QC replays are in `data/results/four_case_reviewed` and
made zero additional provider calls. Chotu's complete output remains under
`data/results/case_sheet_reference_format`. These applicant artifacts are private
workspace data and are excluded from the source repository.

## Additional defects corrected

- Persist freshly computed completeness before saving a decision checkpoint.
- Never publish extraction with source/contract findings into the successful cache.
- Independently enforce current completeness, successful status and QC at download.
- Missing income evidence is a resolvable HIGH finding; documented zero remains adverse.
- Read bureau DOCX labels using the standard XML/ZIP library, removing the missing
  optional `python-docx` dependency observed during the live run.
- Require employer and business-nature citations in source-reader instructions,
  using the same conditional evidence paths as the completeness policy.
- Failed agent checkpoints do not survive configuration/provider recovery.
- Explicit pre-generation HTTP rejection releases the token reservation; ambiguous
  interrupted requests remain conservatively charged.
- Remove medical secondary-model notes from logs.
- Correct the stale shared-volume description: artifact storage imports frozen
  evidence through the internal authenticated evidence API.

Coverage is in `tests/test_followup_audit.py` and the existing hardening, service,
token-control and reference-format suites. The local full-suite report is retained
outside Git at `data/results/followup_final_tests.xml`.

Validation: all 265 local regression tests passed, Ruff passed, and the rebuilt
Docker/PostgreSQL workflow passed DECLINE, FURTHER_REQUIREMENT, REFER, prohibited
student rider and INCOMPLETE/download-409 scenarios with zero provider calls.

## Earlier review items and deployment limits

Hard gate enforcement, fail-closed mandatory processing, insurer-based routing,
pure boundary contracts, DOB-derived entry age, input version/fingerprint retention,
full-file deduplication, required idempotency keys, lease recovery, compact queue
checkpoints and shared delivery policy have regression coverage in the checkout.
TruCap and its dedicated tests were removed at the user's explicit request.
Source extraction now has one document owner, native-text routing, bounded images,
per-agent output limits, prompt-cache requests and a persistent 200,000-token cap.
The two larger cases show that the cap still prevents completion; the cap is not
a promise that every case fits. No fresh repeat run or estimated saving is claimed
for the newly strengthened prompts.

Production prerequisites remain open: actual tenant/OIDC authorization, managed
secret-store migration and rotation, verified encrypted database/volumes, approved
retention/deletion/access-audit policies, backups and measured deployment capacity.
No Key Vault URL, authenticated Azure administrator, OIDC issuer/audience or approved
storage policy is configured here. Production mode remains blocked. Shared tokens
and one extraction/artifact instance remain controlled-POC constraints.

The initial push denial was subsequently resolved: the code-only `f04dfe9` release
was published on GitHub main. Hosted CI ran: both Python jobs passed, and the
service smoke failed on the disabled worker health check. The current correction
and validation status are tracked in `RECOVERY_AUDIT.md`.
Do not claim all seven cases passed: only these four have source documents here.
