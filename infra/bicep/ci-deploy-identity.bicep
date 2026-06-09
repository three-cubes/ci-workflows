// ============================================================================
// ci-deploy-identity.bicep — User-assigned MI + federated credential + RBAC
// for a GitHub Actions WIF-based deploy workflow.
//
// One template, instantiated per repo. Collapses the 30-minute manual runbook
// (devsecops/runbooks/runbook-ci-driven-apply.md in tc-agent-zone) into a
// single `az deployment` call.
//
// Usage:
//   az deployment group create \
//     --resource-group RG-AGENTS-CORE \
//     --template-file infra/bicep/ci-deploy-identity.bicep \
//     --parameters \
//       repoOwner=three-cubes \
//       repoName=tc-agent-zone \
//       environmentName=production \
//       keyVaultName=kv-tc-agents
//
// After deployment, populate the calling repo's GitHub variables with the
// outputs:
//   gh variable set AZURE_CLIENT_ID --body "$(az deployment group show ...
//       --query 'properties.outputs.clientId.value' -o tsv)"
//   ...same for tenantId, subscriptionId
// ============================================================================

@description('GitHub repo owner (org or user, e.g. three-cubes).')
param repoOwner string

@description('GitHub repo name (e.g. tc-agent-zone).')
param repoName string

@description('GitHub Actions environment the workflow targets. Set to empty string if no environment is used.')
param environmentName string = 'production'

@description('Branch the federated credential authorises (typically main).')
param targetBranch string = 'main'

@description('Identity name. Defaults to mi-github-deploy-<repoName> to keep multi-repo deployments distinct in one RG.')
param identityName string = 'mi-github-deploy-${repoName}'

@description('Azure region for the MI. Defaults to the parent RG\'s location.')
param location string = resourceGroup().location

@description('Name of the Key Vault the workflow needs read access to. Empty string disables this assignment.')
param keyVaultName string = ''

@description('Set to false to skip granting Disk Snapshot Contributor on the RG (e.g. if the deploy doesn\'t snapshot).')
param grantSnapshotContributor bool = true

@description('Set to false to skip granting VM Contributor on the RG (e.g. if the deploy doesn\'t use az vm run-command).')
param grantVmContributor bool = true

// ----------------------------------------------------------------------------
// User-assigned managed identity
// ----------------------------------------------------------------------------
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
}

// ----------------------------------------------------------------------------
// Federated credential — branch-scoped (push/PR-from-main triggers)
// ----------------------------------------------------------------------------
resource fedCredBranch 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: identity
  name: 'github-${repoOwner}-${repoName}-${targetBranch}'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    subject: 'repo:${repoOwner}/${repoName}:ref:refs/heads/${targetBranch}'
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}

// ----------------------------------------------------------------------------
// Federated credential — environment-scoped (workflow_dispatch + push when
// the job targets a GitHub Actions environment). Required when the workflow
// has `environment: production` (which our shared workflow does).
// ----------------------------------------------------------------------------
resource fedCredEnv 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = if (!empty(environmentName)) {
  parent: identity
  name: 'github-${repoOwner}-${repoName}-env-${environmentName}'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    subject: 'repo:${repoOwner}/${repoName}:environment:${environmentName}'
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}

// ----------------------------------------------------------------------------
// RBAC — Virtual Machine Contributor on the parent RG
// Required for `az vm run-command invoke`. Scope is the RG, not the whole
// subscription — least privilege.
// ----------------------------------------------------------------------------
var vmContributorRoleId = '9980e02c-c2be-4d73-94e8-173b1dc7cf3c'
resource vmContribAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (grantVmContributor) {
  name: guid(resourceGroup().id, identity.id, vmContributorRoleId)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', vmContributorRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ----------------------------------------------------------------------------
// RBAC — Disk Snapshot Contributor on the parent RG
// Required for the snapshot-before-apply step. WARNING: this built-in role
// includes snapshot+storage DELETE; deliberately NOT granted to runtime VM
// MIs. Granting it here scopes blast-radius to the trusted CI identity only.
// ----------------------------------------------------------------------------
var diskSnapshotContributorRoleId = '7efff54f-10ef-46a0-bcfe-d9aedaab8a86'
resource snapshotContribAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (grantSnapshotContributor) {
  name: guid(resourceGroup().id, identity.id, diskSnapshotContributorRoleId)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', diskSnapshotContributorRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ----------------------------------------------------------------------------
// RBAC — Key Vault Secrets User on the named KV (if provided)
// ----------------------------------------------------------------------------
var kvSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' existing = if (!empty(keyVaultName)) {
  name: keyVaultName
}
resource kvSecretsAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(keyVaultName)) {
  name: guid(kv.id, identity.id, kvSecretsUserRoleId)
  scope: kv
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ----------------------------------------------------------------------------
// Outputs — populate as GitHub repo variables after deployment
// ----------------------------------------------------------------------------
output clientId string = identity.properties.clientId
output principalId string = identity.properties.principalId
output tenantId string = identity.properties.tenantId
output subscriptionId string = subscription().subscriptionId
output identityResourceId string = identity.id
