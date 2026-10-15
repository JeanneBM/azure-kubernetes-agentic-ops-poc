from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

from .contracts import ActionRequest, Facts, IncidentTrigger

# The only change the agent may make on its own: replace a container image that
# cannot be pulled with a near-identical one that provably exists in the registry.
SAFE_ACTIONS = frozenset({"fix_image"})


class PolicyViolation(Exception):
    """The proposed action is outside the self-cure contract. Leads to escalation."""


class OutOfScope(Exception):
    """The trigger targets a namespace this instance is not allowed to manage."""


class ActionExecutor(Protocol):
    def execute(self, action: ActionRequest) -> None:
        """Execute an already-authorized action."""

    def verify(self, action: ActionRequest) -> bool:
        """Independently check that the action had the intended effect."""


@dataclass(frozen=True)
class ImageRef:
    registry: str
    repository: str
    tag: str

    def __str__(self) -> str:
        return f"{self.registry}/{self.repository}:{self.tag}"


class ImageRegistry(Protocol):
    def exists(self, image: ImageRef) -> bool:
        """True if the tag exists, False if the registry says it does not.

        Must raise on any other outcome (auth failure, network error).
        """


def parse_image(ref: str) -> ImageRef:
    if not ref or "@" in ref:
        raise PolicyViolation(f"Unsupported image reference: {ref!r}")
    first, _, rest = ref.partition("/")
    if not rest or not ("." in first or ":" in first or first == "localhost"):
        raise PolicyViolation(f"Image must include an explicit registry host: {ref!r}")
    repository, sep, tag = rest.rpartition(":")
    if not sep or "/" in tag:
        repository, tag = rest, "latest"
    if not repository or not tag:
        raise PolicyViolation(f"Invalid image reference: {ref!r}")
    return ImageRef(first.lower(), repository, tag)


def edit_distance(a: str, b: str) -> int:
    """Optimal string alignment distance: a swapped pair ('tset') costs 1."""
    rows = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        rows[i][0] = i
    for j in range(len(b) + 1):
        rows[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            rows[i][j] = min(rows[i - 1][j] + 1, rows[i][j - 1] + 1, rows[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                rows[i][j] = min(rows[i][j], rows[i - 2][j - 2] + 1)
    return rows[len(a)][len(b)]


class SelfCurePolicy:
    """Deterministic gate between a model proposal and the cluster.

    The model supplies exactly one value: the corrected image reference. Namespace,
    workload, container and the current image come from the cluster and the trigger.
    """

    def __init__(
        self,
        registry: ImageRegistry,
        *,
        namespace: str,
        allowed_registries: Iterable[str],
        max_edit_distance: int = 2,
    ) -> None:
        self._registry = registry
        self._namespace = namespace
        self._allowed_registries = frozenset(r.lower() for r in allowed_registries)
        self._max_distance = max_edit_distance
        if not namespace or not self._allowed_registries:
            raise ValueError("namespace and allowed_registries are required")

    def namespace_allowed(self, namespace: str) -> bool:
        return namespace == self._namespace

    def authorize(self, trigger: IncidentTrigger, facts: Facts) -> ActionRequest:
        proposal = facts.safe_action
        if proposal is None or proposal.name not in SAFE_ACTIONS:
            name = proposal.name if proposal else None
            raise PolicyViolation(f"Action {name!r} is outside the self-cure whitelist")
        if not self.namespace_allowed(trigger.namespace):
            raise OutOfScope(trigger.namespace)
        if set(proposal.parameters) != {"image"}:
            raise PolicyViolation("fix_image accepts exactly one parameter: image")
        if len(facts.pull_failures) != 1:
            raise PolicyViolation(
                f"Expected exactly one container failing to pull, found {len(facts.pull_failures)}"
            )

        failure = facts.pull_failures[0]
        old = parse_image(failure.image)
        new = parse_image(str(proposal.parameters["image"]))

        if old.registry not in self._allowed_registries:
            raise PolicyViolation(f"Registry {old.registry!r} is not allowed")
        if new.registry != old.registry:
            raise PolicyViolation("Corrected image must stay in the same registry")
        if new == old:
            raise PolicyViolation("Corrected image equals the current image")
        if (
            edit_distance(old.repository, new.repository) > self._max_distance
            or edit_distance(old.tag, new.tag) > self._max_distance
        ):
            raise PolicyViolation("Corrected image is too different from the current one to be a typo fix")

        if self._registry.exists(old):
            raise PolicyViolation("Current image exists in the registry; the pull failure is not a typo")
        if not self._registry.exists(new):
            raise PolicyViolation(f"Corrected image {new} was not found in the registry")

        return ActionRequest(
            "fix_image",
            {
                "namespace": trigger.namespace,
                "deployment": trigger.workload,
                "container": failure.container,
                "old_image": failure.image,
                "new_image": str(new),
            },
        )
