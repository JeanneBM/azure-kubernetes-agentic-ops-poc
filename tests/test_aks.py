from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from kubernetes import client

from agentic_ops import ActionRequest
from agentic_ops.aks import AksActionExecutor, AksDiagnosticProvider, find_pull_failures, workload_of
from conftest import NEW, OLD, make_trigger


def pod(reason="ImagePullBackOff", image=OLD, owner=True):
    owners = [client.V1OwnerReference(api_version="apps/v1", kind="ReplicaSet", name="test-app-7d9", uid="u")] if owner else None
    return client.V1Pod(
        metadata=client.V1ObjectMeta(name="test-app-7d9-x", namespace="test-app", owner_references=owners),
        spec=client.V1PodSpec(containers=[client.V1Container(name="api", image=image)]),
        status=client.V1PodStatus(phase="Pending", container_statuses=[client.V1ContainerStatus(
            name="api", image=image, image_id="", ready=False, restart_count=0,
            state=client.V1ContainerState(waiting=client.V1ContainerStateWaiting(reason=reason, message="not found")),
        )]),
    )


def test_pull_failure_detected_from_cluster_state():
    failures = find_pull_failures(pod())
    assert [(f.container, f.image) for f in failures] == [("api", OLD)]
    assert find_pull_failures(pod(reason="CrashLoopBackOff")) == ()


def test_workload_resolved_through_replicaset():
    apps = MagicMock()
    apps.read_namespaced_replica_set.return_value = client.V1ReplicaSet(
        metadata=client.V1ObjectMeta(owner_references=[
            client.V1OwnerReference(api_version="apps/v1", kind="Deployment", name="test-app", uid="d")]))
    assert workload_of(apps, pod()) == "test-app"
    assert workload_of(apps, pod(owner=False)) is None


def test_collect_reports_pull_failure_as_structured_fact():
    core = MagicMock()
    core.read_namespaced_pod.return_value = pod()
    core.list_namespaced_event.return_value = NS(items=[NS(reason="Failed", message="manifest unknown")])
    facts = AksDiagnosticProvider(core).collect(make_trigger())
    assert facts.pull_failures[0].image == OLD
    assert any(e.name == "image-pull-failures" for e in facts.items)
    core.read_namespaced_pod_log.assert_not_called()


def deployment(image=OLD, *, generation=2, observed=2, replicas=1, updated=1, available=1, total=1):
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(generation=generation),
        spec=client.V1DeploymentSpec(
            replicas=replicas, selector=client.V1LabelSelector(),
            template=client.V1PodTemplateSpec(spec=client.V1PodSpec(
                containers=[client.V1Container(name="api", image=image)]))),
        status=client.V1DeploymentStatus(
            observed_generation=observed, replicas=total, updated_replicas=updated, available_replicas=available),
    )


ACTION = ActionRequest("fix_image", {
    "namespace": "test-app", "deployment": "test-app", "container": "api",
    "old_image": OLD, "new_image": NEW,
})


def test_execute_patches_only_the_image_of_the_named_container():
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = deployment(OLD)
    AksActionExecutor(apps).execute(ACTION)
    apps.patch_namespaced_deployment.assert_called_once_with(
        "test-app", "test-app",
        {"spec": {"template": {"spec": {"containers": [{"name": "api", "image": NEW}]}}}},
    )


def test_execute_refuses_when_image_changed_since_diagnosis():
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = deployment("acrprod.azurecr.io/something-else:2")
    with pytest.raises(RuntimeError, match="changed"):
        AksActionExecutor(apps).execute(ACTION)
    apps.patch_namespaced_deployment.assert_not_called()


def test_execute_rejects_unknown_actions():
    with pytest.raises(PermissionError):
        AksActionExecutor(MagicMock()).execute(ActionRequest("scale", {}))


def test_verify_waits_for_rollout_then_succeeds():
    apps = MagicMock()
    apps.read_namespaced_deployment.side_effect = [
        deployment(NEW, observed=1),                    # controller has not seen the new spec
        deployment(NEW, updated=1, available=0),        # new pod not ready yet
        deployment(NEW),                                # healthy
    ]
    sleeps = []
    ok = AksActionExecutor(apps, sleep=sleeps.append, poll_interval=1).verify(ACTION)
    assert ok and len(sleeps) == 2


def test_verify_times_out_when_pods_never_become_available():
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = deployment(NEW, available=0)
    clock = iter(range(0, 1000, 10))
    executor = AksActionExecutor(apps, verify_timeout=30, sleep=lambda s: None, monotonic=lambda: next(clock))
    assert executor.verify(ACTION) is False

