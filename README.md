# Azure Kubernetes Agentic Ops — PoC

> **Test workload:** `test-app` is a disposable nginx placeholder used only for fault injection and recovery checks. It has no business logic, payment processing, business data or extra backend. `tset-app` is the deliberately misspelled image alias used in the recovery scenario.

[![Tests](https://github.com/JeanneBM/azure-kubernetes-agentic-ops-poc/actions/workflows/test.yml/badge.svg)](https://github.com/JeanneBM/azure-kubernetes-agentic-ops-poc/actions/workflows/test.yml)

[![Security](https://github.com/JeanneBM/azure-kubernetes-agentic-ops-poc/actions/workflows/security.yml/badge.svg)](https://github.com/JeanneBM/azure-kubernetes-agentic-ops-poc/actions/workflows/security.yml)

A two-agent AKS proof of concept combining LLM-assisted diagnosis with
policy-controlled remediation of container image-reference errors.

**The model proposes; deterministic policy authorizes; execution is verified.**

[Demo recording](agentic_ops_demo_en_v6_final.mp4) ·
[Project solution PDF](Azure_Kubernetes_Agentic_Ops_Project_Solution_public.pdf) ·
[Project concept](project-concept.md)

## Individual lab reports

Use `python scripts/validate_lab.py smoke` for this repository's existing lab.
For setup, smoke, verified deletion, recreation and another smoke/delete,
use the explicit `cycle` command in the [lab validation guide](docs/lab-validation.md).
Each run writes its own JSON/Markdown report under `.local/reports/`.
Verdicts and exit codes: PASS (0), FAIL (1), INCONCLUSIVE (2).
No combined report across repositories is produced.

## How it works

| Agent | Responsibility |
| --- | --- |
| Diagnostic | Observe image-pull failures, collect Kubernetes evidence and request a structured model proposal. |
| Remediation | Check the proposal against policy and ACR, update the affected container image and verify rollout health. |

Python code coordinates the agents on AKS; Azure OpenAI provides inference.
Version-controlled Kubernetes manifests define workloads, configuration, RBAC
and network policies. Each agent has its own ServiceAccount and Azure Workload
Identity. The internal handoff is token-authenticated and restricted by
ingress NetworkPolicy.

Automatic repair requires exactly one failing container, an absent current
image and an existing nearby correction in the same allowlisted registry.
Targets come from Kubernetes evidence, not the model. Failed checks or
unsuccessful recovery result in escalation for human review.

The PoC explores faster verified incident recovery. A demo does not establish
a speed advantage over a deterministic repair script. Extended scenarios and
live evidence are maintained in the
[evaluation repository](https://github.com/JeanneBM/azure-kubernetes-agentic-ops-eval).

## Start the environment

Use **PowerShell 7**, Azure CLI, kubectl and git. Azure Cloud Shell in PowerShell
mode is suitable. The account needs resource creation, provider registration
and role-assignment permissions. Local Docker is not required.

### What the setup script does

- Creates a dedicated `rg-agentic-ops-poc-lab` resource group, one-node AKS with
  Cilium and Workload Identity, Basic ACR and an Azure OpenAI model deployment.
- Configures separate agent identities and resource-scoped permissions, builds
  the agent image in ACR and deploys both agents.
- Imports nginx under the demo alias `test-app:1.4.2` and starts a healthy
  `test-app` Deployment.
- Saves configuration, a separate kubeconfig and setup snapshots under `.local/`.

**Setup creates billable Azure resources.** It does not inject failures or run
tests. Existing groups without matching local setup state are refused.

Run each command separately:

~~~powershell
az login
az account list --query "[].{Name:name,SubscriptionId:id}" -o table
git clone https://github.com/JeanneBM/azure-kubernetes-agentic-ops-poc.git
Set-Location azure-kubernetes-agentic-ops-poc
./scripts/setup-environment.ps1 -SubscriptionId '<YOUR_SUBSCRIPTION_ID>'
~~~

Replace the placeholder with your subscription ID. For an existing checkout,
run `git pull --ff-only` from its directory, then the setup command.
Completion prints `READY FOR TESTS`.

Load the saved configuration and inspect the lab:

~~~powershell
$config = Get-Content ./.local/rg-agentic-ops-poc-lab.json -Raw | ConvertFrom-Json
$env:KUBECONFIG = $config.kubeconfig
kubectl get pods -n agentic-ops
kubectl get pods -n test-app
~~~

To resume interrupted setup, retain local state and rerun with the same
parameters. Resuming restores the healthy demo baseline; save evidence first.
For defaults and overrides, read the [setup script](scripts/setup-environment.ps1)
or [setup guide](docs/clean-environment-setup.md).

## Run the demo

After setup and loading the configuration above, introduce the image typo:

~~~powershell
$registry = ($config.demoImage -split '/')[0]
kubectl set image deployment/test-app "api=$registry/tset-app:1.4.2" -n test-app
kubectl get pods -n test-app -w
~~~

Press Ctrl+C to stop watching, then inspect the agent decision and final state:

~~~powershell
kubectl logs -n agentic-ops deployment/agentic-ops-diagnostic
kubectl logs -n agentic-ops deployment/agentic-ops-remediation
kubectl get deployment test-app -n test-app -o jsonpath='{.spec.template.spec.containers[0].image}'
kubectl get pods -n test-app
~~~

Expected recovery: the agent restores `test-app:1.4.2`, verifies a healthy
rollout and records `resolved`. Otherwise, inspect the escalation reason.
This is a demo, not an automated evaluation verdict. Repeated trials can be
affected by in-memory incident deduplication.

## Security checks

The [Security workflow](.github/workflows/security.yml) runs Bandit SAST, pip-audit
for resolved Python dependencies, and Trivy scans for secrets, infrastructure
configuration and HIGH/CRITICAL container vulnerabilities, including those without
a fix. It also generates a CycloneDX SBOM and retains reports for 14 days.
The security gate requires both scanner jobs to pass. Checks run on main pushes,
pull requests, weekly and on manual dispatch; they do not deploy to Azure.
A passing badge describes these automated checks, not a complete security audit.

## Local checks

Python 3.11 or later is required; the container and CI use Python 3.12.

~~~sh
python -m pip install -c constraints.txt -e ".[dev]"
python -m pytest
~~~

Tests use mocked Kubernetes, ACR and model responses. They check application
behaviour; live identity, networking and inference require Azure validation.

## Cleanup

After deletion, setup archives retired state and generates fresh resource names.
`stop-agentic-ops.ps1` without `-DeleteResourceGroup` only removes workloads;
it does not stop infrastructure charges. With that switch it delegates to the
verified cleanup script below.

Cleanup refuses an active subscription that differs from the saved lab state.
Keep `.local/` through deletion and retries.

Save evidence, then delete the dedicated lab:

~~~powershell
az account set --subscription $config.subscriptionId
./scripts/delete-environment-resource-group.ps1 -ResourceGroup 'rg-agentic-ops-poc-lab'
~~~

The script requires the group name as confirmation, waits for deletion, verifies
AKS node groups and removes eligible unused regional Network Watchers plus an
empty NetworkWatcherRG. Shared watchers and local files are preserved.
If setup used a different group, supply that name.
See the [cleanup script](scripts/delete-environment-resource-group.ps1).

## Incident handling

- Read-only diagnosis retries transient network errors and HTTP 408, 429, 500,
  502, 503 and 504 up to three attempts, with 1 s and 2 s backoff.
- Policy rejection details survive the internal HTTP handoff.
- The handoff read timeout is 300 s, allowing the default 120 s rollout check
  plus registry validation. This is not an overall infrastructure deadline.
- Remediation is not automatically retried after an uncertain write outcome.
  Exhausted diagnosis failures still escalate and use the incident cooldown.

## Scope and limitations

- One managed namespace and registry; automatic repair only for image-reference typos.
- Incident state and deduplication are in memory; each agent runs one replica.
- Image-only writes are constrained by application policy, not field-level RBAC.
- Image existence and similarity do not establish service or release intent.
- Rollout readiness does not verify business functionality.
- External egress allowlisting requires cluster-specific configuration.

For details, see [two-agent orchestration](docs/two-agent-orchestration.md)
and [deployment boundaries](docs/two-workload-deployment.md).

Historical evidence (logs, archives, screenshots and original reports) retains the resource names recorded at capture time. Current scripts and manifests use the neutral test-app naming. Recreate a disposable lab before using the renamed scripts against an older setup.
