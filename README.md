# Underwriting platform

Document extraction, deterministic underwriting and audited SBI Life case-sheet
generation. Independent extraction, underwriting, artifact/QC and gateway
services share a durable workflow worker. Mandatory processing failures block
decision delivery.

This is a controlled POC with human review. Production prerequisites are defined
in [security, data lifecycle and capacity requirements](docs/PRODUCTION_PREREQUISITES.md).
See [release 1.2.0 hardening](docs/HARDENING.md).

See [folder layout and central configuration](docs/LAYOUT.md),
[service startup](docs/SERVICES.md), [architecture](docs/ARCHITECTURE.md) and
[coding standards](docs/CODING_STANDARDS.md).

Install from this Git checkout root:

```powershell
python -m pip install -e ".[dev,secrets]"
```

Set provider credentials through environment injection, Azure Key Vault, or an
explicit `UW_DEV_ENV_FILE` outside Git. Repository `.env` files are never
automatically loaded. Operational examples are in `.env.example` and
`.services.env.example`; carrier-approved rules are packaged under
`src/underwriting_platform/resources/config/`.

```powershell
uw-case --case_dir "../data/cases/Financial Evaluation/CASE_ID" --case_id CASE_ID
uw-batch --source_root "../data/cases/Financial Evaluation" --concurrency 4
uw-api
uw-worker
```

The Python module commands `python -m underwriting_platform.pipeline.run` and `python -m underwriting_platform.pipeline.batch`
also work after installation. TruCap and obsolete root package wrappers have been
removed. Keep original documents and generated results under the workspace's
`data` directory; keep secrets outside the checkout.

Run regression checks and build the package:

```powershell
python -m pytest -q
python -m ruff check src tests/test_service_architecture.py tests/test_delivery.py tests/test_secret_loading.py tests/support
python -m build
docker build -t underwriting-platform:local .
```

QC status and decision completeness are separate. A diagnostic PDF or QC PASS
does not authorize release; consumers must enforce `delivery_allowed` and the
current completion policy. Deployment and live-provider validation requirements
are described in the service documentation.
