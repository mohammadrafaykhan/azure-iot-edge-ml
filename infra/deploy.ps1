<#
.SYNOPSIS
    Deploy the edge ML line end to end.
.DESCRIPTION
    1. Registers resource providers and deploys main.bicep: IoT Hub with routing to storage, the
       container registry and its pull token, and the edge VM (IoT Edge 1.6 installed at first boot).
    2. Waits for first boot, reads the device certificate thumbprint, registers the IoT Edge device,
       and applies the provisioning config on the VM.
    3. Builds both module images on the VM, pushes them to the registry, and applies the
       deployment manifest pinned to their digests.
    Writes .env for edgectl. Uses the signed-in Azure CLI identity as the operator.
.EXAMPLE
    ./infra/deploy.ps1
.EXAMPLE
    ./infra/deploy.ps1 -Model v2 -VmSize Standard_B2s_v2
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = 'rg-iot-edge-dev',
    [string]$Location = 'centralindia',
    [ValidatePattern('^v\d+$')][string]$Model = 'v1',
    [string]$VmSize = 'Standard_B2als_v2'
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

foreach ($namespace in 'Microsoft.Devices', 'Microsoft.ContainerRegistry', 'Microsoft.Compute', 'Microsoft.Network',
    'Microsoft.Storage', 'Microsoft.DevTestLab') {
    if ((Invoke-AzRaw provider show --namespace $namespace --query registrationState -o tsv) -ne 'Registered') {
        Write-Host "Registering resource provider $namespace"
        Invoke-Az provider register --namespace $namespace --wait | Out-Null
    }
}

$operatorId = Invoke-Az ad signed-in-user show --query id -o tsv
Invoke-Az group create --name $ResourceGroup --location $Location `
    --tags project=azure-iot-edge-ml environment=dev -o none
Write-Host "Resource group $ResourceGroup ready in $Location"

# Azure requires an SSH key to create a Linux VM. No inbound port is open and the private half is
# discarded here, so the key can never be used; the VM is managed through Run Command only.
$sshPublicKey = & $script:Python -c @'
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
print(key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode())
'@
if ($LASTEXITCODE -ne 0 -or -not $sshPublicKey) { throw 'Generating the throwaway SSH public key failed.' }

# A new hub identity can take a minute or two to be usable for its storage role, so the routing
# step may fail on the first attempt; the deployment is idempotent and is retried.
$outputs = $null
for ($attempt = 1; $attempt -le 3 -and -not $outputs; $attempt++) {
    $raw = Invoke-AzRaw deployment group create --resource-group $ResourceGroup `
        --name "edge-ml-$(Get-Date -Format 'yyyyMMddHHmmss')" `
        --template-file (Join-Path $PSScriptRoot 'main.bicep') `
        --parameters operatorPrincipalId=$operatorId sshPublicKey="$sshPublicKey" vmSize=$VmSize `
        --query properties.outputs -o json
    if ($LASTEXITCODE -eq 0) {
        $outputs = ($raw -join "`n") | ConvertFrom-Json
    } elseif ($attempt -lt 3) {
        Write-Host "Infrastructure deployment attempt $attempt failed; retrying in 60 s" -ForegroundColor Yellow
        Start-Sleep -Seconds 60
    }
}
if (-not $outputs) { throw 'Infrastructure deployment failed three times; see the errors above.' }

$values = [ordered]@{
    IOTHUB_HOSTNAME      = $outputs.iotHubHostName.value
    EDGE_DEVICE_ID       = $outputs.deviceId.value
    STORAGE_ACCOUNT_NAME = $outputs.storageAccountName.value
    ACR_LOGIN_SERVER     = $outputs.registryLoginServer.value
    ACR_NAME             = $outputs.registryName.value
    ACR_PULL_TOKEN       = $outputs.pullTokenName.value
    RESOURCE_GROUP       = $ResourceGroup
    VM_NAME              = $outputs.vmName.value
}
$values.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" } |
    Set-Content -Path (Join-Path $script:Repo '.env') -Encoding ascii
$envValues = Read-DotEnv
Write-Host "Infrastructure ready; wrote .env" -ForegroundColor Green

Write-Host "Waiting for first boot of $($values.VM_NAME) (IoT Edge install, device key generation)"
$thumbprint = Invoke-EdgeScript -ResourceGroup $ResourceGroup -VmName $values.VM_NAME `
    -Script 'device-thumbprint.sh' -Marker 'THUMBPRINT'
Invoke-WithRetry -What 'IoT Hub access' -Action { Invoke-Edgectl device register $thumbprint } | Write-Host

$configured = Invoke-EdgeScript -ResourceGroup $ResourceGroup -VmName $values.VM_NAME `
    -Script 'apply-config.sh' -Marker 'CONFIGURED'
Write-Host 'IoT Edge runtime provisioned with the device certificate'

$blob = Publish-BuildContext -StorageAccount $values.STORAGE_ACCOUNT_NAME
Build-EdgeImages -Env $envValues -Blob $blob -Model $Model | Out-Null
Publish-Deployment -Env $envValues | Out-Null

Write-Host "`nDeployed. Modules start within a few minutes; check with: python -m service status" -ForegroundColor Green
