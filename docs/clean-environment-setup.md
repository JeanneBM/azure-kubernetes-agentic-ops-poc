# Prepare a clean Azure environment

`scripts/setup-environment.ps1` provisions the infrastructure and a **healthy**
PoC. It does not run pytest, inject faults, or execute recovery scenarios. Run scenarios
separately after setup. Kubernetes readiness is not proof of successful live
inference, ACR validation, or agent handoff.

## Run once

Use PowerShell 7, a current Azure CLI, kubectl and git (Azure Cloud Shell in
PowerShell mode is suitable). Sign in and supply your subscription ID. The
identity running setup needs resource creation, provider registration and
role-assignment permissions, for example Owner on a dedicated subscription.
Docker is not required locally.

```powershell
az login
# Clone only when you do not already have the repository:
git clone https://github.com/JeanneBM/azure-kubernetes-agentic-ops-poc.git
Set-Location azure-kubernetes-agentic-ops-poc
./scripts/setup-environment.ps1 -SubscriptionId '<YOUR_SUBSCRIPTION_ID>'
```

For an existing checkout, use `git pull --ff-only` then run the same script.
The default resource group is `rg-agentic-ops-poc-lab`, deliberately separate from
manual PoC resources and the evaluation repository lab. **Do not run this
to create a second lab while your current one is still needed unless you intend
to pay for both.** This script is for reproducing setup from a clean environment.

## Resources and cost choices

- AKS in `polandcentral`: Free control-plane tier, one node, default
  `Standard_D2as_v4`, 32 GiB OS disk, no autoscaler or monitoring add-on.
- Azure CNI Overlay with Cilium, OIDC and Workload Identity.
- ACR Basic, admin credentials disabled, RBAC permissions, AKS image-pull access.
- Azure OpenAI in `swedencentral`, `gpt-4.1-nano` version `2025-04-14`,
  `GlobalStandard`, capacity 1.
- Separate diagnostic and remediation identities with resource-scoped
  `Cognitive Services OpenAI User` and `AcrPull` respectively.
- Both agents, their RBAC, internal remediation Service, ingress NetworkPolicies and handoff Secret.
- Two healthy `payments-api` replicas. This is imported `nginx:1.27` under the
  demo image alias `payments-api:1.4.2`, not a payment-processing application.

These defaults reproduce the small lab used during setup; they are not a claim
of the globally cheapest SKU. VM family/regional quota and SKU location
restrictions are checked before provisioning. Azure can still reject capacity,
model quota or a SKU/node count under current AKS constraints. The script stops
rather than silently choosing a larger or more expensive configuration. It is
an isolated lab configuration, not a production availability recommendation.
Parameters `-NodeVmSize`, `-Location`, `-AiLocation`, `-ModelName`,
`-ModelVersion`, `-ModelDeployment` and `-ModelCapacity` allow explicit changes.

VMs, disks, registry, networking and ACR builds can incur charges. Model
inference is billed when separate tests use it. No spending cap is configured.

## Resume and output

Names are persisted before provisioning in `.local/<resource-group>.json`.
Rerun with the same parameters and keep that file to reuse the lab after a
failure. A resource group without matching local state is refused. An AKS
cluster left in a failed provisioning state needs inspection before retry.
Resuming rebuilds agents, reimports the demo image and resets it to a healthy
baseline; preserve prior test evidence before doing so.

The script uses a separate kubeconfig at `.local/<resource-group>/kubeconfig`;
it does not change your default context. To inspect the lab afterwards:

```powershell
$config = Get-Content ./.local/rg-agentic-ops-poc-lab.json -Raw | ConvertFrom-Json
$env:KUBECONFIG = $config.kubeconfig
kubectl get pods -n agentic-ops
kubectl get pods -n payments
```

Configuration contains resource names, endpoint and image references, without
handoff tokens. `.local` is excluded from git and the ACR build context because
kubeconfig contains credentials. The handoff token is generated in memory and
sent through stdin to Kubernetes. Setup Deployment/Pod snapshots are saved in
`.local/<resource-group>/setup/<UTC-run-id>/`; review identifiers before publishing.
Screenshot the final pod tables as evidence of the healthy initial state.
These snapshots are setup observations, not scenario pass results.

Errors stop setup. Resources already created are retained for inspection and
resume; cleanup is explicit. Readiness probes do not verify Azure data-plane
permissions, and RBAC/federated credentials can need propagation time before
the first separate live test.

## Cleanup

After saving evidence, delete the dedicated lab resource group:

```powershell
./scripts/delete-environment-resource-group.ps1 -ResourceGroup 'rg-agentic-ops-poc-lab'
```

The cleanup script asks you to type the group name. Deleting only workloads
leaves AKS nodes and other resources billable. Never commit kubeconfig or
Secret manifests.

## Azure references

- [Azure CNI with Cilium](https://learn.microsoft.com/azure/aks/azure-cni-powered-by-cilium)
- [AKS CLI and disk constraints](https://learn.microsoft.com/cli/azure/aks)
- [AKS system pool constraints](https://learn.microsoft.com/azure/aks/use-system-pools)
- [Model deployment CLI](https://learn.microsoft.com/cli/azure/cognitiveservices/account/deployment)


### Regional leftovers

Cleanup records the AKS node groups and regional Network Watcher IDs before
removing the lab. It verifies both the PoC group and node groups are gone, then
removes empty watchers only when no VNet remains in their region and no flow
logs, connection monitors or packet captures exist. Shared/nonempty watchers are
reported and preserved. An empty conventional NetworkWatcherRG is removed too.
RAG and other unrelated resources are outside the deletion scope. Azure can
recreate a regional watcher when a new VNet is created; cleanup does not disable
this subscription-wide feature.

Inventory is kept in .local/<resource-group>-cleanup.json so cleanup can resume
after the main group is gone. If a legacy group has already been removed without
inventory, the script refuses to guess which leftovers belonged to it. Failed
inventory/API calls stop cleanup; no complete-cleanup claim is made on failure.
Use -WhatIf to preview without writing inventory or deleting resources. Group
name confirmation explicitly covers eligible unused regional watchers outside
the main group. This does not purge soft-deleted services or subscription-level
registrations, nor delete local evidence.

Reference: [Network Watcher automatic enablement and deletion](https://learn.microsoft.com/azure/network-watcher/network-watcher-create).
