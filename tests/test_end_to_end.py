"""Real orchestrator, policy, ACR client, Foundry client, AKS adapters and watcher,
using explicit agent collaborators against an in-memory cluster and mocked HTTP services."""
import json
from types import SimpleNamespace as NS

import httpx
from kubernetes import client

from agentic_ops import IncidentOrchestrator, SelfCurePolicy
from agentic_ops.agents import SafeRemediationAgent
from agentic_ops.acr import AcrRegistry
from agentic_ops.aks import AksActionExecutor, AksDiagnosticProvider
from agentic_ops.foundry import FoundryDiagnosticProvider
from agentic_ops.watcher import PodWatcher
from conftest import NEW, OLD
from test_aks import deployment, pod


class FakeCluster:
    """Minimal Core/Apps API: patching the image 'rolls out' a healthy deployment."""

    def __init__(self):
        self.deployment = deployment(OLD, generation=1, observed=1, available=0)
        self.patches = []

    def read_namespaced_pod(self, name, namespace):
        return pod()

    def list_namespaced_event(self, namespace, field_selector=None):
        return NS(items=[NS(reason="Failed", message=f"manifest for {OLD} not found")])

    def read_namespaced_replica_set(self, name, namespace):
        return client.V1ReplicaSet(metadata=client.V1ObjectMeta(owner_references=[
            client.V1OwnerReference(api_version="apps/v1", kind="Deployment", name="test-app", uid="d")]))

    def read_namespaced_deployment(self, name, namespace):
        return self.deployment

    def patch_namespaced_deployment(self, name, namespace, body):
        self.patches.append((name, namespace, body))
        image = body["spec"]["template"]["spec"]["containers"][0]["image"]
        self.deployment = deployment(image, generation=2, observed=2)  # rollout completes


def wire(model_image, registry_has=(NEW,)):
    cluster = FakeCluster()

    def foundry(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][1]["content"]
        assert OLD in prompt  # the model saw the cluster evidence
        reply = {"groundedness": 0.95, "summary": "Image name has a typo.",
                 "safe_action": {"name": "fix_image", "parameters": {"image": model_image}}}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}}]})

    def acr(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/exchange":
            return httpx.Response(200, json={"refresh_token": "rt"})
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "at"})
        ref = request.url.path.removeprefix("/v2/").replace("/manifests/", ":")
        return httpx.Response(200 if f"acrprod.azurecr.io/{ref}" in registry_has else 404)

    class Cred:
        def get_token(self, scope):
            return NS(token="aad")

    diagnostics = FoundryDiagnosticProvider(
        AksDiagnosticProvider(cluster), endpoint="https://f.openai.azure.com", deployment="gpt",
        http_client=httpx.Client(transport=httpx.MockTransport(foundry)), token_provider=lambda: "t",
    )
    policy = SelfCurePolicy(
        AcrRegistry(credential=Cred(), http_client=httpx.Client(transport=httpx.MockTransport(acr))),
        namespace="test-app", allowed_registries=["acrprod.azurecr.io"],
    )
    executor = AksActionExecutor(cluster, verify_timeout=5, poll_interval=0, sleep=lambda s: None)
    results = []
    orchestrator = IncidentOrchestrator(diagnostics, remediation_agent=SafeRemediationAgent(policy, executor))
    watcher = PodWatcher(cluster, cluster, "test-app", lambda t: results.append(orchestrator.handle(t)))
    return cluster, watcher, results


def test_typo_in_image_name_is_fixed_automatically():
    cluster, watcher, results = wire(NEW)
    assert watcher.process_pod(pod())
    result = results[0]
    assert result.incident.status.value == "resolved" and result.action_executed
    assert cluster.deployment.spec.template.spec.containers[0].image == NEW
    assert len(cluster.patches) == 1


def test_hallucinated_image_is_not_applied():
    cluster, watcher, results = wire("acrprod.azurecr.io/test-app:1.4.3")  # plausible, but this tag does not exist
    watcher.process_pod(pod())
    assert results[0].incident.status.value == "escalated"
    assert "not found" in results[0].incident.reason
    assert cluster.patches == []


def test_image_that_exists_but_fails_to_pull_is_not_touched():
    cluster, watcher, results = wire(NEW, registry_has=(OLD, NEW))  # e.g. missing AcrPull on kubelet
    watcher.process_pod(pod())
    assert results[0].incident.status.value == "escalated"
    assert cluster.patches == []

