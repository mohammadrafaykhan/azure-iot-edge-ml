// Edge ML line: an Ubuntu VM running IoT Edge 1.6 (the edge device), IoT Hub (F1) routing the
// device's alerts and summaries to Blob storage with its managed identity, and a private container
// registry the device pulls from with a repository-scoped token. The VM has no inbound ports; it
// is managed through Azure Run Command, which Azure RBAC governs.

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Object ID of the operator who runs edgectl and the deploy scripts.')
param operatorPrincipalId string

@description('Public half of an SSH key the VM requires at creation. No inbound port is open, so it is never used.')
@secure()
param sshPublicKey string

@description('Admin account name on the VM (key-only; no inbound port reaches it).')
param adminUsername string = 'edgeadmin'

@description('VM size for the edge device.')
param vmSize string = 'Standard_B2als_v2'

@description('IoT Edge device ID.')
param deviceId string = 'edge-01'

@description('Daily auto-shutdown time (HHmm) in the time zone below; a cost guard if teardown is forgotten.')
param autoShutdownTime string = '2300'

@description('Windows time zone ID for the auto-shutdown schedule.')
param autoShutdownTimeZone string = 'Pakistan Standard Time'

var suffix = uniqueString(resourceGroup().id)
var hubName = 'iot-edge-${suffix}'
var storageName = 'stedge${suffix}'
var registryName = 'acredge${suffix}'
var vmName = 'vm-edge-${suffix}'
var insightsContainer = 'edge-insights'
var buildContainer = 'build'
var pullTokenName = 'edge-pull'
var repositories = ['edge-ml/press-sensor', 'edge-ml/anomaly-detector']

var roles = {
  acrPush: '8311e382-0749-4cb8-b61a-304f252e45ec'
  storageBlobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  storageBlobDataReader: '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
  iotHubDataContributor: '4fc6c259-987e-4a07-842e-c321cc9d413f'
}

// ---------------------------------------------------------------- storage

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource insights 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: insightsContainer
}

resource builds 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: buildContainer
}

// ---------------------------------------------------------------- container registry

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

// The device can read the two module repositories and nothing else. The token's password is
// generated with an expiry by the deploy script, never stored in a template or file.
resource pullScope 'Microsoft.ContainerRegistry/registries/scopeMaps@2023-07-01' = {
  parent: registry
  name: pullTokenName
  properties: {
    description: 'Pull-only access to the edge module repositories'
    actions: flatten(map(repositories, repo => ['repositories/${repo}/content/read', 'repositories/${repo}/metadata/read']))
  }
}

resource pullToken 'Microsoft.ContainerRegistry/registries/tokens@2023-07-01' = {
  parent: registry
  name: pullTokenName
  properties: {
    scopeMapId: pullScope.id
    status: 'enabled'
  }
}

// ---------------------------------------------------------------- IoT hub

var hubProperties = {
  disableLocalAuth: true
  eventHubEndpoints: {
    events: { retentionTimeInDays: 1, partitionCount: 2 }
  }
  features: 'None'
}

resource hub 'Microsoft.Devices/IotHubs@2023-06-30' = {
  name: hubName
  location: location
  sku: { name: 'F1', capacity: 1 }
  identity: { type: 'SystemAssigned' }
  properties: hubProperties
}

resource hubWritesInsights 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(insights.id, hub.id, roles.storageBlobDataContributor)
  scope: insights
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataContributor)
    principalId: hub.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// The identity-based endpoint can only be added once the hub's identity holds its role.
module routing 'hub-routing.bicep' = {
  name: 'hub-routing'
  params: {
    hubName: hub.name
    location: location
    baseProperties: hubProperties
    storageAccountName: storage.name
    containerName: insights.name
  }
  dependsOn: [hubWritesInsights]
}

// ---------------------------------------------------------------- network (outbound only)

resource nsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-edge-${suffix}'
  location: location
  properties: {
    securityRules: [] // the default rules deny all inbound traffic from the internet
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-edge-${suffix}'
  location: location
  properties: {
    addressSpace: { addressPrefixes: ['10.40.0.0/24'] }
    subnets: [
      {
        name: 'edge'
        properties: {
          addressPrefix: '10.40.0.0/26'
          networkSecurityGroup: { id: nsg.id }
          defaultOutboundAccess: false
        }
      }
    ]
  }
}

