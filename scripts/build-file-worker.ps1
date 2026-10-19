$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
docker build --tag firefly-file-worker:1 (Join-Path $projectRoot "backend\file-worker")
if ($LASTEXITCODE -ne 0) { throw "File worker image build failed." }
