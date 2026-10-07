#Requires -Version 7.0
<#
.SYNOPSIS
Provision a small Azure lab and deploy a healthy two-agent PoC. No fault injection.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SubscriptionId,
    [ValidatePattern('^[a-zA-Z0-9_-]{1,80}$')][string]$ResourceGroup = 'rg-agentic-ops-poc-lab',
    [string]$Location = 'polandcentral',
    [string]$AiLocation = 'swedencentral',
    [string]$NodeVmSize = 'Standard_D2as_v4',
    [string]$ModelName = 'gpt-4.1-nano',
    [string]$ModelVersion = '2025-04-14',
    [string]$ModelDeployment = 'diagnostic-gpt41nano',
    [ValidateRange(1,100)][int]$ModelCapacity = 1
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
# Native command exit codes are checked explicitly, including commands captured as JSON.
$PSNativeCommandUseErrorActionPreference = $false
$root = Split-Path $PSScriptRoot -Parent
foreach ($command in 'az','kubectl','git') {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) { throw "Install $command first (Azure Cloud Shell PowerShell includes these tools)." }
}
foreach ($file in 'Dockerfile','deploy/aks-agentic-ops.yaml','deploy/demo-payments-api.yaml') {
    if (-not (Test-Path (Join-Path $root $file))) { throw "Missing repository file: $file" }
}
function Invoke-Az {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
    $output = & az @Arguments --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw "Azure CLI failed: az $($Arguments[0..([Math]::Min(2,$Arguments.Count-1))] -join ' ')" }
    return $output
}
function Get-AzJson {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
    return (Invoke-Az ($Arguments + @('-o','json')) | Out-String | ConvertFrom-Json)
}
function Invoke-Kube {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
    $output = & kubectl --kubeconfig $script:kubeconfig @Arguments
    if ($LASTEXITCODE -ne 0) { throw "kubectl failed: $($Arguments -join ' ')" }
    return $output
}
function Grant-Role {
    param([string]$Principal,[string]$Role,[string]$Scope)
    for ($attempt=1; $attempt -le 12; $attempt++) {
        & az role assignment create --assignee-object-id $Principal --assignee-principal-type ServicePrincipal --role $Role --scope $Scope --only-show-errors -o none
        if ($LASTEXITCODE -eq 0) { return }
        if ($attempt -lt 12) { Start-Sleep -Seconds 10 }
    }
    throw "Could not assign $Role. Check role-assignment permission and identity propagation."
}
function Render-Manifest {
    param([string]$Source,[string]$Destination,[hashtable]$Values)
    $manifest = Get-Content (Join-Path $root $Source) -Raw
    if ([string]::IsNullOrWhiteSpace($manifest)) { throw "Empty manifest: $Source" }
    foreach ($key in $Values.Keys) {
        if ([string]::IsNullOrWhiteSpace([string]$Values[$key])) { throw "Missing manifest value: $key" }
        $manifest = $manifest.Replace(('${'+$key+'}'),[string]$Values[$key])
    }
    if ($manifest -match '\$\{[^}]+\}') { throw "Unresolved placeholder in $Source" }
    Set-Content $Destination $manifest -Encoding utf8
}
$account = Get-AzJson @('account','show')
Invoke-Az @('account','set','--subscription',$SubscriptionId) | Out-Null
$account = Get-AzJson @('account','show')
$SubscriptionId = $account.id
$local = Join-Path $root '.local'
New-Item -ItemType Directory -Force $local | Out-Null
# Persist names before provisioning so failed runs can resume without another lab.
$statePath = Join-Path $local "$ResourceGroup.json"
if (Test-Path $statePath) {
    $state = Get-Content $statePath -Raw | ConvertFrom-Json
    if ($state.subscriptionId -ne $SubscriptionId -or $state.location -ne $Location -or $state.aiLocation -ne $AiLocation -or $state.nodeVmSize -ne $NodeVmSize -or $state.modelName -ne $ModelName -or $state.modelVersion -ne $ModelVersion -or $state.modelDeployment -ne $ModelDeployment -or $state.modelCapacity -ne $ModelCapacity) {
        throw 'Existing local state uses different parameters. Reuse the original parameters or choose a new ResourceGroup.'
    }
} else {
    $exists = Invoke-Az @('group','exists','--name',$ResourceGroup,'-o','tsv')
    if ($exists.Trim() -eq 'true') { throw "Resource group $ResourceGroup already exists without local setup state. Choose a new -ResourceGroup; this script will not adopt unrelated resources." }
    $suffix = [Guid]::NewGuid().ToString('N').Substring(0,10)
    $state = [ordered]@{
        subscriptionId=$SubscriptionId; resourceGroup=$ResourceGroup; location=$Location; aiLocation=$AiLocation
        nodeVmSize=$NodeVmSize; modelName=$ModelName; modelVersion=$ModelVersion
        modelDeployment=$ModelDeployment; modelCapacity=$ModelCapacity
        acrName="acragentpoc$suffix"; aksName='aks-agentic-ops-poc'; aiName="aoai-agentpoc-$suffix"
        diagnosticIdentity='id-agentic-diagnostic'; remediationIdentity='id-agentic-remediation'
        managedNamespace='payments'; imageTag="poc-$suffix"
    }
    $state | ConvertTo-Json | Set-Content $statePath -Encoding utf8
    $state = Get-Content $statePath -Raw | ConvertFrom-Json
}
$work = Join-Path $local $ResourceGroup
New-Item -ItemType Directory -Force $work | Out-Null
$script:kubeconfig = Join-Path $work 'kubeconfig'
Write-Host "Subscription: $SubscriptionId; lab: $ResourceGroup; state: $statePath"
Write-Host 'Cost profile: one AKS node, Free control plane, Basic ACR, pay-per-token model. No Log Analytics, ingress or extra public service.'

