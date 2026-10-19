param([int]$WebPort = 3001, [int]$ApiPort = 8002)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$runtime = Join-Path $root ".runtime"
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$node = (Get-Command node.exe).Source
$checker = Join-Path $PSScriptRoot "check-quote-frontend.cjs"
& $node $checker $WebPort
if ($LASTEXITCODE -eq 0) {
    Write-Output "Web already ready: http://127.0.0.1:$WebPort"
    return
}
$probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $WebPort)
try { $probe.Start() }
catch { throw "127.0.0.1:$WebPort is occupied. Select another -WebPort." }
finally { $probe.Stop() }
$priorTarget = $env:API_TARGET
$priorCi = $env:CI
try {
    $env:API_TARGET = "http://127.0.0.1:$ApiPort"
    # Vite otherwise treats the detached launcher's stdin EOF as a shutdown signal.
    $env:CI = "true"
    $web = Start-Process -FilePath $node `
        -ArgumentList @("node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", "$WebPort", "--strictPort") `
        -WorkingDirectory (Join-Path $root "frontend") -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $runtime "web.out.log") `
        -RedirectStandardError (Join-Path $runtime "web.err.log")
    $statePath = Join-Path $runtime "processes.json"
    $state = if (Test-Path -LiteralPath $statePath) {
        Get-Content -LiteralPath $statePath | ConvertFrom-Json
    } else { [pscustomobject]@{} }
    $state | Add-Member -NotePropertyName web -NotePropertyValue $web.Id -Force
    $state | Add-Member -NotePropertyName webPort -NotePropertyValue $WebPort -Force
    $state | Add-Member -NotePropertyName apiPort -NotePropertyValue $ApiPort -Force
    $state | ConvertTo-Json | Set-Content -LiteralPath $statePath
    for ($attempt = 0; $attempt -lt 10; $attempt++) {
        $web.Refresh()
        if ($web.HasExited) { throw "Frontend exited. See .runtime/web.err.log." }
        & $node $checker $WebPort
        if ($LASTEXITCODE -eq 0) {
            Write-Output "Web: http://127.0.0.1:$WebPort"
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw "Frontend did not serve this project's entry page. Inspect .runtime/web.err.log."
} finally {
    $env:API_TARGET = $priorTarget
    $env:CI = $priorCi
}
