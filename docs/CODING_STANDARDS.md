# Coding and design standards

Use Python 3.11 or later and install through `pyproject.toml`. Keep implementation
under `src/`; place tests under `tests/`. New service code is formatted with Ruff
(100-character target) and checked for imports, unused/undefined names, common
bug patterns and Python modernization.

## Boundaries

- Domain objects describe state. They have no HTTP, filesystem or database calls.
- Application use cases depend on `Protocol` interfaces. Inject adapters at the
  service/worker composition boundary; avoid concrete infrastructure imports in
  application workflows.
- Infrastructure owns database transactions, HTTP clients, provider/kernel
  adapters and filesystem access. Each adapter has one responsibility.
- Services validate typed contracts, authenticate and delegate to use cases.
  Carrier decisions belong in `engine`, never in routes or deployment settings.
- Shared operational configuration belongs in `underwriting_platform.configuration`.
  Use its path constants and typed settings instead of deriving data locations in
  feature modules. Carrier rules remain versioned package resources.
- Keep public envelopes under `contracts/v1.py`. Breaking envelope changes need
  a new API version; canonical case field changes also require architecture docs.

These boundaries apply single responsibility, dependency inversion and interface
segregation. Adapters can be replaced without changing the workflow. Do not add
abstract classes or pattern layers without a concrete substitution requirement.

## Implementation rules

- Use type hints on public interfaces, explicit exceptions and immutable settings.
  Secrets must not appear in settings repr, public errors or new service logs.
- Use dependency injection for tests; never enable a production "fake extraction"
  setting. Test fixtures stay outside the distributed package.
- Use parameterized SQL and scoped paths. Commit checkpoint writes atomically;
  publish artifact metadata only after PDF/QC files have been persisted.
- Mandatory processing fails closed. Catch an error only when converting it to an
  explicit failure, retry or manual-review outcome.
- Preserve source provenance, confidence and carrier rule citations. An advisory
  heuristic must not become an issuance rule.
- Blocking kernel work runs off the API event loop. Long workflows belong to the
  dedicated worker; configure finite HTTP timeouts and bounded retry attempts.
- Tests should prove behavior: gate failures, cross-case protection, lease loss,
  durable restart, malformed dependencies and blocked downloads.

## Legacy migration policy

The policy kernel keeps its existing package names and is regression-tested.
Legacy modules have a documented lint baseline for import/style/unused-variable
cleanup; syntax errors and undefined names are still checked. New platform layers
and new integration fixtures use the full configured rules. Avoid mass formatting
policy code in an architecture change, since it obscures rule changes during
review. Tighten legacy exemptions as those modules are deliberately modernized.

Run the verification commands in [SERVICES.md](SERVICES.md) before a merge.
Pin dependency updates intentionally, run the carrier regressions, and rebuild
the container. The shared image is a deployment convenience; each service remains
independently runnable and owns only its declared runtime state.
