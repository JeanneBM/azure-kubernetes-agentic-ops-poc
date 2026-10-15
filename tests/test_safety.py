import pytest

from agentic_ops import ActionRequest, OutOfScope, PolicyViolation, PullFailure
from agentic_ops.safety import edit_distance, parse_image

from conftest import NEW, OLD, FakeRegistry, make_facts, make_policy, make_trigger


def test_typo_fix_is_authorized_and_bound_to_trigger_context():
    action = make_policy().authorize(make_trigger(), make_facts())
    assert action.name == "fix_image"
    assert dict(action.parameters) == {
        "namespace": "test-app", "deployment": "test-app", "container": "api",
        "old_image": OLD, "new_image": NEW,
    }


def test_model_cannot_supply_namespace_or_deployment():
    facts = make_facts(params={"image": NEW, "namespace": "kube-system", "deployment": "coredns"})
    with pytest.raises(PolicyViolation):
        make_policy().authorize(make_trigger(), facts)


@pytest.mark.parametrize("name", ["scale", "rollout_restart", "evict_pod", "patch_deployment"])
def test_actions_outside_whitelist_are_rejected(name):
    with pytest.raises(PolicyViolation):
        make_policy().authorize(make_trigger(), make_facts(name=name))


def test_other_registry_is_rejected():
    facts = make_facts("evil.azurecr.io/test-app:1.4.2")
    with pytest.raises(PolicyViolation, match="same registry"):
        make_policy(FakeRegistry({"evil.azurecr.io/test-app:1.4.2"})).authorize(make_trigger(), facts)


def test_registry_not_on_allowlist_is_rejected():
    facts = make_facts(failures=(PullFailure("api", "other.azurecr.io/tset-app:1.4.2"),),
                       image="other.azurecr.io/test-app:1.4.2")
    with pytest.raises(PolicyViolation, match="not allowed"):
        make_policy().authorize(make_trigger(), facts)


def test_completely_different_image_is_not_a_typo_fix():
    other = "acrprod.azurecr.io/billing-service:9.9.9"
    with pytest.raises(PolicyViolation, match="too different"):
        make_policy(FakeRegistry({other})).authorize(make_trigger(), make_facts(other))


def test_existing_current_image_means_not_a_typo():
    registry = FakeRegistry({OLD, NEW})
    with pytest.raises(PolicyViolation, match="not a typo"):
        make_policy(registry).authorize(make_trigger(), make_facts())


def test_corrected_image_must_exist():
    with pytest.raises(PolicyViolation, match="not found"):
        make_policy(FakeRegistry(set())).authorize(make_trigger(), make_facts())


def test_registry_errors_propagate_so_the_incident_is_escalated():
    registry = FakeRegistry(error=RuntimeError("401"))
    with pytest.raises(RuntimeError):
        make_policy(registry).authorize(make_trigger(), make_facts())


def test_requires_exactly_one_pull_failure():
    with pytest.raises(PolicyViolation, match="exactly one"):
        make_policy().authorize(make_trigger(), make_facts(failures=()))


def test_other_namespace_is_out_of_scope():
    with pytest.raises(OutOfScope):
        make_policy().authorize(make_trigger("kube-system"), make_facts())


def test_digest_and_dockerhub_references_are_unsupported():
    for ref in ["acrprod.azurecr.io/api@sha256:abc", "nginx:1.27", "library/nginx:1.27"]:
        with pytest.raises(PolicyViolation):
            parse_image(ref)


def test_parse_image_defaults_tag_and_handles_ports():
    assert str(parse_image("acr.io/team/app")) == "acr.io/team/app:latest"
    assert str(parse_image("localhost:5000/app:1")) == "localhost:5000/app:1"


def test_edit_distance_counts_transposition_as_one():
    assert edit_distance("tset-app", "test-app") == 1
    assert edit_distance("1.4.2", "1.4.3") == 1
    assert edit_distance("abc", "xyz") == 3
