#Requires -Version 7.0
<#
.SYNOPSIS
Delete the PoC group and clean unused regional Network Watcher leftovers.
.DESCRIPTION
Inspect AKS node groups and regional watchers before deletion. Remove only empty,
unused watchers in the lab regions; preserve watchers with other VNets or child
resources. Never delete unrelated groups, local evidence or subscription settings.
#>
[CmdletBinding(SupportsShouldProcess)]
param([Parameter(Mandatory)][ValidatePattern('^[a-zA-Z0-9_-]{1,80}$')][string]$ResourceGroup)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw 'Azure CLI is required.' }
function Az {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
    $output = & az @Arguments --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw "Azure CLI failed: $($Arguments -join ' ')" }
    return $output
}
function Json {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
    return (Az ($Arguments + @('-o','json')) | Out-String | ConvertFrom-Json)
}
$account = Json @('account','show')
$subscription = $account.id
$base = @('--subscription',$subscription)
$local = Join-Path (Split-Path $PSScriptRoot -Parent) '.local'
$inventoryPath = Join-Path $local "$ResourceGroup-cleanup.json"
$exists = (Az (@('group','exists','-n',$ResourceGroup,'-o','tsv')+$base) | Out-String).Trim() -eq 'true'
if ($exists) {
    $group = Json (@('group','show','-n',$ResourceGroup)+$base)
    $clusters = @(Json (@('aks','list','-g',$ResourceGroup)+$base))
    $regions = @(@($group.location) + @($clusters | ForEach-Object location) | Select-Object -Unique)
    $nodes = @($clusters | ForEach-Object nodeResourceGroup | Where-Object { $_ } | Select-Object -Unique)
    $watchers = @(Json (@('resource','list','--resource-type','Microsoft.Network/networkWatchers')+$base) | Where-Object { $_.location -in $regions -and $_.resourceGroup -ne $ResourceGroup })
    $inventory = @{subscriptionId=$subscription;groupId=$group.id;regions=$regions;nodeGroups=$nodes;watcherIds=@($watchers | ForEach-Object id);watcherGroups=@($watchers | ForEach-Object resourceGroup | Select-Object -Unique)}
} elseif (Test-Path $inventoryPath) {
    $inventory = Get-Content $inventoryPath -Raw | ConvertFrom-Json
    if ($inventory.subscriptionId -ne $subscription) { throw 'Cleanup inventory belongs to another subscription.' }
} else {
    throw 'Group is already absent and no cleanup inventory is available. Inspect leftovers explicitly; do not guess their ownership.'
}
Write-Host "Subscription: $($account.name) ($subscription)"
Write-Host "Delete PoC group: $($inventory.groupId)"
Write-Host "Verify AKS node groups: $($inventory.nodeGroups -join ', ')"
Write-Host "Check regional watchers: $($inventory.watcherIds -join ', ')"
Write-Host 'Save test evidence first. Regional watchers are shared: those serving other VNets or containing diagnostics will be preserved.'
if (-not $PSCmdlet.ShouldProcess($inventory.groupId,'Delete PoC group; verify node groups; remove empty unused regional watchers')) { return }
$confirmation = Read-Host "Type '$ResourceGroup' to delete this lab and eligible unused watchers"
if ($confirmation -cne $ResourceGroup) { throw 'Confirmation did not match. No resources were changed.' }
New-Item -ItemType Directory -Force $local | Out-Null
$inventory | ConvertTo-Json -Depth 10 | Set-Content $inventoryPath -Encoding utf8
if ($exists) { Az (@('group','delete','-n',$ResourceGroup,'--yes')+$base) | Out-Host }
if ((Az (@('group','exists','-n',$ResourceGroup,'-o','tsv')+$base) | Out-String).Trim() -ne 'false') { throw 'PoC group still exists.' }
foreach ($nodeGroup in $inventory.nodeGroups) {
    if ((Az (@('group','exists','-n',$nodeGroup,'-o','tsv')+$base) | Out-String).Trim() -ne 'false') {
        throw "AKS node group remains: $nodeGroup. Inspect it before deletion; cleanup is not complete."
    }
}
$preserved = @()
foreach ($watcherId in $inventory.watcherIds) {
    $resources = @(Json (@('resource','list','--resource-type','Microsoft.Network/networkWatchers')+$base))
    $watcher = $resources | Where-Object id -EQ $watcherId | Select-Object -First 1
    if ($null -eq $watcher) { continue }
    $vnets = @(Json (@('network','vnet','list')+$base) | Where-Object location -EQ $watcher.location)
    if ($vnets.Count -gt 0) {
        $preserved += $watcherId
        Write-Host "PRESERVED shared watcher: $watcherId (other VNets remain in $($watcher.location))."
        continue
    }
    # Children are not reliably included in az resource list. Query each collection.
    $childCount = 0
    foreach ($collection in 'flowLogs','connectionMonitors','packetCaptures') {
        $response = Json @('rest','--method','get','--url',"https://management.azure.com$watcherId/$collection`?api-version=2024-05-01")
        if ($response.nextLink) { throw "Paged child inventory for $watcherId; refusing deletion without complete inventory." }
        $childCount += @($response.value).Count
    }
    if ($childCount -gt 0) {
        $preserved += $watcherId
        Write-Host "PRESERVED watcher with diagnostic child resources: $watcherId"
        continue
    }
    Az (@('resource','delete','--ids',$watcherId)+$base) | Out-Host
    $remaining = @(Json (@('resource','list','--resource-type','Microsoft.Network/networkWatchers')+$base) | Where-Object id -EQ $watcherId)
    if ($remaining.Count -gt 0) { throw "Watcher remains: $watcherId" }
    Write-Host "Removed unused watcher: $watcherId"
}
# Remove only the conventional watcher group, and only if it has no resources.
foreach ($watcherGroup in $inventory.watcherGroups) {
    if ($watcherGroup -ine 'NetworkWatcherRG') { continue }
    if ((Az (@('group','exists','-n',$watcherGroup,'-o','tsv')+$base) | Out-String).Trim() -ne 'true') { continue }
    $contents = @(Json (@('resource','list','-g',$watcherGroup)+$base))
    if ($contents.Count -eq 0) {
        Az (@('group','delete','-n',$watcherGroup,'--yes')+$base) | Out-Host
        if ((Az (@('group','exists','-n',$watcherGroup,'-o','tsv')+$base) | Out-String).Trim() -ne 'false') { throw 'Empty NetworkWatcherRG was not removed.' }
        Write-Host 'Removed empty NetworkWatcherRG.'
    }
}
Write-Host 'PoC group removed; AKS node groups verified absent; eligible unused regional watchers cleaned.'
if ($preserved.Count -gt 0) { Write-Host "Shared/nonempty watchers preserved: $($preserved -join ', ')" }
Write-Host 'Unrelated resources (including RAG), local files and evidence were preserved. Subscription-wide automatic Network Watcher creation was not disabled.'
