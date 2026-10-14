"""Smoke assertions with fake Kubernetes evidence; never connect to a cluster."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "aks_smoke_test_adapter", scripts / "aks_validation.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    lab = module.AksLab(tmp_path, "test", "rg-test", "scripts")
    state = {
        "demoImage": "demo.azurecr.io/payments-api:1.4.2",
        "subscriptionId": "sub",
        "aksName": "aks",
        "kubeconfig": "recorded",
    }
    monkeypatch.setattr(lab, "state", lambda: state)
    return module, lab, state


@pytest.mark.parametrize(
    "alteration,expected",
    [(None, "PASS"), ("spec", "FAIL"), ("health", "FAIL"), ("audit", "FAIL")],
)
def test_requires_matching_audit_original_spec_and_healthy_rollout(
    tmp_path, monkeypatch, adapter, alteration, expected
):
    _, lab, state = adapter
    before = {
        "metadata": {"generation": 1},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {"containers": [{"name": "api", "image": state["demoImage"]}]}
            },
        },
        "status": {
            "observedGeneration": 1,
            "replicas": 1,
            "updatedReplicas": 1,
            "availableReplicas": 1,
        },
    }
    after = copy.deepcopy(before)
    after["metadata"]["generation"] = 3
    after["status"]["observedGeneration"] = 3
    record = {
        "event": "two_agent_incident_decision",
        "key": "payments/payments-api",
        "status": "resolved",
        "action_executed": True,
        "action": {
            "name": "fix_image",
            "parameters": {
                "container": "api",
                "old_image": "demo.azurecr.io/paymnets-api:1.4.2",
                "new_image": state["demoImage"],
            },
        },
    }
    if alteration == "spec":
        after["spec"]["replicas"] = 2
    elif alteration == "health":
        after["status"]["availableReplicas"] = 0
    elif alteration == "audit":
        record["action_executed"] = False
    monkeypatch.setattr(lab, "cluster", lambda state: {})
    monkeypatch.setattr(
        lab,
        "snapshot",
        lambda state, directory, filename, *args: (
            before if filename == "before.json" else after
        ),
    )
    monkeypatch.setattr(
        lab,
        "kube",
        lambda state, *args: (
            "timestamp " + json.dumps(record) if args[0] == "logs" else ""
        ),
    )
    assert lab.smoke(tmp_path).verdict == expected


def test_cluster_mismatch_blocks_all_mutation(tmp_path, monkeypatch, adapter):
    module, lab, state = adapter
    calls = []
    monkeypatch.setattr(
        module,
        "azure",
        lambda *args, **kwargs: {"fqdn": "recorded.example", "privateFqdn": None},
    )
    monkeypatch.setattr(
        lab, "kube", lambda state, *args: calls.append(args) or "https://other.example"
    )
    with pytest.raises(module.Unavailable, match="cluster_mismatch"):
        lab.smoke(tmp_path)
    assert all(args[0] == "config" for args in calls)


def test_does_not_accept_another_workloads_audit(adapter):
    _, lab, _ = adapter
    assert (
        lab.decision(
            json.dumps(
                {
                    "event": "two_agent_incident_decision",
                    "key": "other/workload",
                    "status": "resolved",
                }
            )
        )
        is None
    )
