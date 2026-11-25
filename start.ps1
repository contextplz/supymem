#requires -Version 5.1
$ErrorActionPreference = 'Stop'
& "$PSScriptRoot/scripts/run_local.ps1" @args
exit $LASTEXITCODE
