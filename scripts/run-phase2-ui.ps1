$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$schema = "test_firefly_" + [Guid]::NewGuid().ToString("N")
foreach ($port in @(8001, 5174)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
        throw "Test port $port already in use."
    }
}
$env:UI_TEST_SCHEMA = $schema
$env:API_TARGET = "http://127.0.0.1:8001"
$python = Join-Path $root ".venv/Scripts/python.exe"
$node = (Get-Command node.exe).Source
$api = $null
$web = $null
try {
    $api = Start-Process -FilePath $python -ArgumentList @("ui_test_server.py") -WorkingDirectory (Join-Path $root "backend") -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $root ".runtime/ui-api.out.log") -RedirectStandardError (Join-Path $root ".runtime/ui-api.err.log")
    $web = Start-Process -FilePath $node -ArgumentList @("node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", "5174", "--strictPort") -WorkingDirectory (Join-Path $root "frontend") -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $root ".runtime/ui-web.out.log") -RedirectStandardError (Join-Path $root ".runtime/ui-web.err.log")
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $result = Invoke-RestMethod "http://127.0.0.1:8001/api/health"
            if ($result.status -eq "ok") { $ready = $true; break }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
    if (!$ready) { throw "Test API did not become ready." }
    & $node (Join-Path $root "scripts/smoke-phase2.cjs")
    if ($LASTEXITCODE -ne 0) { throw "UI tests failed." }
} finally {
    foreach ($process in @($api, $web)) {
        if ($process -and !$process.HasExited) {
            Get-CimInstance Win32_Process -Filter "ParentProcessId=$($process.Id)" | ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
            Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
        }
    }
    Push-Location (Join-Path $root "backend")
    try {
        & $python -c "import os,re;from sqlalchemy import create_engine,text;from app.config import settings;s=os.environ['UI_TEST_SCHEMA'];assert re.fullmatch(r'test_firefly_[0-9a-f]{32}',s);e=create_engine(settings().database_url);c=e.connect();c.execute(text('DROP SCHEMA IF EXISTS '+s+' CASCADE'));c.commit();c.close();print('Disposed test schema')"
    } finally { Pop-Location }
    Remove-Item Env:UI_TEST_SCHEMA
    Remove-Item Env:API_TARGET
}
