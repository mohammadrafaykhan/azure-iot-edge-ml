<#
.SYNOPSIS
    Roll out a model version to the edge device.
.DESCRIPTION
    Packages the current source, builds the images on the VM with the chosen model config
    (ml/models/<Model>.json, trained during the image build), and applies a manifest pinned to the
    new digests. Only modules whose image changed are restarted; operator settings such as active
    faults are kept. Rolling back is a rollout of the earlier version.
.EXAMPLE
    ./infra/rollout.ps1 -Model v2
#>
[CmdletBinding()]
param([Parameter(Mandatory)][ValidatePattern('^v\d+$')][string]$Model)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

if (-not (Test-Path (Join-Path $script:Repo "ml/models/$Model.json"))) { throw "ml/models/$Model.json not found" }
$envValues = Read-DotEnv
$blob = Publish-BuildContext -StorageAccount $envValues.STORAGE_ACCOUNT_NAME
Build-EdgeImages -Env $envValues -Blob $blob -Model $Model | Out-Null
Publish-Deployment -Env $envValues | Out-Null
Write-Host "Model $Model rolling out; the detector reports its model version in: python -m service status" -ForegroundColor Green
