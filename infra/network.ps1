<#
.SYNOPSIS
    Cut or restore the edge device's connection to IoT Hub (store-and-forward demonstration).
.DESCRIPTION
    Blocks the hub's address on the VM for the host and for containers, resetting open connections.
    The modules keep running; the IoT Edge hub queues upstream messages and delivers them, alerts
    first, when the link is restored.
.EXAMPLE
    ./infra/network.ps1 -Offline
.EXAMPLE
    ./infra/network.ps1 -Online
#>
[CmdletBinding(DefaultParameterSetName = 'Offline')]
param(
    [Parameter(ParameterSetName = 'Offline', Mandatory)][switch]$Offline,
    [Parameter(ParameterSetName = 'Online', Mandatory)][switch]$Online
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$envValues = Read-DotEnv
$mode = if ($Offline) { 'offline' } else { 'online' }
$result = Invoke-EdgeScript -ResourceGroup $envValues.RESOURCE_GROUP -VmName $envValues.VM_NAME -Script 'network.sh' `
    -Arguments @($mode, $envValues.IOTHUB_HOSTNAME) -Marker 'NETWORK'
Write-Host "Edge device is now $result at $([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))" -ForegroundColor Green
