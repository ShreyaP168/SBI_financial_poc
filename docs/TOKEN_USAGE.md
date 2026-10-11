# Extraction token controls

The shared case budget remains 200,000 tokens. Generation checks input-token
estimates and reserves the per-call output cap before submitting a request.
Budget exhaustion produces an incomplete diagnostic case, never issuance.

Identity and income run before compliance. Compliance receives their structured
findings and validated citations instead of rereading single-purpose identity
and financial evidence. Proposal forms, declarations, AML records and ambiguous
documents still require direct compliance reading: upstream agents do not
extract all their regulatory facts. Failed upstream findings cannot supply
trusted inherited citations. Compliance may only copy a validated citation
exactly; inventing a page or excerpt produces a source finding.

Native-text pages without images or annotations use text only, except forms
whose declarations depend on layout. Scanned pages retain vision. Recognized
ID cards use a 1,000-pixel maximum dimension; dense documents retain their
original extraction resolution. Face photos are excluded from field agents
and remain available to the separate identity/photo verification pipeline.

Full-file deduplication is followed by conservative decoded-content deduplication
before the survey. All rendered page pixels, page counts and text must match.
Partially rendered and unreadable documents are never collapsed. Duplicate
aliases appear in the extraction report, and source retention keeps originals.
Perceptually similar scans are not automatically discarded: small changes may
alter underwriting evidence. This does not promise arbitrary scan deduplication.

System prompts request ephemeral provider caching. Provider usage records show
actual cache reads/writes; support, minimum prompt size and cache lifetime affect
whether savings occur. No retry without caching is issued merely to obtain it.
Agent output caps are identity 6,000, income 8,000, compliance 5,000, medical
6,000, assets/liabilities 5,000 and business/surrogate 6,000. Other agents default
to 6,000; the survey uses 8,000. Truncation fails closed without identical retries.
Notes and evidence excerpts are concise, but critical citations remain mandatory.

Successful stage checkpoints avoid repeating completed extraction on retry.
The same reviewed extraction model remains configured; a smaller routing model
requires separate accuracy evaluation before selection. A budget cap is not a
measured token reduction. Compare provider usage on completed representative
cases before making claims about savings or underwriting accuracy.

## Native forms and single-case measurement

The document-level form flag no longer forces images for every native-text page.
`prepare_doc` records page-level layout dependencies: images, annotations,
curves, graphical checkboxes and unreadable glyphs. Clear native form pages
use their full text; pages with those dependencies keep vision. Scanned pages
retain vision. Original page numbers and full native text remain available.

Optional duplicate medical extraction is off by default. Set
`UW_DUAL_VENDOR_MEDICAL=true` only when an additional vendor comparison is
required. It uses the same case budget when enabled. Gemini document
classification defaults to `gemini-2.5-flash`, configurable through
`GEMINI_DOCUMENT_CLASSIFIER_MODEL`. The classifier uses a structured JSON-array
schema and an 8,192-token allowance so reasoning does not truncate candidate rows.
Reconciliation allows 4,000 output tokens with bounded, concise explanations.
These extraction configuration changes invalidate prior extraction caches.

`provider_usage` in the extraction report records safe per-request stage,
model, token counts and Azure image/text sizes. Azure cache reads/writes are
reported separately. Gemini totals include provider-reported thinking tokens;
input plus visible output can therefore be less than the total. The budget
still conservatively charges reservations if actual usage is unavailable.
Usage records from a fresh single-case run measure that run; reused stage
checkpoints can include historical spend in the budget ledger.

Local OCR was evaluated on the representative case's blurred attachments.
It did not yield reliable text and is not used to assert their underwriting
facts or replace images. No smaller Azure deployment is assumed to exist.

## One owning source reader

`UW_EXTRACTION_MODE=domain` is the default for all clients. Explicit experimental
`UW_EXTRACTION_MODE=source_once` uses the survey
selects the applicable domain schemas, then one source reader extracts their
canonical blocks together. Each non-photo source document belongs to one packed
batch, sized by actual images and text rather than an eight-file limit. Images,
page numbers and full native text use the same preparation and citation validator.
Core identity/income/compliance blocks remain mandatory. Domain outputs are
partitioned deterministically; they do not invoke additional document reads.
The original provider payload is retained once under `source_owner` in the report.
Output allowance is 16,000 for the combined extraction, rather than separate
allowances for multiple agents rereading the same evidence. A failed or truncated
combined response still fails closed. Successful source batches are checkpointed.

The source reader now retains complete versioned domain prompt instructions
and extracts evidence facts. It does not issue decisions; carrier gates,
calculations, completion policy and QC still run in the deterministic pipeline.
Provider and scripted clients use the same default; tests explicitly select
`source_once` when exercising it. Synthetic tests exercise the owning
source path, including trade in business name and in business nature, source
citations, the evaluated occupation gate and its decline outcome.

The 73% saving reported above measured the earlier reduced-prompt source reader.
It is not a measurement of the current full-instruction reader. The available
four-case run yielded one complete case, one citation-blocked case and two
budget-limited cases. No seven-case paired live accuracy comparison has passed;
three reviewed cases have no source files here. Source-once remains opt-in.
Its redundant JSON reconciler call is skipped. Budget limits do not participate
in extraction revisions, so changing a limit retains successful stage checkpoints
and prior spend; it never resets spend or makes a partial case complete.

Native proposal business declarations are checked directly. An exact labeled
business name supplies its document/page citation. An uncited prose expansion
of that name is retained as a model inference, rather than represented as a
separate declared business nature. Cited declarations are preserved.

## Measured one-case regression, 2026-10-11

Case `4CCZ526688` consumed 125,813 Azure tokens across three generation calls,
versus 474,758 across twelve in the original run: 73.5% fewer Azure tokens.
Including Gemini classification/photo matching, measured consumption was
154,925 tokens for 35 unique documents, a case-level average of 4,426.43 tokens
per document. These averages are not individually metered document costs.

The live normalized extraction was replayed through engine, rendering and QC
after exact native business-name verification, with no additional provider calls.
The case was decision-complete with DECLINE, matching the supplied UW baseline.
Verified income was 860,764 and HLV 25,822,920, matching the supplied financials.
QC was WARN with no blocking findings. This validates one representative case;
it does not establish the outcomes of the other six cases. Prior diagnostic
attempts consumed 195,588 and 165,277 tokens separately during tuning.
