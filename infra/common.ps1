# Shared by deploy.ps1, rollout.ps1, and network.ps1 (dot-sourced).

$script:Repo = Split-Path $PSScriptRoot -Parent
$script:Python = Join-Path $script:Repo '.venv/Scripts/python.exe'
if (-not (Test-Path $script:Python)) { $script:Python = 'python' }
$script:ImagesFile = Join-Path $script:Repo '.edge-images.json'

# az writes warnings to stderr. Windows PowerShell 5.1 turns redirected stderr into error records,
# which 'Stop' would make fatal, so az runs under 'Continue' and its exit code decides success.
function Invoke-AzRaw {
    $ErrorActionPreference = 'Continue'
    & az @args
}

function Invoke-Az {
    $output = Invoke-AzRaw @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args[0..2] -join ' ') failed (exit $LASTEXITCODE)" }
    return $output
}

function Invoke-Edgectl {
    $ErrorActionPreference = 'Continue'
    $output = & $script:Python -m service @args
    if ($LASTEXITCODE -ne 0) { throw "edgectl $($args[0..1] -join ' ') failed (exit $LASTEXITCODE)" }
    return ($output -join "`n")
}

# New role assignments take a few minutes to apply; retry operations that depend on them.
function Invoke-WithRetry {
    param([scriptblock]$Action, [string]$What, [int]$Attempts = 10, [int]$DelaySeconds = 30)
    for ($attempt = 1; ; $attempt++) {
        try { return & $Action }
        catch {
            if ($attempt -ge $Attempts) { throw }
            Write-Host "$What not ready yet (attempt $attempt): retrying in $DelaySeconds s" -ForegroundColor Yellow
            Start-Sleep -Seconds $DelaySeconds
        }
    }
}

function Read-DotEnv {
    $values = @{}
    $path = Join-Path $script:Repo '.env'
    if (-not (Test-Path $path)) { throw '.env not found; run infra/deploy.ps1 first.' }
    foreach ($line in Get-Content $path) {
        if ($line -match '^\s*([A-Z_]+)=(.*)$') { $values[$Matches[1]] = $Matches[2].Trim() }
    }
    return $values
}

# Run a script from infra/edge on the VM with Azure Run Command and return the text after the
# line that starts with $Marker. Output comes back only after the script ends (last 4 KB).
function Invoke-EdgeScript {
    param(
        [string]$ResourceGroup, [string]$VmName, [string]$Script,
        [string[]]$Arguments = @(), [string]$Marker
    )
    $source = Join-Path $PSScriptRoot "edge/$Script"
    $temp = Join-Path ([IO.Path]::GetTempPath()) "edge-$([guid]::NewGuid().ToString('N')).sh"
    [IO.File]::WriteAllText($temp, ((Get-Content $source -Raw) -replace "`r`n", "`n"))
    try {
        $command = @('vm', 'run-command', 'invoke', '--resource-group', $ResourceGroup, '--name', $VmName,
            '--command-id', 'RunShellScript', '--scripts', "@$temp", '--query', 'value[0].message', '-o', 'json')
        if ($Arguments.Count) { $command += @('--parameters') + $Arguments }
        $message = (Invoke-Az @command) -join "`n" | ConvertFrom-Json
    } finally {
        Remove-Item $temp -ErrorAction SilentlyContinue
    }
    $line = ($message -split "`n") | Where-Object { $_ -like "$Marker *" } | Select-Object -Last 1
    if (-not $line) { throw "$Script did not report '$Marker'. Output:`n$message" }
    return $line.Substring($Marker.Length + 1).Trim()
}

function Publish-BuildContext {
    param([string]$StorageAccount)
    $outDir = Join-Path ([IO.Path]::GetTempPath()) 'edge-ml-build'
    $package = (& $script:Python (Join-Path $PSScriptRoot 'package.py') $script:Repo $outDir) | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Packaging the build context failed.' }
    Invoke-WithRetry -What 'Upload to the build container' -Action {
        Invoke-Az storage blob upload --auth-mode login --account-name $StorageAccount --container-name build `
            --name $package.blob --file $package.path --overwrite --only-show-errors -o none | Out-Null
    }
    Remove-Item $package.path -ErrorAction SilentlyContinue
    Write-Host "Build context $($package.blob) uploaded"
    return $package.blob
}

function Build-EdgeImages {
    param([hashtable]$Env, [string]$Blob, [string]$Model)
    Write-Host "Building images on $($Env.VM_NAME) with model $Model (a few minutes)"
    $json = Invoke-EdgeScript -ResourceGroup $Env.RESOURCE_GROUP -VmName $Env.VM_NAME -Script 'build-images.sh' `
        -Arguments @($Env.STORAGE_ACCOUNT_NAME, $Blob, $Env.ACR_LOGIN_SERVER, $Model) -Marker 'IMAGES'
    $images = $json | ConvertFrom-Json
    [IO.File]::WriteAllText($script:ImagesFile, ($images | ConvertTo-Json))
    Write-Host "  press-sensor     $($images.'press-sensor')"
    Write-Host "  anomaly-detector $($images.'anomaly-detector')"
    return $images
}

# Give the device a fresh, expiring pull password and apply the manifest. The password goes from
# az to an environment variable read by edgectl; it is never printed or written to disk.
function Publish-Deployment {
    param([hashtable]$Env, [int]$PasswordDays = 7)
    $password = Invoke-Az acr token credential generate --registry $Env.ACR_NAME --name $Env.ACR_PULL_TOKEN `
        --expiration-in-days $PasswordDays --password1 --query 'passwords[0].value' -o tsv
    Write-Host 'New registry pull password generated; waiting 60 s for it to replicate'
    Start-Sleep -Seconds 60
    $env:ACR_PULL_PASSWORD = ($password -join '').Trim()
    try {
        $result = Invoke-WithRetry -What 'IoT Hub access' -Attempts 6 -Action {
            Invoke-Edgectl deploy --images $script:ImagesFile
        }
    } finally {
        Remove-Item Env:ACR_PULL_PASSWORD -ErrorAction SilentlyContinue
        $password = $null
    }
    Write-Host "Deployment applied to $($Env.EDGE_DEVICE_ID)"
    return $result
}
