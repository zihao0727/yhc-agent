$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtime = Join-Path $root ".runtime"
$statePath = Join-Path $runtime "processes.json"
$state = Get-Content -LiteralPath $statePath | ConvertFrom-Json
$python = Join-Path $root ".venv\Scripts\python.exe"
$old = Get-CimInstance Win32_Process -Filter "ProcessId=$($state.api)"
if ($old) {
    if (!$old.CommandLine.Contains($python) -or !$old.CommandLine.Contains("app.main:app")) {
        throw "Recorded PID no longer belongs to this application. Restart refused."
    }
    Get-CimInstance Win32_Process -Filter "ParentProcessId=$($old.ProcessId)" | Where-Object {
        $_.CommandLine -and $_.CommandLine.Contains($python) -and $_.CommandLine.Contains("app.main:app")
    } | ForEach-Object { Stop-Process -Id $_.ProcessId }
    Stop-Process -Id $old.ProcessId -ErrorAction SilentlyContinue
}
for ($i = 0; $i -lt 20; $i++) {
    if (!(Get-NetTCPConnection -State Listen -LocalPort $state.apiPort -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 250
}
if (Get-NetTCPConnection -State Listen -LocalPort $state.apiPort -ErrorAction SilentlyContinue) {
    throw "API port remains in use; no unrelated process was stopped."
}
$api = Start-Process -FilePath $python -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$($state.apiPort)") -WorkingDirectory (Join-Path $root "backend") -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime "api.out.log") -RedirectStandardError (Join-Path $runtime "api.err.log")
$state.api = $api.Id
$state | ConvertTo-Json | Set-Content -LiteralPath $statePath
Write-Output "API restarted at http://127.0.0.1:$($state.apiPort)"