// Explicit outbound path (new subnets have no default outbound access).
resource publicIp 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: 'pip-edge-${suffix}'
  location: location
  sku: { name: 'Standard', tier: 'Regional' }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource nic 'Microsoft.Network/networkInterfaces@2024-05-01' = {
  name: 'nic-edge-${suffix}'
  location: location
  properties: {
    ipConfigurations: [
      {
        name: 'primary'
        properties: {
          subnet: { id: vnet.properties.subnets[0].id }
          privateIPAllocationMethod: 'Dynamic'
          publicIPAddress: { id: publicIp.id }
        }
      }
    ]
  }
}

// ---------------------------------------------------------------- edge VM

var cloudInit = replace(
  replace(loadTextContent('edge/cloud-init.yaml'), '__HUB_HOSTNAME__', '${hubName}.azure-devices.net'),
  '__DEVICE_ID__',
  deviceId
)

resource vm 'Microsoft.Compute/virtualMachines@2024-07-01' = {
  name: vmName
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    hardwareProfile: { vmSize: vmSize }
    securityProfile: {
      securityType: 'TrustedLaunch'
      uefiSettings: { secureBootEnabled: true, vTpmEnabled: true }
    }
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: 'ubuntu-24_04-lts'
        sku: 'server'
        version: 'latest'
      }
      osDisk: {
        createOption: 'FromImage'
        diskSizeGB: 30
        deleteOption: 'Delete'
        managedDisk: { storageAccountType: 'StandardSSD_LRS' }
      }
    }
    osProfile: {
      computerName: deviceId
      adminUsername: adminUsername
      customData: base64(cloudInit)
      linuxConfiguration: {
        disablePasswordAuthentication: true
        provisionVMAgent: true
        ssh: {
          publicKeys: [{ path: '/home/${adminUsername}/.ssh/authorized_keys', keyData: sshPublicKey }]
        }
      }
    }
    networkProfile: {
      networkInterfaces: [{ id: nic.id, properties: { deleteOption: 'Delete' } }]
    }
    diagnosticsProfile: { bootDiagnostics: { enabled: true } }
  }
}

resource autoShutdown 'Microsoft.DevTestLab/schedules@2018-09-15' = {
  name: 'shutdown-computevm-${vm.name}'
  location: location
  properties: {
    status: 'Enabled'
    taskType: 'ComputeVmShutdownTask'
    dailyRecurrence: { time: autoShutdownTime }
    timeZoneId: autoShutdownTimeZone
    targetResourceId: vm.id
    notificationSettings: { status: 'Disabled' }
  }
}

// The VM is also the image builder: it reads the build context and pushes to the registry.
resource vmPushesImages 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, vm.id, roles.acrPush)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPush)
    principalId: vm.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource vmReadsBuilds 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(builds.id, vm.id, roles.storageBlobDataReader)
  scope: builds
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataReader)
    principalId: vm.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------- operator (edgectl, deploy scripts)

resource operatorHub 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(hub.id, operatorPrincipalId, roles.iotHubDataContributor)
  scope: hub
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.iotHubDataContributor)
    principalId: operatorPrincipalId
    principalType: 'User'
  }
}

resource operatorUploadsBuilds 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(builds.id, operatorPrincipalId, roles.storageBlobDataContributor)
  scope: builds
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataContributor)
    principalId: operatorPrincipalId
    principalType: 'User'
  }
}

resource operatorReadsInsights 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(insights.id, operatorPrincipalId, roles.storageBlobDataReader)
  scope: insights
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataReader)
    principalId: operatorPrincipalId
    principalType: 'User'
  }
}

output iotHubName string = hub.name
output iotHubHostName string = hub.properties.hostName
output storageAccountName string = storage.name
output registryLoginServer string = registry.properties.loginServer
output registryName string = registry.name
output pullTokenName string = pullToken.name
output vmName string = vm.name
output deviceId string = deviceId
