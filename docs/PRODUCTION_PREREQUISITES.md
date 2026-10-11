# Controlled POC boundary and production prerequisites

This release is a controlled POC with human underwriting review. Artifact delivery
is not authorization to issue a policy. Service `UW_DEPLOYMENT_MODE` defaults to
`controlled_poc`; any other mode fails service startup. Shared bearer tokens support a
trusted localhost operator only. These controls do not implement tenant isolation.
The production gate must remain until the requirements below are implemented,
tested and approved. A setting change alone is not approval.

## Identity, authorization and access audit

Production requires an OIDC identity provider with validated issuer, audience,
signature, expiry and authorized tenant/subscriber claims. Derive tenant and user
from the verified identity, never from a request's case identifier. Enforce separate
submit, source-upload, case-read, decision-read, download, retention-admin and
legal-hold scopes at every API and storage operation. Internal calls require
service identities and short-lived audience-bound credentials; shared service
tokens are insufficient. Tenant and owner filtering must cover job rows,
idempotency keys, checkpoints, cached evidence, manifests and PDFs. Cross-tenant
read, enumeration, submission and download denial tests are release requirements.

Use private networking, TLS and an authorized gateway. Downloads must revalidate
ownership and delivery status; an unguessable job UUID is not authorization.
Audit each allowed and denied access with UTC time, tenant, authenticated actor,
action, opaque resource ID, purpose, request ID, outcome and policy version.
Send audit records to an independently protected append-only destination. Exclude
document text, financial values, provider responses, tokens and credentials.

## Storage encryption and backup verification

Verify TLS and server identity for the actual database connection. Verify at-rest
encryption for the actual database data files, extraction/artifact volumes, object
storage, temporary disks and backups. Record deployment resource IDs, encryption
configuration, key ownership and a successful restore test. Container isolation
does not demonstrate encryption. Prefer managed encrypted storage with managed
identity and separately administered encryption keys; document key rotation and
revocation, backup access and deletion propagation. This local deployment has not
been certified for encrypted persistent storage.

## Retention and deletion specification

Before production applicant data is processed, the insurer/data owner must approve
a versioned retention policy with durations, triggering events, legal holds,
deletion service levels, audit-record retention and backup expiry. Durations are
not invented in application code. The policy must include all of these records:

| Record | Owner and deletion scope |
| --- | --- |
| Source documents and external reference evidence | Intake owner; original upload, every snapshot, version and temporary extraction file |
| Extraction cache and raw agent reports | Extraction owner; response JSON, evidence snapshots, cache revisions and unused content-addressed objects |
| Jobs and checkpoints | Workflow owner; `workflow_jobs`, `workflow_payloads`, results, failures and idempotency tombstones |
| Decisions, PDFs and reproduction bundles | Artifact owner; diagnostics, delivered decisions, QC files, manifests, all run bundles and unused shared objects |
| Logs, access audit and backups | Operations/compliance; independent retention and verified expiry across replicas and restored copies |

A retention job must select expired records by tenant and approved policy,
honor legal holds, produce a reviewed deletion plan, revoke delivery/download
access first, cancel processing, delete related database and storage records,
garbage-collect objects only when no retained bundle references them, and write a
non-PII deletion receipt to the independent audit sink. Deleting one hardlinked
bundle does not delete the shared object or other links. Test recovery from
partial deletion and ensure retries do not recreate deleted evidence. Restore
procedures must replay deletion receipts before serving data. Retain only a
non-PII idempotency tombstone as authorized by policy to prevent paid-work retries.

No automatic deletion of the user's existing documents or saved results was
enabled by this refactor. Real retention periods, tenant identities, audit sinks
and encrypted deployment resources have not been supplied or verified here.

## Capacity and operational admission

Run exactly one extraction replica/process and one artifacts replica/process on
one host for this POC. Both serialize work. The durable worker count is not the
extraction capacity. Do not horizontally replicate file-backed services or share
their volumes across independent hosts. Production horizontal scaling requires
distributed ownership, immutable object storage and tenant-scoped atomic cache
publication; a process-local lock is insufficient.

Admission is bounded by `UW_QUEUE_CAPACITY` (default 100 pending jobs, an initial
POC bound rather than a measured capacity promise). A full queue returns 503;
an existing idempotent retry still returns its original job. `/v1/operations/queue`
reports pending count, capacity and oldest pending age to the authenticated
operator. Structured logs include queue age, attempt count, extraction/artifact
lock wait and processing duration. Measure p50/p95/p99 cold and warm stage times,
throughput, provider rate limits, error/retry rate, queue age and storage growth
using representative approved workloads before increasing admission or workers.
Set the queue bound from measured service time and the agreed maximum wait; use
load tests to confirm it. Synthetic smoke timings do not predict paid-provider
performance. Alert on increasing queue age and dependency failures.

## Submission identity and release evidence

`Idempotency-Key` is required, nonblank and at most 128 characters. The client
must create it before submitting and reuse it for retries after timeouts. The
same key and identical request return the same job; changed requests return 409.
Generate a new key only for an intentional new run. Production must scope these
keys to the authenticated tenant/customer.

Release evidence must include a committed source revision, hosted CI run, package
and image identifiers, the security/retention approvals and representative stage
measurements. Local test/build success is not a hosted CI run. The checkout's
remote is currently on `code.techjini.com`; `.github/workflows/underwriting.yml`
will run only in a GitHub repository with Actions enabled. Configure the actual
hosting provider's CI before claiming hosted validation.
