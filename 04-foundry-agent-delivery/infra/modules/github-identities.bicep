targetScope = 'resourceGroup'

param location string
param identityPrefix string
param devOidcSubject string
param prodOidcSubject string

var environmentNames = ['dev', 'prod']

resource identities 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [for environmentName in environmentNames: {
  name: '${identityPrefix}-${environmentName}'
  location: location
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

output ids array = [for (environmentName, index) in environmentNames: identities[index].id]
output principalIds array = [for (environmentName, index) in environmentNames: identities[index].properties.principalId]
output clientIds array = [for (environmentName, index) in environmentNames: identities[index].properties.clientId]
