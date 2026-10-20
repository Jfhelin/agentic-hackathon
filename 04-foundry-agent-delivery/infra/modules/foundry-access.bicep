targetScope = 'resourceGroup'

param accountName string
param projectName string
param identityId string
param principalId string
param agentRoleId string
param inferenceRoleId string
@description('Empty to skip the evaluation and judge assignments (prod).')
param evaluationRoleId string = ''
param judgeRoleId string = ''

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: account
  name: projectName
}

resource agentAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, identityId, agentRoleId)
  scope: project
  properties: {
    roleDefinitionId: agentRoleId
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}

resource inferenceAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, identityId, inferenceRoleId)
  scope: account
  properties: {
    roleDefinitionId: inferenceRoleId
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}

resource evaluationAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(evaluationRoleId)) {
  name: guid(project.id, identityId, evaluationRoleId)
  scope: project
  properties: {
    roleDefinitionId: evaluationRoleId
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}

resource judgeAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(judgeRoleId)) {
  name: guid(account.id, identityId, judgeRoleId)
  scope: account
  properties: {
    roleDefinitionId: judgeRoleId
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}
