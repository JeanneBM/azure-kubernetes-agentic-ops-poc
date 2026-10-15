"""Image-typo smoke and lifecycle checks for one recorded AKS lab."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from lab_validation import Outcome, Unavailable, azure, execute, utc


class AksLab:
    def __init__(self, root, repository, group, scripts):
        self.root, self.repository, self.group, self.scripts = (
            root,
            repository,
            group,
            scripts,
        )
        self.subscription = None

    def state(self):
        state = json.loads(
            (self.root / f".local/{self.group}.json").read_text(encoding="utf-8-sig")
        )
        if state["resourceGroup"] != self.group:
            raise Unavailable("recorded_group_mismatch")
        return state

    def preflight(self, subscription, confirmation, directory):
        if confirmation != self.group:
            return Outcome("INCONCLUSIVE", "confirmation_mismatch")
        self.subscription = azure("account", "show", subscription=subscription)["id"]
        if azure("group", "exists", "-n", self.group, subscription=self.subscription):
            return Outcome("INCONCLUSIVE", "cycle_requires_absent_group")
        path = self.root / f".local/{self.group}.json"
        if path.exists() and self.state()["subscriptionId"] != self.subscription:
            return Outcome("INCONCLUSIVE", "recorded_subscription_mismatch")
        return Outcome("PASS", "isolated_cycle_preflight")

    def setup(self, subscription, directory):
        executable = shutil.which("pwsh")
        if not executable:
            raise Unavailable("powershell_missing")
        execute(
            [
                executable,
                "-NoProfile",
                "-File",
                f"{self.scripts}/setup-environment.ps1",
                "-SubscriptionId",
                subscription,
                "-ResourceGroup",
                self.group,
            ],
            self.root,
            directory,
            timeout=3600,
        )
        return Outcome("PASS", "setup_completed")

    def identity(self):
        return self.state()["aiName"]

    def kube(self, state, *args):
        executable = shutil.which("kubectl")
        if not executable:
            raise Unavailable("kubectl_missing")
        result = subprocess.run(
            [executable, "--kubeconfig", state["kubeconfig"], *args],
            capture_output=True,
            text=True,
            timeout=240,
        )
        if result.returncode:
            raise Unavailable("kubernetes_operation_failed")
        return result.stdout

    def cluster(self, state):
        actual = azure(
            "aks",
            "show",
            "-g",
            self.group,
            "-n",
            state["aksName"],
            subscription=state["subscriptionId"],
        )
        server = self.kube(
            state,
            "config",
            "view",
            "--minify",
            "-o",
            "jsonpath={.clusters[0].cluster.server}",
        )
        if urlsplit(server.strip()).hostname not in {
            actual.get("fqdn"),
            actual.get("privateFqdn"),
        }:
            raise Unavailable("recorded_cluster_mismatch")
        return actual

    def snapshot(self, state, directory, filename, *args):
        value = json.loads(self.kube(state, *args, "-o", "json"))
        (directory / filename).write_text(json.dumps(value, indent=2), encoding="utf-8")
        return value

    @staticmethod
    def healthy(deployment):
        status, spec, metadata = (
            deployment.get("status", {}),
            deployment["spec"],
            deployment["metadata"],
        )
        replicas = spec.get("replicas", 1)
        return (
            replicas > 0
            and status.get("observedGeneration", 0) >= metadata["generation"]
            and all(
                status.get(key, 0) == replicas
                for key in ("updatedReplicas", "availableReplicas", "replicas")
            )
        )

    def check(self, directory):
        state = self.state()
        self.cluster(state)
        workload = self.snapshot(
            state,
            directory,
            "workload.json",
            "get",
            "deployment/test-app",
            "-n",
            "test-app",
        )
        checks = {"recordedCluster": True, "healthyWorkload": self.healthy(workload)}
        containers = workload["spec"]["template"]["spec"]["containers"]
        checks["singleIntendedContainer"] = (
            len(containers) == 1
            and containers[0]["name"] == "api"
            and containers[0]["image"] == state["demoImage"]
        )
        for role in ("diagnostic", "remediation"):
            deployment = self.snapshot(
                state,
                directory,
                role + ".json",
                "get",
                f"deployment/agentic-ops-{role}",
                "-n",
                "agentic-ops",
            )
            checks[role + "Ready"] = deployment["spec"].get(
                "replicas"
            ) == 1 and self.healthy(deployment)
        return Outcome(
            "PASS" if all(checks.values()) else "INCONCLUSIVE",
            "readiness_checked",
            checks,
        )

    def smoke(self, directory):
        state = self.state()
        self.cluster(state)  # Repeat immediately before mutation.
        before = self.snapshot(
            state,
            directory,
            "before.json",
            "get",
            "deployment/test-app",
            "-n",
            "test-app",
        )
        containers = before["spec"]["template"]["spec"]["containers"]
        good = state["demoImage"]
        if (
            len(containers) != 1
            or containers[0]["name"] != "api"
            or containers[0]["image"] != good
            or not self.healthy(before)
        ):
            return Outcome("INCONCLUSIVE", "healthy_baseline_required")
        registry = good.split("/", 1)[0]
        bad = registry + "/tset-app:1.4.2"
        self.kube(
            state,
            "rollout",
            "restart",
            "deployment/agentic-ops-diagnostic",
            "-n",
            "agentic-ops",
        )
        self.kube(
            state,
            "rollout",
            "status",
            "deployment/agentic-ops-diagnostic",
            "-n",
            "agentic-ops",
            "--timeout=180s",
        )
        started = utc()
        injected = False
        try:
            self.kube(
                state,
                "set",
                "image",
                "deployment/test-app",
                "api=" + bad,
                "-n",
                "test-app",
            )
            injected = True
            decision = None
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                logs = self.kube(
                    state,
                    "logs",
                    "deployment/agentic-ops-diagnostic",
                    "-n",
                    "agentic-ops",
                    "--timestamps",
                    "--since-time=" + started,
                )
                (directory / "diagnostic.log").write_text(logs, encoding="utf-8")
                decision = self.decision(logs)
                if decision:
                    break
                time.sleep(5)
            after = self.snapshot(
                state,
                directory,
                "after.json",
                "get",
                "deployment/test-app",
                "-n",
                "test-app",
            )
            if decision is None:
                return Outcome("INCONCLUSIVE", "decision_not_observed")
            (directory / "decision.json").write_text(
                json.dumps(decision, indent=2), encoding="utf-8"
            )
            action = decision.get("action") or {}
            params = action.get("parameters") or {}
            checks = {
                "agentResolved": decision.get("status") == "resolved"
                and decision.get("action_executed") is True,
                "intendedImageRepair": action.get("name") == "fix_image"
                and params.get("container") == "api"
                and params.get("old_image") == bad
                and params.get("new_image") == good,
                "healthyRollout": self.healthy(after),
                "originalSpecRestored": after["spec"] == before["spec"],
                "twoImageWrites": after["metadata"]["generation"]
                == before["metadata"]["generation"] + 2,
            }
            return Outcome(
                "PASS" if all(checks.values()) else "FAIL",
                "image_repair_checked",
                checks,
            )
        finally:
            if injected:
                # Preserve the failure for inspection. Cycle mode deletes the lab afterwards.
                self.snapshot(
                    state, directory, "pods-after.json", "get", "pods", "-n", "test-app"
                )
                logs = self.kube(
                    state,
                    "logs",
                    "deployment/agentic-ops-remediation",
                    "-n",
                    "agentic-ops",
                    "--timestamps",
                    "--since-time=" + started,
                )
                (directory / "remediation.log").write_text(logs, encoding="utf-8")

    @staticmethod
    def decision(logs):
        found = None
        for line in logs.splitlines():
            try:
                record = json.loads(line[line.index("{") :])
            except (ValueError, json.JSONDecodeError):
                continue
            if (
                record.get("event") == "two_agent_incident_decision"
                and record.get("key") == "test-app/test-app"
                and record.get("status") in {"resolved", "escalated"}
            ):
                found = record
        return found

    def delete(self, confirmation, directory):
        path = self.root / f".local/{self.group}.json"
        subscription = self.subscription
        if path.exists():
            state = self.state()
            subscription = state["subscriptionId"]
            azure("account", "set", subscription=subscription)
            executable = shutil.which("pwsh")
            if not executable:
                raise Unavailable("powershell_missing")
            execute(
                [
                    executable,
                    "-NoProfile",
                    "-File",
                    f"{self.scripts}/delete-environment-resource-group.ps1",
                    "-ResourceGroup",
                    self.group,
                ],
                self.root,
                directory,
                input_text=confirmation + "\n",
                timeout=3600,
            )
        absent = not azure(
            "group", "exists", "-n", self.group, subscription=subscription
        )
        (directory / "cleanup.json").write_text(
            json.dumps({"groupAbsent": absent}), encoding="utf-8"
        )
        return Outcome(
            "PASS" if absent else "FAIL", "cleanup_verified", {"groupAbsent": absent}
        )
