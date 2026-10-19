param([int]$ApiPort = 8002, [int]$WebPort = 3001)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$runtime = Join-Path $root ".runtime"
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
if (!(Test-Path -LiteralPath (Join-Path $root "backend\.env"))) {
    throw "Configure backend/.env first."
}
foreach ($port in @($ApiPort, $WebPort)) {
    $probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $port)
    try { $probe.Start() }
    catch { throw "127.0.0.1:$port is occupied. Select a different port; no process was stopped." }
    finally { $probe.Stop() }
}
$python = (Resolve-Path -LiteralPath (Join-Path $root ".venv\Scripts\python.exe")).Path
$node = (Get-Command node.exe).Source
$priorMode = $env:AGENT_WORKER_MODE
$priorTarget = $env:API_TARGET
$priorEncoding = $env:PYTHONIOENCODING
$priorCi = $env:CI
try {
    $env:AGENT_WORKER_MODE = "external"
    $env:PYTHONIOENCODING = "utf-8"
    Push-Location (Join-Path $root "backend")
    try {
        & $python -m alembic upgrade head
        if ($LASTEXITCODE -ne 0) { throw "Database migration failed. Services were not started." }
    } finally { Pop-Location }
    $api = Start-Process -FilePath $python `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$ApiPort") `
        -WorkingDirectory (Join-Path $root "backend") -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $runtime "api.out.log") `
        -RedirectStandardError (Join-Path $runtime "api.err.log")
    $env:API_TARGET = "http://127.0.0.1:$ApiPort"
    # Detached Vite must not stop when its launching terminal closes stdin.
    $env:CI = "true"
    $web = Start-Process -FilePath $node `
        -ArgumentList @("node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", "$WebPort", "--strictPort") `
        -WorkingDirectory (Join-Path $root "frontend") -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $runtime "web.out.log") `
        -RedirectStandardError (Join-Path $runtime "web.err.log")
    $workerId = & (Join-Path $PSScriptRoot "start-quote-worker.ps1") -Root $root
    @{ api = $api.Id; web = $web.Id; worker = $workerId; apiPort = $ApiPort; webPort = $WebPort } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtime "processes.json")
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $status = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/api/health/ready" -TimeoutSec 2
            & $node (Join-Path $PSScriptRoot "check-quote-frontend.cjs") $WebPort
            if ($status.ready -and $LASTEXITCODE -eq 0) {
                $ready = $true; break
            }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    if (!$ready) { throw "API, worker or frontend readiness failed. Inspect .runtime service logs; PIDs are saved." }
    Write-Output "Web: http://127.0.0.1:$WebPort"
    Write-Output "API: http://127.0.0.1:$ApiPort/api/docs"
    Write-Output "Independent worker ready (PID $workerId)."
} finally {
    $env:AGENT_WORKER_MODE = $priorMode
    $env:API_TARGET = $priorTarget
    $env:PYTHONIOENCODING = $priorEncoding
    $env:CI = $priorCi
}
