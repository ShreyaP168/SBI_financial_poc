param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runName = 'uw-smoke-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
$runtimeRoot = if ($env:UW_RUNTIME_ROOT) { $env:UW_RUNTIME_ROOT } else { Join-Path (Split-Path $projectRoot -Parent) 'data/runtime' }
$runDirectory = Join-Path $runtimeRoot $runName
New-Item -ItemType Directory -Force -Path $runDirectory | Out-Null
New-Item -ItemType Directory -Path (Join-Path $runDirectory 'cases'), (Join-Path $runDirectory 'references') | Out-Null
$envFile = Join-Path $runDirectory 'smoke.env'
$providerFile = Join-Path $runDirectory 'provider.env'
Set-Content -LiteralPath $providerFile -Value '# Synthetic test: no provider credentials' -Encoding ascii
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$listener.Start()
$testPort = $listener.LocalEndpoint.Port
$listener.Stop()
$gatewayToken = [guid]::NewGuid().ToString('N')
$serviceToken = [guid]::NewGuid().ToString('N')
$databasePassword = [guid]::NewGuid().ToString('N')
$environment = @(
    "UW_GATEWAY_TOKEN=$gatewayToken",
    "UW_SERVICE_TOKEN=$serviceToken",
    "UW_DB_PASSWORD=$databasePassword",
    "UW_TEST_PORT=$testPort",
    ('UW_CASE_HOST_PATH=' + (Join-Path $runDirectory 'cases').Replace('\', '/')),
    ('UW_REFERENCE_HOST_PATH=' + (Join-Path $runDirectory 'references').Replace('\', '/')),
    ('UW_PROVIDER_ENV_FILE=' + $providerFile.Replace('\', '/'))
)
Set-Content -LiteralPath $envFile -Value $environment -Encoding ascii
$composeArgs = @('--env-file', $envFile, '-f', (Join-Path $projectRoot 'compose.yaml'), '-f', (Join-Path $projectRoot 'compose.test.yaml'), '-p', $runName)
$savedToken = $env:UW_GATEWAY_TOKEN
$savedUrl = $env:UW_TEST_GATEWAY_URL
try {
    & docker compose @composeArgs up -d --no-build --wait --wait-timeout 120
    if ($LASTEXITCODE -ne 0) {
        & docker compose @composeArgs logs --no-color --tail 100 gateway worker jobs-db
        throw 'Synthetic Compose stack failed readiness'
    }
    & docker compose @composeArgs exec -T worker python /app/test_support/postgres_concurrency.py
    if ($LASTEXITCODE -ne 0) { throw 'Concurrent PostgreSQL initialization failed' }
    $env:UW_GATEWAY_TOKEN = $gatewayToken
    $env:UW_TEST_GATEWAY_URL = "http://127.0.0.1:$testPort"
    & $Python (Join-Path $PSScriptRoot 'support/smoke_client.py')
    if ($LASTEXITCODE -ne 0) { throw 'Synthetic service workflow failed' }
} finally {
    $env:UW_GATEWAY_TOKEN = $savedToken
    $env:UW_TEST_GATEWAY_URL = $savedUrl
    # This unique test project owns these containers and ephemeral volumes.
    & docker compose @composeArgs down --volumes --remove-orphans
    if ($LASTEXITCODE -eq 0) {
        $pathSeparator = [IO.Path]::DirectorySeparatorChar
        $resolvedRuntime = [IO.Path]::GetFullPath($runtimeRoot).TrimEnd($pathSeparator)
        $resolvedRun = [IO.Path]::GetFullPath($runDirectory)
        if (-not $resolvedRun.StartsWith($resolvedRuntime + $pathSeparator) -or (Split-Path $resolvedRun -Leaf) -ne $runName) {
            throw 'Refusing to remove an unexpected smoke-test directory'
        }
        Remove-Item -LiteralPath $resolvedRun -Recurse -Force
    }
}
