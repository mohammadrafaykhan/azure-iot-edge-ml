// Message routing for the hub, applied after its managed identity can write to the container.
// The detector's alerts and summaries go to Blob storage as JSON; everything else falls back to
// the built-in endpoint. A free hub allows one custom endpoint, which this uses.

param hubName string
param location string
param baseProperties object
param storageAccountName string
param containerName string

var endpointName = 'edge-insights'

// Kept as a plain value: a quoted literal inside the union() expression below fails ARM validation.
var detectorCondition = '$connectionModuleId = \'anomaly-detector\''

resource hub 'Microsoft.Devices/IotHubs@2023-06-30' = {
  name: hubName
  location: location
  sku: { name: 'F1', capacity: 1 }
  identity: { type: 'SystemAssigned' }
  properties: union(baseProperties, {
    routing: {
      endpoints: {
        eventHubs: []
        serviceBusQueues: []
        serviceBusTopics: []
        storageContainers: [
          {
            name: endpointName
            authenticationType: 'identityBased'
            endpointUri: 'https://${storageAccountName}.blob.${environment().suffixes.storage}/'
            containerName: containerName
            encoding: 'JSON'
            fileNameFormat: '{iothub}/{YYYY}/{MM}/{DD}/{HH}/{mm}/{partition}'
            batchFrequencyInSeconds: 60
            maxChunkSizeInBytes: 10485760
            subscriptionId: subscription().subscriptionId
            resourceGroup: resourceGroup().name
          }
        ]
      }
      routes: [
        {
          name: 'edge-insights'
          source: 'DeviceMessages'
          condition: detectorCondition
          endpointNames: [endpointName]
          isEnabled: true
        }
      ]
      fallbackRoute: {
        name: '$fallback'
        source: 'DeviceMessages'
        condition: 'true'
        endpointNames: ['events']
        isEnabled: true
      }
    }
  })
}
