/*
  rake Azure Infrastructure
  ─────────────────────────
  Deploys the complete rake microservices stack to Azure:

  • Resource Group
  • Storage Account (blobs: uploads, rake-results, data-results)
  • Service Bus Namespace + queues (code-review-jobs, security-audit-jobs, security-alerts)
  • Application Insights + Log Analytics workspace
  • Azure Container Registry (stores rake Docker image)
  • Azure Container Apps Environment
  • Three Container Apps:
      rake-code-review      — code review service
      rake-security-audit   — security audit service
      rake-data-analysis    — data analysis service

  Deploy:
    az deployment group create \
      --resource-group rg-rake \
      --template-file infra/main.bicep \
      --parameters @infra/main.parameters.json
*/

targetScope = 'resourceGroup'

@description('Azure region')
param location string = resourceGroup().location

@description('Unique suffix for globally-unique resource names (e.g. abc123)')
param suffix string

@description('Container image tag (e.g. sha-abc123)')
param imageTag string = 'latest'

@description('Anthropic API key (stored in Key Vault)')
@secure()
param anthropicApiKey string

@description('Minimum replicas per Container App (0 = scale-to-zero)')
param minReplicas int = 0

@description('Maximum replicas per Container App')
param maxReplicas int = 10

// ── Variables ────────────────────────────────────────────────────────────────

var storageAccountName = 'strake${suffix}'
var acrName = 'acrake${suffix}'
var appInsightsName = 'appi-rake-${suffix}'
var logAnalyticsName = 'law-rake-${suffix}'
var sbNamespaceName = 'sb-rake-${suffix}'
var containerEnvName = 'cae-rake-${suffix}'
var imageBase = '${acrName}.azurecr.io/rake'

// ── Storage Account ───────────────────────────────────────────────────────────

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storage
  name: 'default'
}

var containers = ['uploads', 'rake-results', 'data-results', 'data-uploads']

resource blobContainers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = [for name in containers: {
  parent: blobService
  name: name
  properties: { publicAccess: 'None' }
}]

// ── Service Bus ───────────────────────────────────────────────────────────────

resource sbNamespace 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: sbNamespaceName
  location: location
  sku: { name: 'Standard', tier: 'Standard' }
}

var queues = ['code-review-jobs', 'security-audit-jobs', 'security-alerts']

resource sbQueues 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = [for q in queues: {
  parent: sbNamespace
  name: q
  properties: {
    defaultMessageTimeToLive: 'P1D'
    maxDeliveryCount: 3
  }
}]

resource sbListenSend 'Microsoft.ServiceBus/namespaces/authorizationRules@2022-10-01-preview' = {
  parent: sbNamespace
  name: 'rake-services'
  properties: { rights: ['Listen', 'Send'] }
}

// ── Log Analytics + Application Insights ─────────────────────────────────────

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: logAnalyticsName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// ── Container Registry ────────────────────────────────────────────────────────

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: true }
}

// ── Container Apps Environment ────────────────────────────────────────────────

resource containerEnv 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: containerEnvName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// ── Shared environment variables ──────────────────────────────────────────────

var sharedEnv = [
  { name: 'RAKE_LLM',          value: 'claude' }
  { name: 'RAKE_MODEL',        value: 'claude-sonnet-4-6' }
  { name: 'RAKE_TIMEOUT',      value: '240' }
  { name: 'RAKE_BINARY',       value: '/usr/local/bin/rake' }
  { name: 'RESULTS_CONTAINER', value: 'rake-results' }
  { name: 'DATA_RESULTS_CONTAINER', value: 'data-results' }
  {
    name: 'AZURE_STORAGE_CONNECTION_STRING'
    value: 'DefaultEndpointsProtocol=https;AccountName=${storageAccountName};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=core.windows.net'
  }
  {
    name: 'SERVICE_BUS_CONNECTION'
    value: sbListenSend.listKeys().primaryConnectionString
  }
  {
    name: 'APPINSIGHTS_INSTRUMENTATIONKEY'
    value: appInsights.properties.InstrumentationKey
  }
  {
    name: 'ANTHROPIC_API_KEY'
    secretRef: 'anthropic-api-key'
  }
]

var sharedSecrets = [
  { name: 'anthropic-api-key', value: anthropicApiKey }
  { name: 'acr-password',      value: acr.listCredentials().passwords[0].value }
]

var acrRegistries = [
  {
    server: '${acrName}.azurecr.io'
    username: acr.listCredentials().username
    passwordSecretRef: 'acr-password'
  }
]

// ── Container App: code-review ────────────────────────────────────────────────

resource codeReviewApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: 'rake-code-review'
  location: location
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      secrets: sharedSecrets
      registries: acrRegistries
      ingress: {
        external: true
        targetPort: 80
        transport: 'http'
      }
    }
    template: {
      containers: [
        {
          name: 'code-review'
          image: '${imageBase}:${imageTag}'
          env: sharedEnv
          resources: { cpu: json('0.5'), memory: '1Gi' }
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'sb-scale'
            custom: {
              type: 'azure-servicebus'
              metadata: {
                queueName: 'code-review-jobs'
                namespace: sbNamespaceName
                messageCount: '5'
              }
              auth: [{ secretRef: 'anthropic-api-key', triggerParameter: 'connection' }]
            }
          }
        ]
      }
    }
  }
}

// ── Container App: security-audit ─────────────────────────────────────────────

resource securityAuditApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: 'rake-security-audit'
  location: location
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      secrets: sharedSecrets
      registries: acrRegistries
      ingress: {
        external: true
        targetPort: 80
        transport: 'http'
      }
    }
    template: {
      containers: [
        {
          name: 'security-audit'
          image: '${imageBase}:${imageTag}'
          env: sharedEnv
          resources: { cpu: json('0.5'), memory: '1Gi' }
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
    }
  }
}

// ── Container App: data-analysis ──────────────────────────────────────────────

resource dataAnalysisApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: 'rake-data-analysis'
  location: location
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      secrets: sharedSecrets
      registries: acrRegistries
      ingress: {
        external: true
        targetPort: 80
        transport: 'http'
      }
    }
    template: {
      containers: [
        {
          name: 'data-analysis'
          image: '${imageBase}:${imageTag}'
          env: sharedEnv
          resources: { cpu: json('0.5'), memory: '1Gi' }
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
    }
  }
}

// ── Outputs ───────────────────────────────────────────────────────────────────

output codeReviewUrl string = 'https://${codeReviewApp.properties.configuration.ingress.fqdn}'
output securityAuditUrl string = 'https://${securityAuditApp.properties.configuration.ingress.fqdn}'
output dataAnalysisUrl string = 'https://${dataAnalysisApp.properties.configuration.ingress.fqdn}'
output acrLoginServer string = acr.properties.loginServer
output appInsightsKey string = appInsights.properties.InstrumentationKey
