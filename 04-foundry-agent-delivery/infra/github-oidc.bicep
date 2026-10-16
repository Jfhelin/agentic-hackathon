targetScope = 'subscription'

param location string
param identityResourceGroupName string
param devResourceGroupName string
param devAccountName string
param devProjectName string
param prodResourceGroupName string
param prodAccountName string
param prodProjectName string
param identityPrefix string
param devOidcSubject string
param prodOidcSubject string

var environmentNames = ['dev', 'prod']

resource identityResourceGroup 'Microsoft.Resources/resourceGroups@2022-09-01' existing = {
  name: identityResourceGroupName
}

resource devAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: devAccountName
  scope: resourceGroup(devResourceGroupName)
}

resource devProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: devAccount
  name: devProjectName
}

resource prodAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: prodAccountName
  scope: resourceGroup(prodResourceGroupName)
}

resource prodProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: prodAccount
  name: prodProjectName
}

resource agentRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(subscription().id, 'workshop-foundry-agent-release')
  properties: {
    roleName: 'Workshop Foundry agent release ${uniqueString(subscription().id)}'
    description: 'Read/create prompt agent versions and invoke responses at assigned scopes.'
    type: 'CustomRole'
    assignableScopes: [subscription().id]
    permissions: [{
      actions: ['Microsoft.CognitiveServices/accounts/projects/read']
      notActions: []
      dataActions: [
        'Microsoft.CognitiveServices/accounts/AIServices/agents/read'
        'Microsoft.CognitiveServices/accounts/AIServices/agents/write'
        'Microsoft.CognitiveServices/accounts/AIServices/responses/read'
        'Microsoft.CognitiveServices/accounts/AIServices/responses/write'
      ]
      notDataActions: []
    }]
  }
}

resource inferenceRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(subscription().id, 'workshop-foundry-agent-inference')
  properties: {
    roleName: 'Workshop Foundry inference ${uniqueString(subscription().id)}'
    description: 'Read configured account deployments and invoke Responses API at assigned scopes.'
    type: 'CustomRole'
    assignableScopes: [subscription().id]
    permissions: [{
      actions: [
        'Microsoft.CognitiveServices/accounts/read'
        'Microsoft.CognitiveServices/accounts/deployments/read'
      ]
      notActions: []
      dataActions: [
        'Microsoft.CognitiveServices/accounts/OpenAI/responses/read'
        'Microsoft.CognitiveServices/accounts/OpenAI/responses/write'
      ]
      notDataActions: []
    }]
  }
}

resource evaluationRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(subscription().id, 'workshop-foundry-evaluation')
  properties: {
    roleName: 'Workshop Foundry evaluation ${uniqueString(subscription().id)}'
    description: 'Create/read Foundry evaluations and evaluator assets. Asset write is broader than evaluator-only access.'
    type: 'CustomRole'
    assignableScopes: [subscription().id]
    permissions: [{
      actions: []
      notActions: []
      dataActions: [
        'Microsoft.CognitiveServices/accounts/AIServices/assets/read'
        'Microsoft.CognitiveServices/accounts/AIServices/assets/write'
        'Microsoft.CognitiveServices/accounts/AIServices/evaluations/read'
        'Microsoft.CognitiveServices/accounts/AIServices/evaluations/write'
        'Microsoft.CognitiveServices/accounts/OpenAI/evals/read'
        'Microsoft.CognitiveServices/accounts/OpenAI/evals/write'
      ]
      notDataActions: []
    }]
  }
}

resource judgeRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(subscription().id, 'workshop-foundry-evaluation-judge')
  properties: {
    roleName: 'Workshop Foundry judge ${uniqueString(subscription().id)}'
    description: 'Invoke chat completions for Foundry rubric scoring; no deployment or key access.'
    type: 'CustomRole'
    assignableScopes: [subscription().id]
    permissions: [{
      actions: []
      notActions: []
      dataActions: [
        'Microsoft.CognitiveServices/accounts/OpenAI/deployments/chat/completions/action'
      ]
      notDataActions: []
    }]
  }
}

resource identities 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [for environmentName in environmentNames: {
  name: '${identityPrefix}-${environmentName}'
  location: location
  scope: resourceGroup(identityResourceGroupName)
}]

resource federatedCredentials 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = [for (environmentName, index) in environmentNames: {
  parent: identities[index]
  name: 'github-${environmentName}'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    subject: index == 0 ? devOidcSubject : prodOidcSubject
    audiences: ['api://AzureADTokenExchange']
  }
}]

resource devAgentAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(devProject.id, identities[0].id, agentRole.id)
  scope: devProject
  properties: {
    roleDefinitionId: agentRole.id
    principalId: identities[0].properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource prodAgentAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(prodProject.id, identities[1].id, agentRole.id)
  scope: prodProject
  properties: {
    roleDefinitionId: agentRole.id
    principalId: identities[1].properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource devInferenceAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(devAccount.id, identities[0].id, inferenceRole.id)
  scope: devAccount
  properties: {
    roleDefinitionId: inferenceRole.id
    principalId: identities[0].properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource prodInferenceAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(prodAccount.id, identities[1].id, inferenceRole.id)
  scope: prodAccount
  properties: {
    roleDefinitionId: inferenceRole.id
    principalId: identities[1].properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource devEvaluationAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(devProject.id, identities[0].id, evaluationRole.id)
  scope: devProject
  properties: {
    roleDefinitionId: evaluationRole.id
    principalId: identities[0].properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource devJudgeAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(devAccount.id, identities[0].id, judgeRole.id)
  scope: devAccount
  properties: {
    roleDefinitionId: judgeRole.id
    principalId: identities[0].properties.principalId
    principalType: 'ServicePrincipal'
  }
}

output clientIds array = [for identity in identities: identity.properties.clientId]
