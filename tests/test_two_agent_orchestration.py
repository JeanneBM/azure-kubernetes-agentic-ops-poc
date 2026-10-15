from agentic_ops import IncidentOrchestrator, IncidentStatus, SafeRemediationAgent
from agentic_ops.agents import RemediationOutcome
from conftest import FakeDiagnostics, FakeExecutor, make_facts, make_policy, make_trigger


def test_second_agent_reports_failed_verification_without_resolving():
    agent = SafeRemediationAgent(make_policy(), FakeExecutor(healthy=False))

    outcome = agent.remediate(make_trigger(), make_facts())

    assert outcome.executed
    assert not outcome.successful
    assert "second agent" in outcome.reason


def test_second_agent_failure_escalates_the_incident():
    class FailingSecondAgent:
        def manages(self, namespace):
            return namespace == "test-app"

        def remediate(self, trigger, facts):
            raise RuntimeError("rollout API unavailable")

    result = IncidentOrchestrator(
        FakeDiagnostics(make_facts()), remediation_agent=FailingSecondAgent()
    ).handle(make_trigger())

    assert result.incident.status is IncidentStatus.ESCALATED
    assert not result.action_executed
    assert result.incident.reason == "second agent RuntimeError: rollout API unavailable"


def test_orchestrator_uses_the_isolated_second_agent_outcome():
    class DecliningSecondAgent:
        def __init__(self):
            self.calls = 0

        def manages(self, namespace):
            return namespace == "test-app"

        def remediate(self, trigger, facts):
            self.calls += 1
            return RemediationOutcome(None, False, False, "second agent declined the action")

    second = DecliningSecondAgent()
    result = IncidentOrchestrator(
        FakeDiagnostics(make_facts()), remediation_agent=second
    ).handle(make_trigger())

    assert second.calls == 1
    assert result.incident.status is IncidentStatus.ESCALATED
    assert result.incident.reason == "second agent declined the action"




def test_runtime_requires_an_explicit_supported_role(monkeypatch):
    import pytest
    from agentic_ops.split_app import build_app

    monkeypatch.delenv("AGENTIC_OPS_ROLE", raising=False)
    with pytest.raises(ValueError, match="diagnostic or remediation"):
        build_app()
    monkeypatch.setenv("AGENTIC_OPS_ROLE", "unsupported")
    with pytest.raises(ValueError, match="diagnostic or remediation"):
        build_app()
