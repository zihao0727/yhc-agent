param([int]$ApiPort = 8000, [int]$WebPort = 5173)
$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "start-quote-services.ps1") -ApiPort $ApiPort -WebPort $WebPort
