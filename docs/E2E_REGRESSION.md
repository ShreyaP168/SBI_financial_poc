# End-to-end regression for the public source snapshot

Local verification: 227 regression tests passed, configured lint/format checks
passed, the Docker image built, and the PostgreSQL service workflow passed.
The container workflow uses controlled synthetic extraction fixtures and the real
gateway, durable queue/worker, underwriting engine, renderer and QC services.
No live extraction or photo-provider calls were made.

| Scenario | Expected verdict | PDF access |
| --- | --- | --- |
| Keyman aged 66 | DECLINE | 200 |
| Missing required medical completion | FURTHER_REQUIREMENT | 200 |
| Unresolved operational eligibility | REFER | 200 |
| Disallowed kirana business | DECLINE | 200 |
| Student with prohibited ADB | DECLINE | 200 |
| Missing required age evidence | INCOMPLETE | 409 |

The workflow also verifies authentication, mandatory idempotency keys, identical
client retries returning the same job, conflicting retries returning 409, an empty
queue after completion and 24 concurrent PostgreSQL initializations. Test
containers and volumes are removed after the run. Synthetic timings do not
establish live-provider production capacity.

The source snapshot excludes credentials, applicant documents, saved case results
and legacy Git history. Real names and case references in fixtures/comments were
anonymized while preserving the regression behavior. Local history remains intact.

Local detailed reports live outside Git under `data/results`:
`e2e_publish_tests.xml`, `e2e_publish_docker_build.log` and
`e2e_publish_compose.log`. Hosted CI results must be recorded separately after
the source is pushed to GitHub. Production prerequisites remain unchanged.
