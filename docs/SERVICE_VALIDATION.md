# Service refactor validation — 2026-10-11

Current delivery-policy update (platform 1.1.0): **203 tests passed**, configured
lint passed, Docker image and source/wheel builds passed, and the PostgreSQL
service smoke test passed with zero provider calls. The smoke applicant was
declined; QC WARN was permitted only after decision completeness succeeded.
Source-PDF tests use real preparation with controlled provider outputs; TruCap
tests use representative synthetic callbacks. No live TruCap endpoint or Key Vault
has been exercised. Existing CLI/extraction services still use the configured LLM
client, and TruCap remains a separately invoked optional adapter.

The local credential file was moved outside the actual Git checkout with
owner/SYSTEM-only access. Repository `.env` auto-loading was removed. Key Vault
loading was tested with mocked managed-identity clients. No vault URL or Azure
administrator/service-principal configuration was present, and Azure CLI was not
available, so vault population and credential rotation remain outstanding.

The original architecture-only validation below is retained for history.

| Check | Result |
|---|---|
| Underwriting regressions and service tests | 156 passed on Python 3.13 |
| Ruff lint, configured legacy baseline and full new-service rules | Passed |
| Ruff formatting for platform and new integration fixtures | Passed |
| Source distribution and wheel build | Passed |
| Packaged rules, prompts and bundled PDF fonts | Present |
| Credentials and test fixtures in production wheel | Excluded |
| Docker runtime image build | Passed |
| Six-process Compose stack: four APIs, worker and PostgreSQL | Ready and exercised |
| Concurrent PostgreSQL initialization | 24 operations passed |
| Synthetic age-66 Keyman applicant | DECLINE; never standard issuance |
| Gateway authentication, request idempotency and PDF delivery | Passed |
| Synthetic PDF QC | WARN; delivery allowed by the existing QC contract |
| External extraction/provider calls in architecture validation | Zero |

The unit/integration suite also covers missing configuration, mandatory stage
exceptions, malformed dependency responses, cross-case responses, bounded retry,
checkpoint restart, expired leases, lease heartbeat, cache invalidation and
QC-blocked downloads. An intermittent fresh-database startup failure was addressed
by serializing schema initialization with a PostgreSQL transaction advisory lock.
The final Docker stack and concurrency check passed after that correction.

The Docker test substitutes extraction with synthetic data and uses the real
underwriter, renderer, QC, HTTP boundaries and PostgreSQL queue. Its QC warning
reflects the minimal synthetic evidence. This validates the architecture and its
safety behavior; it is not a fresh accuracy assessment of every real case or an
external-provider integration test. Test containers and volumes were removed.

CI now declares Python 3.11/3.13 test/build jobs and a Docker integration job;
those hosted CI jobs have not been triggered from this workspace.

Reproduction commands and deployment details are in [SERVICES.md](SERVICES.md).
