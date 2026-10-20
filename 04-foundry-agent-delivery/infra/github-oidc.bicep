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

module identities 'modules/github-identities.bicep' = {
  name: '${identityPrefix}-identities'
  scope: resourceGroup(identityResourceGroupName)
  params: {
    location: location
    identityPrefix: identityPrefix
    devOidcSubject: devOidcSubject
    prodOidcSubject: prodOidcSubject
  }
}

module devAccess 'modules/foundry-access.bicep' = {
  name: '${identityPrefix}-access-dev'
  scope: resourceGroup(devResourceGroupName)
  params: {
    accountName: devAccountName
    projectName: devProjectName
    identityId: identities.outputs.ids[0]
    principalId: identities.outputs.principalIds[0]
    agentRoleId: agentRole.id
    inferenceRoleId: inferenceRole.id
    evaluationRoleId: evaluationRole.id
    judgeRoleId: judgeRole.id
  }
}

module prodAccess 'modules/foundry-access.bicep' = {
  name: '${identityPrefix}-access-prod'
  scope: resourceGroup(prodResourceGroupName)
  params: {
    accountName: prodAccountName
    projectName: prodProjectName
    identityId: identities.outputs.ids[1]
    principalId: identities.outputs.principalIds[1]
    agentRoleId: agentRole.id
    inferenceRoleId: inferenceRole.id
  }
}

output clientIds array = identities.outputs.clientIds