Write-Host '[1/7] Check VM SKU and quota, then register providers'
$skus = @(Get-AzJson @('vm','list-skus','--location',$Location,'--resource-type','virtualMachines','--size',$NodeVmSize,'--all'))
$sku = $skus | Where-Object name -EQ $NodeVmSize | Select-Object -First 1
if ($null -eq $sku) { throw "$NodeVmSize is not listed in $Location. Select another -NodeVmSize." }
$blocked = @($sku.restrictions | Where-Object { $_.type -eq 'Location' })
if ($blocked.Count -gt 0) { throw "$NodeVmSize has a location restriction for this subscription. Select another SKU." }
$cores = [int](($sku.capabilities | Where-Object name -EQ 'vCPUs').value)
$usage = @(Get-AzJson @('vm','list-usage','--location',$Location))
$aksList = @(Get-AzJson @('aks','list','--query',"[?resourceGroup=='$ResourceGroup' && name=='$($state.aksName)']"))
if ($aksList.Count -eq 0) {
    foreach ($family in @('cores',$sku.family)) {
        $quota = $usage | Where-Object { $_.name.value -ieq $family } | Select-Object -First 1
        if ($null -eq $quota -or ($quota.limit - $quota.currentValue) -lt $cores) { throw "Insufficient $family quota in $Location for $NodeVmSize. No cluster will be created. Change SKU/region or request quota." }
    }
}
foreach ($provider in 'Microsoft.ContainerService','Microsoft.ContainerRegistry','Microsoft.ManagedIdentity','Microsoft.CognitiveServices','Microsoft.Compute','Microsoft.Network') {
    Invoke-Az @('provider','register','--namespace',$provider,'--wait','-o','none') | Out-Null
}
Invoke-Az @('group','create','--name',$ResourceGroup,'--location',$Location,'--tags','project=agentic-ops-poc','-o','none') | Out-Null

Write-Host '[2/7] Create Basic ACR and the small inference deployment'
$acr = Get-AzJson @('acr','create','--name',$state.acrName,'--resource-group',$ResourceGroup,'--location',$Location,'--sku','Basic','--admin-enabled','false','--role-assignment-mode','rbac')
$ai = Get-AzJson @('cognitiveservices','account','create','--name',$state.aiName,'--resource-group',$ResourceGroup,'--location',$AiLocation,'--kind','OpenAI','--sku','S0','--custom-domain',$state.aiName,'--yes')
Invoke-Az @('cognitiveservices','account','deployment','create','--name',$state.aiName,'--resource-group',$ResourceGroup,'--deployment-name',$ModelDeployment,'--model-name',$ModelName,'--model-version',$ModelVersion,'--model-format','OpenAI','--sku-name','GlobalStandard','--sku-capacity',"$ModelCapacity",'-o','none') | Out-Null

