param([int]$ApiPort = 8000, [int]$WebPort = 5173)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtime = Join-Path $root ".runtime"
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
if (!(Test-Path -LiteralPath (Join-Path $root "backend/.env"))) {
    throw "Configure backend/.env first."
}
foreach ($port in @($ApiPort, $WebPort)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
        throw "Port $port is occupied. Select another port using -ApiPort / -WebPort."
    }
}
$python = Join-Path $root ".venv/Scripts/python.exe"
$node = (Get-Command node.exe).Source
$env:API_TARGET = "http://127.0.0.1:$ApiPort"
$api = Start-Process -FilePath $python -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$ApiPort") -WorkingDirectory (Join-Path $root "backend") -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime "api.out.log") -RedirectStandardError (Join-Path $runtime "api.err.log")
$web = Start-Process -FilePath $node -ArgumentList @("node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", "$WebPort", "--strictPort") -WorkingDirectory (Join-Path $root "frontend") -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime "web.out.log") -RedirectStandardError (Join-Path $runtime "web.err.log")
@{ api = $api.Id; web = $web.Id; apiPort = $ApiPort; webPort = $WebPort } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtime "processes.json")
Write-Output "Web: http://127.0.0.1:$WebPort"
Write-Output "API: http://127.0.0.1:$ApiPort/api/docs"
