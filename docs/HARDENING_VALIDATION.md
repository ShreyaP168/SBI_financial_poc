# Local validation for 1.2.0

Validated on Windows with Python 3.13 and Docker Desktop:

- 227 regression tests passed, including all external evidence invalidations,
  changed-workbook join values, failure-cache rejection, safe logs, deterministic
  provider failures, contract drift, heartbeat recovery, frozen evidence retention,
  required idempotency and bounded queue admission.
- Configured Ruff lint and formatting checks passed.
- Source and wheel distributions built. Implementation is namespaced under
  `underwriting_platform`; root shims and legacy adapters are absent.
- The Docker image built and the Compose smoke passed against PostgreSQL.
  Twenty-four concurrent database initializations succeeded. The synthetic age-66
  Keyman case returned DECLINE, produced a 47,572-byte PDF and QC WARN, with zero
  external provider calls. Test containers and volumes were removed.

The workspace's `data/results` retains `hardening_tests.xml`, package/image build
logs and `hardening_compose_smoke.log`. Built distributions are in `data/builds`.
These are local checks. No hosted CI run, live provider capacity test, production
tenant authorization, storage encryption verification or retention deployment is
claimed. See [production prerequisites](PRODUCTION_PREREQUISITES.md).