Write-Host '[3/7] Create one-node AKS with Cilium and Workload Identity'
if ($aksList.Count -eq 0) {
    Invoke-Az @('aks','create','--resource-group',$ResourceGroup,'--name',$state.aksName,'--location',$Location,'--tier','free','--node-count','1','--node-vm-size',$NodeVmSize,'--node-osdisk-size','32','--enable-managed-identity','--enable-oidc-issuer','--enable-workload-identity','--network-plugin','azure','--network-plugin-mode','overlay','--network-dataplane','cilium','--attach-acr',$state.acrName,'--generate-ssh-keys','-o','none') | Out-Null
} else {
    if ($aksList[0].provisioningState -ne 'Succeeded') { throw 'Existing cluster is not in Succeeded state. Inspect it before resuming.' }
    if ($aksList[0].powerState.code -eq 'Stopped') { Invoke-Az @('aks','start','-g',$ResourceGroup,'-n',$state.aksName,'-o','none') | Out-Null }
}
$aks = Get-AzJson @('aks','show','-g',$ResourceGroup,'-n',$state.aksName)
Invoke-Az @('aks','get-credentials','-g',$ResourceGroup,'-n',$state.aksName,'--file',$kubeconfig,'--overwrite-existing','-o','none') | Out-Null

Write-Host '[4/7] Configure separate identities and least-privilege roles'
$diagnostic = Get-AzJson @('identity','create','-g',$ResourceGroup,'-n',$state.diagnosticIdentity,'--location',$Location)
$remediation = Get-AzJson @('identity','create','-g',$ResourceGroup,'-n',$state.remediationIdentity,'--location',$Location)
Grant-Role $diagnostic.principalId 'Cognitive Services OpenAI User' $ai.id
Grant-Role $remediation.principalId 'AcrPull' $acr.id
foreach ($entry in @(@($state.diagnosticIdentity,'agentic-ops-diagnostic'),@($state.remediationIdentity,'agentic-ops-remediation'))) {
    Invoke-Az @('identity','federated-credential','create','--name',$entry[1],'--identity-name',$entry[0],'-g',$ResourceGroup,'--issuer',$aks.oidcIssuerProfile.issuerUrl,'--subject',"system:serviceaccount:agentic-ops:$($entry[1])",'--audiences','api://AzureADTokenExchange','-o','none') | Out-Null
}

