from agentic_ops import (
    ActionRequest, Evidence, Facts, IncidentTrigger, PullFailure, SelfCurePolicy,
)

OLD = "acrprod.azurecr.io/tset-app:1.4.2"
NEW = "acrprod.azurecr.io/test-app:1.4.2"


class FakeRegistry:
    def __init__(self, existing=(), error=None):
        self.existing = set(existing)
        self.error = error
        self.calls = []

    def exists(self, image):
        self.calls.append(str(image))
        if self.error:
            raise self.error
        return str(image) in self.existing


def make_policy(registry=None, **kwargs):
    return SelfCurePolicy(
        registry or FakeRegistry({NEW}),
        namespace="test-app",
        allowed_registries=["acrprod.azurecr.io"],
        **kwargs,
    )


def make_trigger(namespace="test-app", workload="test-app"):
    return IncidentTrigger(namespace, f"{workload}-abc-1", workload, "ImagePullBackOff", "corr-1")


def make_facts(image=NEW, *, groundedness=0.95, failures=None, params=None, name="fix_image", summary="typo"):
    if failures is None:
        failures = (PullFailure("api", OLD),)
    if image is None and params is None:
        action = None
    else:
        action = ActionRequest(name, params if params is not None else {"image": image})
    return Facts(
        (Evidence("pod-phase", "Pending", "k8s://pod/test-app/test-app-abc-1"),),
        groundedness, action, summary, failures,
    )


class FakeDiagnostics:
    def __init__(self, facts=None, error=None):
        self.facts, self.error, self.calls = facts, error, 0

    def collect(self, trigger):
        self.calls += 1
        if self.error:
            raise self.error
        return self.facts


class FakeExecutor:
    def __init__(self, healthy=True, error=None):
        self.actions, self.healthy, self.error = [], healthy, error

    def execute(self, action):
        if self.error:
            raise self.error
        self.actions.append(action)

    def verify(self, action):
        return self.healthy
