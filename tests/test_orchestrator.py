import threading
from datetime import datetime, timedelta, timezone

import pytest

from agentic_ops import IncidentOrchestrator, IncidentStatus, OutOfScope, SafeRemediationAgent
from conftest import (
    NEW, FakeDiagnostics, FakeExecutor, FakeRegistry, make_facts, make_policy, make_trigger,
)


def build(facts=None, *, executor=None, diagnostics=None, policy=None, **kwargs):
    diagnostics = diagnostics or FakeDiagnostics(facts if facts is not None else make_facts())
    executor = executor or FakeExecutor()
    return IncidentOrchestrator(diagnostics, remediation_agent=SafeRemediationAgent(policy or make_policy(), executor), **kwargs), diagnostics, executor


def test_typo_is_fixed_and_incident_resolved():
    orchestrator, _, executor = build()
    result = orchestrator.handle(make_trigger())
    assert result.incident.status is IncidentStatus.RESOLVED
    assert result.action_executed
    assert executor.actions[0].parameters["new_image"] == NEW
    assert executor.actions[0].parameters["namespace"] == "test-app"


def test_unhealthy_rollout_after_fix_is_escalated_not_resolved():
    orchestrator, _, _ = build(executor=FakeExecutor(healthy=False))
    result = orchestrator.handle(make_trigger())
    assert result.incident.status is IncidentStatus.ESCALATED
    assert result.action_executed
    assert "did not become healthy" in result.incident.reason


def test_model_supplied_target_is_never_executed():
    facts = make_facts(params={"image": NEW, "namespace": "kube-system"})
    orchestrator, _, executor = build(facts)
    result = orchestrator.handle(make_trigger())
    assert result.incident.status is IncidentStatus.ESCALATED
    assert executor.actions == []


def test_out_of_scope_namespace_raises_before_any_work():
    orchestrator, diagnostics, _ = build()
    with pytest.raises(OutOfScope):
        orchestrator.handle(make_trigger("kube-system"))
    assert diagnostics.calls == 0


def test_low_groundedness_is_escalated_without_action():
    orchestrator, _, executor = build(make_facts(groundedness=0.84))
    result = orchestrator.handle(make_trigger())
    assert result.incident.status is IncidentStatus.ESCALATED
    assert executor.actions == []


def test_no_action_creates_recommendation_with_model_summary():
    orchestrator, _, _ = build(make_facts(None, params=None, summary="Missing secret"))
    result = orchestrator.handle(make_trigger())
    assert result.incident.status is IncidentStatus.ESCALATED
    assert result.incident.recommendation.summary == "Missing secret"
    assert result.incident.recommendation.requires_human_approval


@pytest.mark.parametrize("error", [ValueError("bad contract"), RuntimeError("k8s down")])
def test_diagnostic_errors_are_escalated_not_raised(error):
    orchestrator, _, executor = build(diagnostics=FakeDiagnostics(error=error))
    result = orchestrator.handle(make_trigger())
    assert result.incident.status is IncidentStatus.ESCALATED
    assert type(error).__name__ in result.incident.reason
    assert executor.actions == []


def test_registry_error_is_escalated_without_action():
    policy = make_policy(FakeRegistry(error=RuntimeError("401 from ACR")))
    orchestrator, _, executor = build(policy=policy)
    result = orchestrator.handle(make_trigger())
    assert result.incident.status is IncidentStatus.ESCALATED
    assert executor.actions == []


def test_executor_failure_is_escalated():
    orchestrator, _, _ = build(executor=FakeExecutor(error=RuntimeError("image changed")))
    result = orchestrator.handle(make_trigger())
    assert result.incident.status is IncidentStatus.ESCALATED
    assert not result.action_executed


def test_pods_of_one_workload_share_an_incident_and_cooldown_applies():
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    orchestrator, diagnostics, _ = build(cooldown=timedelta(minutes=10), clock=lambda: now[0])
    orchestrator.handle(make_trigger())
    now[0] += timedelta(minutes=5)
    second = orchestrator.handle(make_trigger())
    assert second.deduplicated and diagnostics.calls == 1
    now[0] += timedelta(minutes=6)
    assert not orchestrator.handle(make_trigger()).deduplicated


def test_escalated_incident_is_retried_after_cooldown():
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    orchestrator, diagnostics, _ = build(
        make_facts(groundedness=0.1), cooldown=timedelta(minutes=10), clock=lambda: now[0]
    )
    orchestrator.handle(make_trigger())
    now[0] += timedelta(minutes=11)
    assert not orchestrator.handle(make_trigger()).deduplicated
    assert diagnostics.calls == 2


def test_concurrent_signals_execute_the_action_once():
    entered, release = threading.Event(), threading.Event()

    class SlowDiagnostics(FakeDiagnostics):
        def collect(self, trigger):
            entered.set()
            release.wait(5)
            return super().collect(trigger)

    executor = FakeExecutor()
    orchestrator, _, _ = build(diagnostics=SlowDiagnostics(make_facts()), executor=executor)
    first = threading.Thread(target=lambda: orchestrator.handle(make_trigger()))
    first.start()
    assert entered.wait(5)
    second = orchestrator.handle(make_trigger())  # arrives while the first is in progress
    release.set()
    first.join(5)
    assert second.deduplicated
    assert len(executor.actions) == 1



@pytest.mark.parametrize('status', [429, 503])
def test_transient_diagnostic_error_recovers_before_cooldown(status):
    import httpx

    class RecoveringDiagnostics(FakeDiagnostics):
        def collect(self, trigger):
            self.calls += 1
            if self.calls == 1:
                response = httpx.Response(status, request=httpx.Request('POST', 'https://model'))
                response.raise_for_status()
            return make_facts()

    diagnostics = RecoveringDiagnostics()
    delays = []
    orchestrator, _, executor = build(diagnostics=diagnostics, sleep=delays.append)
    result = orchestrator.handle(make_trigger())
    assert result.incident.status is IncidentStatus.RESOLVED
    assert diagnostics.calls == 2
    assert delays == [1]
    assert len(executor.actions) == 1


def test_diagnostic_retries_are_bounded():
    import httpx
    diagnostics = FakeDiagnostics(error=httpx.ReadTimeout('model unavailable'))
    delays = []
    orchestrator, _, executor = build(diagnostics=diagnostics, sleep=delays.append)
    result = orchestrator.handle(make_trigger())
    assert result.incident.status is IncidentStatus.ESCALATED
    assert diagnostics.calls == 3
    assert delays == [1, 2]
    assert executor.actions == []