Write-Host '[5/7] Build agents and import the healthy demo image'
# Docker is not required locally: the build runs in ACR.
Invoke-Az @('acr','build','--registry',$state.acrName,'--image',"agentic-ops:$($state.imageTag)",$root) | Out-Host
Invoke-Az @('acr','import','--name',$state.acrName,'--source','docker.io/library/nginx:1.27','--image','payments-api:1.4.2','--force','-o','none') | Out-Null
$agentImage = "$($acr.loginServer)/agentic-ops:$($state.imageTag)"
$demoImage = "$($acr.loginServer)/payments-api:1.4.2"
$values = @{
    ACR_LOGIN_SERVER=$acr.loginServer; AGENTIC_OPS_IMAGE=$agentImage; MANAGED_NAMESPACE='payments'
    AZURE_AI_FOUNDRY_ENDPOINT=$ai.properties.endpoint; AZURE_AI_FOUNDRY_DEPLOYMENT=$ModelDeployment
    DIAGNOSTIC_AZURE_CLIENT_ID=$diagnostic.clientId; REMEDIATION_AZURE_CLIENT_ID=$remediation.clientId
}
Write-Host '[6/7] Deploy healthy demo first, then both agents'
foreach ($namespace in 'agentic-ops','payments') {
    $nsFile = Join-Path $work "$namespace.yaml"
    "apiVersion: v1`nkind: Namespace`nmetadata:`n  name: $namespace" | Set-Content $nsFile
    Invoke-Kube @('apply','-f',$nsFile) | Out-Host
}
# Stop existing watchers before resetting the demo on a resumed setup.
$deployments = Invoke-Kube @('get','deployments','-n','agentic-ops','-o','json') | Out-String | ConvertFrom-Json
foreach ($deployment in $deployments.items) {
    if ($deployment.metadata.name -in @('agentic-ops-diagnostic','agentic-ops-remediation')) {
        Invoke-Kube @('scale',"deployment/$($deployment.metadata.name)",'-n','agentic-ops','--replicas=0') | Out-Host
    }
}
$demoPath = Join-Path $work 'payments-healthy.yaml'
Render-Manifest 'deploy/demo-payments-api.yaml' $demoPath $values
$demo = (Get-Content $demoPath -Raw).Replace('/paymnets-api:1.4.2','/payments-api:1.4.2')
Set-Content $demoPath $demo -Encoding utf8
Invoke-Kube @('apply','-f',$demoPath) | Out-Host
Invoke-Kube @('rollout','status','deployment/payments-api','-n','payments','--timeout=300s') | Out-Host
# The token is generated in memory and sent through stdin, never saved in setup state.
$bytes = [byte[]]::new(32)
[Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$secret = @{apiVersion='v1';kind='Secret';metadata=@{name='agentic-ops-remediation';namespace='agentic-ops'};type='Opaque';data=@{token=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([Convert]::ToBase64String($bytes)))}}
$secret | ConvertTo-Json -Depth 5 | & kubectl --kubeconfig $kubeconfig apply -f "-" | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'Could not create handoff secret.' }
$secret=$null; $bytes=$null
$agentsPath = Join-Path $work 'agents-rendered.yaml'
Render-Manifest 'deploy/aks-agentic-ops.yaml' $agentsPath $values
Invoke-Kube @('apply','-f',$agentsPath) | Out-Host
foreach ($role in 'remediation','diagnostic') {
    Invoke-Kube @('rollout','status',"deployment/agentic-ops-$role",'-n','agentic-ops','--timeout=300s') | Out-Host
}

Write-Host '[7/7] Save setup snapshots and configuration for separate test scripts'
$setupDir = Join-Path $work ('setup/'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ'))
New-Item -ItemType Directory -Force $setupDir | Out-Null
Invoke-Kube @('get','deployments','-n','agentic-ops','-o','json') | Set-Content (Join-Path $setupDir 'agents.json')
Invoke-Kube @('get','deployment','payments-api','-n','payments','-o','json') | Set-Content (Join-Path $setupDir 'payments-healthy.json')
Invoke-Kube @('get','pods','-n','payments','-o','json') | Set-Content (Join-Path $setupDir 'payments-pods.json')
$state | Add-Member -NotePropertyName kubeconfig -NotePropertyValue $kubeconfig -Force
$state | Add-Member -NotePropertyName agentImage -NotePropertyValue $agentImage -Force
$state | Add-Member -NotePropertyName demoImage -NotePropertyValue $demoImage -Force
$state | Add-Member -NotePropertyName endpoint -NotePropertyValue $ai.properties.endpoint -Force
$state | ConvertTo-Json | Set-Content $statePath -Encoding utf8
Invoke-Kube @('get','pods','-n','agentic-ops') | Out-Host
Invoke-Kube @('get','pods','-n','payments') | Out-Host
Write-Host "READY FOR TESTS. No fault was injected. Configuration: $statePath"
Write-Host "Setup snapshots: $setupDir (review before publishing). Take a screenshot of the pod tables."
Write-Host "For manual kubectl commands in this shell: `$env:KUBECONFIG = '$kubeconfig'"
Write-Host "After saving test evidence, delete the lab to stop charges: ./scripts/delete-environment-resource-group.ps1 -ResourceGroup '$ResourceGroup'"
Write-Host 'Ready means Kubernetes rollout readiness. Model inference, ACR validation and agent handoff are verified by separate live tests.'
