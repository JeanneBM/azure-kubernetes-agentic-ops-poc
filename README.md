# Azure Kubernetes Agentic Ops PoC

[![Tests](https://github.com/JeanneBM/azure-kubernetes-agentic-ops-poc/actions/workflows/test.yml/badge.svg)](https://github.com/JeanneBM/azure-kubernetes-agentic-ops-poc/actions/workflows/test.yml)

**Code-defined agent orchestration on Azure Kubernetes Service (AKS), with LLM-assisted diagnosis and deterministic remediation.** The PoC coordinates a diagnostic agent and a remediation agent, deployed as two independently authenticated workloads with separate responsibilities and permissions.

**Agent deployment follows Infrastructure as Code (IaC):** version-controlled Kubernetes manifests define the workloads, configuration, RBAC, and network policies, applied with `kubectl apply` to an existing AKS cluster.

**The agents and their orchestration are implemented in Python and run on AKS.** Azure AI Foundry / Azure OpenAI supplies model inference to the diagnostic agent. The watcher, agent handoff, incident lifecycle, safety policy, and Kubernetes execution are controlled by application code.

The PoC demonstrates one narrowly scoped recovery scenario: correcting an image-reference typo in a Kubernetes Deployment whose images are stored in Azure Container Registry (ACR), such as `paymnets-api:1.4.2` instead of `payments-api:1.4.2`. Cases outside this policy are escalated with evidence and a reason for human review.

[Watch the demo recording](./agentic_ops_demo_en_v6_final.mp4) · [Project solution PDF](./Azure_Kubernetes_Agentic_Ops_Project_Solution_public.pdf)

## Proof of concept objective: demonstrate agent response speed

The goal of this proof of concept (PoC) is to demonstrate how an agent can shorten the time between detecting a Kubernetes failure and completing a verified remediation. The watcher initiates the diagnostic and remediation workflow without waiting for a human to notice the incident, collect evidence, and perform the permitted correction manually.

The deliberately narrow image-typo scenario makes this response time measurable while keeping the action scope controlled. Measure the interval from the first observed image-pull failure to a verified healthy rollout, with separate timings for detection, diagnosis, policy validation, execution, and rollout verification.

A comparison with a manual response should use the same failure scenario, available evidence, and completion criterion. Faster response is an objective to validate through measurements, not a benchmark result established by this README. The current PoC does not establish that an LLM-based agent is faster than a dedicated deterministic repair script.

## Architecture and responsibility boundaries

| Component running on AKS | Responsibility | Kubernetes permissions | Azure capability |
| --- | --- | --- | --- |
| Diagnostic agent | Observe failures, collect evidence, request a model proposal, and send a typed request to remediation. | Read Pods, Events, ReplicaSets, and Deployments in the managed namespace. | Foundry / Azure OpenAI inference. |
| Remediation agent | Independently authorize the proposed change, validate the image in ACR, execute the change, and verify rollout health. | Read and patch Deployments in the managed namespace. | ACR tag validation. |

The diagnostic agent uses an LLM to analyse evidence and propose a correction. The remediation agent is implemented as a deterministic policy and execution service: it authorizes the proposal, executes the permitted action, and verifies the outcome without an LLM or web-search capability. Each agent workload has its own Kubernetes ServiceAccount and Azure Workload Identity.

The internal handoff is authenticated with a shared token and restricted by ingress NetworkPolicy. The remediation workload still requires network access to Kubernetes, ACR, DNS, and Azure identity services. The portable manifest does not enforce a complete external egress allowlist; see [deployment boundaries](docs/two-workload-deployment.md).

### How orchestration works

`IncidentOrchestrator` coordinates the diagnostic-to-remediation flow, incident state, deduplication, and final outcome. The agents exchange a typed facts contract through an authenticated internal API. The orchestrator marks an incident resolved only when remediation confirms a healthy rollout; otherwise, it records escalation for human review.

This is a bounded, code-defined agent workflow with fixed roles and a narrow action policy. It does not implement open-ended planning, dynamic agent selection, or a general-purpose multi-agent framework.

The key design is **the diagnostic agent proposes; the remediation agent authorizes, acts, and verifies**. The image typo is the demonstration scenario for this responsibility boundary.

## Incident flow

1. `PodWatcher` observes `ImagePullBackOff` and `ErrImagePull` in one managed namespace.
2. `AksDiagnosticProvider` collects the Pod state, Kubernetes Events, ownership information, and failing image reference.
3. `FoundryDiagnosticProvider` sends this evidence to the model and parses its structured JSON response. The current PoC uses Kubernetes evidence; it does not implement internet search.
4. The diagnostic workload sends typed facts and the proposal to the remediation workload over the authenticated internal API.
5. `SelfCurePolicy` treats the proposal as untrusted input and checks it against the fixed remediation scope and ACR.
6. `AksActionExecutor` patches the affected container image and verifies rollout health.
7. `IncidentOrchestrator` records the outcome, deduplicates by namespace and workload, and emits an audit event.

An incident is marked `resolved` only after a healthy rollout. A rejected proposal or a model, ACR, Kubernetes, execution, or rollout error results in escalation. Escalation exposes the reason and evidence for human review; it is not an implemented approval-and-resume workflow.

## Automatic remediation policy

The only automatic action is `fix_image`. It is permitted only when:

- exactly one container has an image-pull failure;
- the model supplies exactly one parameter: the proposed image reference;
- the current registry is allowlisted and the proposed registry is unchanged;
- repository and tag each differ by no more than two edits under the configured typo-distance rule;
- the current image does not exist in ACR;
- the proposed image exists in ACR;
- the Deployment still has the image observed during diagnosis when the executor applies the change.

The model cannot choose a namespace, workload, Deployment, or container. Targets are derived from the configured scope, trigger, and Kubernetes evidence.

ACR existence and name similarity constrain the operation; they do not prove that an image is trusted or semantically correct. The model's groundedness score is self-reported, not an independently measured safety guarantee.

## Development and verification

Python 3.11 or later is required for development. The container and CI use Python 3.12.

~~~sh
python -m pip install -c constraints.txt -e ".[dev]"
python -m pytest
~~~

GitHub Actions runs the test suite on pushes and pull requests. The suite covers policy, orchestration, Kubernetes and ACR adapters, model-response handling, watcher behaviour, and the two-component handoff.

**Test boundary:** automated tests use fake Kubernetes, ACR, and Foundry endpoints. They do not establish that Azure identity, cluster networking, or remediation works in a live AKS environment. Validate these separately in a non-production cluster.

### Coverage snapshot

Local measurement on Python 3.12 on **2026-10-02**: **63 tests passed**. Coverage was measured across all modules in `src/agentic_ops`, including modules not imported by the tests.

| Metric | Result |
| --- | --- |
| Statement (line) coverage | **85.4%** (487 of 570 executable statements) |
| Branch coverage | **72.3%** (94 of 130 branches) |
| Combined statement and branch coverage | **83.0%** |

Module results below use the combined statement and branch metric:

| Module | Coverage |
| --- | --- |
| `__init__.py` | 100% |
| `acr.py` | 90% |
| `agents.py` | 100% |
| `aks.py` | 90% |
| `contracts.py` | 97% |
| `foundry.py` | 100% |
| `orchestrator.py` | 96% |
| `remote.py` | 49% |
| `safety.py` | 95% |
| `split_app.py` | 30% |
| `watcher.py` | 53% |
| `web.py` | 100% |

The main remaining gap is the HTTP handoff between agents. Workload construction and parts of the watcher lifecycle also require further tests.

This is a dated local snapshot, not a live CI coverage result. The existing CI workflow runs pytest without collecting coverage. The local measurement used separately installed dependencies rather than the exact `constraints.txt` environment used by CI.

To repeat the measurement after installing the development dependencies:

~~~sh
python -m pip install coverage
python -m coverage run --branch --source=src/agentic_ops -m pytest
python -m coverage report -m
~~~

Coverage measures which code the tests execute; it does not establish live AKS integration correctness or test assertion quality.

## Automatic environment setup

Run the following one-line command from the repository root in PowerShell 7
(after signing in with `az login`):

~~~powershell
./scripts/setup-environment.ps1 -SubscriptionId '<YOUR_SUBSCRIPTION_ID>'
~~~

The script provisions a dedicated `rg-agentic-ops-poc-lab` resource group,
one-node AKS with Cilium and Workload Identity, Basic ACR, Azure OpenAI model,
and separate identities; builds and deploys both agents and a healthy demo.
It does not inject faults or run tests. No local Docker installation is required.
See [the setup guide](docs/clean-environment-setup.md) for requirements, defaults,
resuming setup, local snapshots and cleanup. Existing unrelated resources are
not adopted. The manual deployment instructions below remain available.

## Deployment prerequisites

- AKS with OIDC issuer and Workload Identity enabled.
- A NetworkPolicy-capable CNI.
- ACR reachable from AKS.
- An Azure AI Foundry / Azure OpenAI compatible endpoint with a deployed model.
- Azure CLI, kubectl, and PowerShell for the commands below.
- An existing AKS cluster, ACR, and model resource; the instructions below configure and deploy the application rather than provision the entire environment.

## AKS deployment

The commands deploy two independent workloads with separate identities. For network boundary details, see [the two-workload deployment guide](docs/two-workload-deployment.md).

### Configure variables

PowerShell:

~~~
$resourceGroup = "rg-agentic-ops"
$aksName = "aks-agentic-ops"
$acrName = "<YOUR_ACR_NAME>"
$managedNamespace = "payments"
$identityResourceGroup = $resourceGroup
$diagnosticIdentityName = "id-agentic-ops-diagnostic"
$remediationIdentityName = "id-agentic-ops-remediation"

$env:ACR_LOGIN_SERVER = "$acrName.azurecr.io"
$env:MANAGED_NAMESPACE = $managedNamespace
$env:AZURE_AI_FOUNDRY_ENDPOINT = "https://<FOUNDRY_RESOURCE>.openai.azure.com"
$env:AZURE_AI_FOUNDRY_DEPLOYMENT = "<FOUNDRY_MODEL_DEPLOYMENT_NAME>"
$env:AGENTIC_OPS_IMAGE = "$env:ACR_LOGIN_SERVER/agentic-ops:0.2.0"

az login
az account set --subscription "<SUBSCRIPTION_ID_OR_NAME>"
~~~

### Configure two Workload Identities

Create both user-assigned identities and obtain the AKS OIDC issuer:

~~~
az identity create --name $diagnosticIdentityName --resource-group $identityResourceGroup
az identity create --name $remediationIdentityName --resource-group $identityResourceGroup

$diagnosticClientId = az identity show --name $diagnosticIdentityName --resource-group $identityResourceGroup --query clientId -o tsv
$diagnosticPrincipalId = az identity show --name $diagnosticIdentityName --resource-group $identityResourceGroup --query principalId -o tsv
$remediationClientId = az identity show --name $remediationIdentityName --resource-group $identityResourceGroup --query clientId -o tsv
$remediationPrincipalId = az identity show --name $remediationIdentityName --resource-group $identityResourceGroup --query principalId -o tsv
$issuer = az aks show --name $aksName --resource-group $resourceGroup --query oidcIssuerProfile.issuerUrl -o tsv
~~~

Grant only the required roles:

~~~
$foundryResourceId = "<FOUNDRY_RESOURCE_ID>"
$acrResourceId = az acr show --name $acrName --resource-group $resourceGroup --query id -o tsv

az role assignment create --assignee-object-id $diagnosticPrincipalId --assignee-principal-type ServicePrincipal --role "Cognitive Services OpenAI User" --scope $foundryResourceId
az role assignment create --assignee-object-id $remediationPrincipalId --assignee-principal-type ServicePrincipal --role AcrPull --scope $acrResourceId
az aks update --name $aksName --resource-group $resourceGroup --attach-acr $acrName
~~~

Create one federated credential for each ServiceAccount:

~~~
az identity federated-credential create --name agentic-ops-diagnostic --identity-name $diagnosticIdentityName --resource-group $identityResourceGroup --issuer $issuer --subject "system:serviceaccount:agentic-ops:agentic-ops-diagnostic" --audiences "api://AzureADTokenExchange"
az identity federated-credential create --name agentic-ops-remediation --identity-name $remediationIdentityName --resource-group $identityResourceGroup --issuer $issuer --subject "system:serviceaccount:agentic-ops:agentic-ops-remediation" --audiences "api://AzureADTokenExchange"
~~~

### Build and deploy

Build the image:

~~~
az acr build --registry $acrName --image agentic-ops:0.2.0 .
~~~

Create namespaces and the token used by the authenticated diagnostic-to-remediation handoff:

~~~
az aks get-credentials --resource-group $resourceGroup --name $aksName --overwrite-existing
kubectl create namespace agentic-ops --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace $env:MANAGED_NAMESPACE --dry-run=client -o yaml | kubectl apply -f -
$handoffBytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($handoffBytes)
$handoffToken = [Convert]::ToBase64String($handoffBytes)
kubectl create secret generic agentic-ops-remediation -n agentic-ops --from-literal=token=$handoffToken
~~~

Render all seven placeholders in `deploy/aks-agentic-ops.yaml`: `DIAGNOSTIC_AZURE_CLIENT_ID`, `REMEDIATION_AZURE_CLIENT_ID`, `AZURE_AI_FOUNDRY_ENDPOINT`, `AZURE_AI_FOUNDRY_DEPLOYMENT`, `AGENTIC_OPS_IMAGE`, `MANAGED_NAMESPACE`, and `ACR_LOGIN_SERVER`.

~~~
$rendered = Get-Content deploy/aks-agentic-ops.yaml -Raw
$values = @{
  DIAGNOSTIC_AZURE_CLIENT_ID = $diagnosticClientId
  REMEDIATION_AZURE_CLIENT_ID = $remediationClientId
  AZURE_AI_FOUNDRY_ENDPOINT = $env:AZURE_AI_FOUNDRY_ENDPOINT
  AZURE_AI_FOUNDRY_DEPLOYMENT = $env:AZURE_AI_FOUNDRY_DEPLOYMENT
  AGENTIC_OPS_IMAGE = $env:AGENTIC_OPS_IMAGE
  MANAGED_NAMESPACE = $env:MANAGED_NAMESPACE
  ACR_LOGIN_SERVER = $env:ACR_LOGIN_SERVER
}
foreach ($name in $values.Keys) { $rendered = $rendered.Replace(('${' + $name + '}'), $values[$name]) }
Set-Content deploy/aks-agentic-ops.rendered.yaml $rendered

kubectl apply -f deploy/aks-agentic-ops.rendered.yaml
kubectl rollout status deployment/agentic-ops-diagnostic -n agentic-ops
kubectl rollout status deployment/agentic-ops-remediation -n agentic-ops
~~~

## Demo

Import the correct image into ACR:

~~~
az acr import --name $acrName --source docker.io/library/nginx:1.27 --image payments-api:1.4.2
~~~

Render and apply the demo manifest. It intentionally references `paymnets-api`:

~~~powershell
$demo = Get-Content deploy/demo-payments-api.yaml -Raw
$demo = $demo.Replace('${MANAGED_NAMESPACE}', $env:MANAGED_NAMESPACE)
$demo = $demo.Replace('${ACR_LOGIN_SERVER}', $env:ACR_LOGIN_SERVER)
Set-Content deploy/demo-payments-api.rendered.yaml $demo
kubectl apply -f deploy/demo-payments-api.rendered.yaml
~~~

Then watch recovery:

~~~
kubectl get pods -n $env:MANAGED_NAMESPACE -w
kubectl logs -n agentic-ops deploy/agentic-ops-diagnostic
kubectl logs -n agentic-ops deploy/agentic-ops-remediation
kubectl get deployment payments-api -n $env:MANAGED_NAMESPACE -o jsonpath='{.spec.template.spec.containers[0].image}'
~~~

## PoC limitations

- Only image-reference typo remediation is automatic.
- The supplied deployment manages one namespace and one ACR; supported targets are Deployments with exactly one failing container per incident.
- Incident state and deduplication are in memory. Each workload intentionally runs one replica; durable state and coordination across replicas are not implemented.
- RBAC allows Deployment patching within the managed namespace. The image-only restriction is enforced by application code, not field-level Kubernetes RBAC.
- A healthy rollout confirms workload readiness, not full business functionality.
- External egress restrictions require cluster-specific configuration.
- Automated tests use fake service endpoints; live AKS validation is a separate step.

## Cleanup

For the automatically provisioned lab, save evidence and run:

~~~powershell
./scripts/delete-environment-resource-group.ps1 -ResourceGroup 'rg-agentic-ops-poc-lab'
~~~

This waits for group deletion, verifies AKS node groups, and removes eligible
unused regional Network Watchers and an empty NetworkWatcherRG. Shared watchers
are preserved. See [cleanup details](docs/clean-environment-setup.md#cleanup).

The existing commands below are available for manually provisioned environments.


To remove only the application from the existing cluster:

~~~powershell
.\scripts\stop-agentic-ops.ps1 -ResourceGroup "rg-agentic-ops" -AksName "aks-agentic-ops"
~~~

To delete the complete resource group and all resources in it:

~~~powershell
.\scripts\stop-agentic-ops.ps1 -ResourceGroup "rg-agentic-ops" -DeleteResourceGroup
~~~

Resource-group deletion is destructive. Review the script's confirmation and `-WhatIf` options before using it.

