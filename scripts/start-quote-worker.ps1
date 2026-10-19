param([string]$Root = (Join-Path $PSScriptRoot ".."))
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path -LiteralPath $Root).Path
$runtime = Join-Path $Root ".runtime"
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$workerStatePath = Join-Path $runtime "worker.json"
$python = (Resolve-Path -LiteralPath (Join-Path $Root ".venv\Scripts\python.exe")).Path
if (Test-Path -LiteralPath $workerStatePath) {
    $workerState = Get-Content -LiteralPath $workerStatePath | ConvertFrom-Json
    $previous = Get-CimInstance Win32_Process -Filter "ProcessId=$($workerState.pid)" -ErrorAction SilentlyContinue
    if ($previous -and $previous.CommandLine.Contains($python) -and
        $previous.CommandLine.Contains("app.agent_worker")) {
        return [int]$previous.ProcessId
    }
}
$priorMode = $env:AGENT_WORKER_MODE
$priorEncoding = $env:PYTHONIOENCODING
try {
    $env:AGENT_WORKER_MODE = "external"
    $env:PYTHONIOENCODING = "utf-8"
    $worker = Start-Process -FilePath $python -ArgumentList @("-m", "app.agent_worker") `
        -WorkingDirectory (Join-Path $Root "backend") -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $runtime "worker.out.log") `
        -RedirectStandardError (Join-Path $runtime "worker.err.log")
    @{ pid = $worker.Id; root = $Root; startedAt = (Get-Date).ToUniversalTime().ToString("o") } |
        ConvertTo-Json | Set-Content -LiteralPath $workerStatePath
    return $worker.Id
} finally {
    $env:AGENT_WORKER_MODE = $priorMode
    $env:PYTHONIOENCODING = $priorEncoding
}
