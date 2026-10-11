# Repository layout

The Git checkout contains application code, deployment manifests, documentation
and regression tests. Original documents, results and credentials live outside Git.

```text
workspace/
  lifeinsurance-poc-repo/       Git checkout (open this folder in the IDE)
    src/
      underwriting_platform/
        configuration/         shared paths, services, providers and secrets
        contracts/             versioned HTTP messages
        domain/                workflow state and interfaces
        application/           workflows and decision delivery policy
        infrastructure/        persistence and external adapters
        services/              HTTP service entry points
        resources/config/      versioned carrier rules and QC profiles
        agents/                  extraction agents and prompts
        orchestrator/            survey, dispatch and reconciliation
        llm/                     provider clients and document preparation
        engine/                  underwriting and identity checks
        renderer/                active SBI renderer and fonts
        qc/                      case-sheet quality checks
        pipeline/                single and batch CLI runners
    tests/                     maintained regression tests and fixtures
    docs/                      architecture and operational documentation
    .github/workflows/         CI
    pyproject.toml             package, commands and coding standards
    Dockerfile, compose*.yaml   service deployment
  data/
    cases/                     original case documents
    references/                original carrier and bureau reference documents
    results/                   saved case outputs, workbooks and test reports
    archive/                   retained historical raw input
    runtime/                   generated local state, caches and temporary tests
  .local-secrets/               owner-protected credentials, outside Git
  .tools/python-deps/           local execution dependencies, outside Git
```

Root files are limited to standard project manifests, README, development
instructions and nonsecret configuration examples. Python implementation has one
copy under `src`; root compatibility wrappers have been removed. Install the
project before invoking its commands.

## Central configuration

`underwriting_platform.configuration` owns shared configuration:

| Module | Responsibility |
| --- | --- |
| `paths.py` | project, data, cases, references, runtime, caches and rule locations |
| `settings.py` | immutable service, database, retry and timeout settings |
| `providers.py` | extraction model/deployment, API options and retry configuration |
| `secrets.py` | environment injection, explicit external development files and Azure Key Vault |

Defaults resolve data to the workspace's `data` directory. Override with
`UW_DATA_ROOT`, `UW_CASE_ROOT`, `UW_REFERENCE_ROOT`, `UW_RUNTIME_ROOT`,
`UW_ARTIFACT_ROOT`, `UW_EXTRACTION_CACHE` or `UW_CONFIG_ROOT`. Prefer absolute paths
for deployments. CLI output defaults use the shared runtime directory. Carrier
policy assets stay versioned in the package, separate from operational settings.
Never put secret values in these modules or configuration examples.

The unused TruCap callback/upload service, its starter project and its dedicated
tests were removed at the user's request. The common delivery, auditability and
source-document tests remain. Legacy TALIC/PDF renderers and one-off analysis
commands were removed after checking their consumers. The active renderer is
`renderer.triage_sheet`.

Original documents, historical results and the protected credential file were
retained. Throwaway extraction experiments, manual scratch dumps and synthetic
test runs are disposable. A workbook held open by Excel may temporarily retain
its old location until the application releases the file.
