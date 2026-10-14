[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory = $true)]
    [string]$ResourceGroup,

    [string]$AksName,

    [string]$ManagedNamespace,

    [switch]$DeleteResourceGroup
)

$ErrorActionPreference = "Stop"

function Assert-Command {
    param([Parameter(Mandatory = $true)][string]$Name)

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found in PATH."
    }
}

Assert-Command "az"

if (-not $DeleteResourceGroup -and [string]::IsNullOrWhiteSpace($AksName)) {
    throw "Specify -AksName to remove only the AKS workload, or use -DeleteResourceGroup to remove all Azure resources in the resource group."
}

$group = az group show --name $ResourceGroup --output json 2>$null | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $null -eq $group) {
    throw "Azure resource group '$ResourceGroup' was not found or is not accessible."
}

if ($DeleteResourceGroup) {
    # Delegate to the verified cleanup path, including node groups and watchers.
    & (Join-Path $PSScriptRoot 'delete-environment-resource-group.ps1') -ResourceGroup $ResourceGroup -WhatIf:$WhatIfPreference
    return
}

Write-Warning "Workload-only removal does not stop Azure infrastructure charges. Use -DeleteResourceGroup for full cleanup."
Assert-Command "kubectl"

if ($PSCmdlet.ShouldProcess("agentic-ops namespace", "Remove the AKS workload")) {
    $credentials = az aks get-credentials `
        --resource-group $ResourceGroup `
        --name $AksName `
        --overwrite-existing `
        --output none
    if ($LASTEXITCODE -ne 0) {
        throw "Could not obtain credentials for AKS cluster '$AksName'."
    }

    if (-not [string]::IsNullOrWhiteSpace($ManagedNamespace)) {
        kubectl delete role,rolebinding agentic-ops -n $ManagedNamespace --ignore-not-found
    }

    kubectl delete namespace agentic-ops --ignore-not-found
    if ($LASTEXITCODE -ne 0) {
        throw "Could not remove the agentic-ops namespace."
    }
    Write-Host "AKS workload removed. The AKS cluster and its nodes are still running."
}
