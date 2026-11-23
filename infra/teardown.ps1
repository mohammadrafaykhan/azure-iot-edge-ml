<#
.SYNOPSIS
    Delete every Azure resource for azure-iot-edge-ml.
.DESCRIPTION
    Removes the resource group: the edge VM and its disk, network, and public IP, IoT Hub, the
    container registry, and storage. Local files (.env, .edge-images.json) are kept.
.EXAMPLE
    ./infra/teardown.ps1 -ResourceGroup rg-iot-edge-dev
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = 'rg-iot-edge-dev',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

if ((az group exists --name $ResourceGroup) -ne 'true') {
    Write-Host "Resource group $ResourceGroup does not exist; nothing to delete."
    return
}

if (-not $Force) {
    $answer = Read-Host "Delete resource group '$ResourceGroup' and everything in it? Type the name to confirm"
    if ($answer -ne $ResourceGroup) {
        Write-Host 'Cancelled.'
        return
    }
}

az group delete --name $ResourceGroup --yes
if ($LASTEXITCODE -ne 0) { throw "Failed to delete $ResourceGroup" }
Write-Host "Deleted $ResourceGroup"
