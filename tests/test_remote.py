import httpx
import pytest
from fastapi.testclient import TestClient

from agentic_ops.agents import SafeRemediationAgent
from agentic_ops.remote import HandoffPayload, FactsPayload, HttpRemediationAgent, create_remediation_app
from agentic_ops.safety import PolicyViolation
from conftest import FakeExecutor, make_facts, make_policy, make_trigger


def test_policy_rejection_survives_the_http_handoff():
    executor = FakeExecutor()
    app = create_remediation_app(SafeRemediationAgent(make_policy(), executor), agent_token='secret')
    trigger = make_trigger()
    facts = make_facts(name='scale')
    payload = HandoffPayload(**trigger.__dict__, facts=FactsPayload.from_facts(facts))
    with TestClient(app) as server:
        response = server.post('/internal/v1/remediate', headers={'X-Agent-Token': 'secret'}, json=payload.model_dump())
    assert response.status_code == 422
    assert 'whitelist' in response.json()['detail']
    remote = HttpRemediationAgent('http://remediation', 'secret', 'payments', http_client=httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(422, json=response.json()))))
    with pytest.raises(PolicyViolation, match='whitelist'):
        remote.remediate(trigger, facts)
    assert executor.actions == []


def test_remediation_requires_authentication():
    app = create_remediation_app(SafeRemediationAgent(make_policy(), FakeExecutor()), agent_token='secret')
    payload = HandoffPayload(**make_trigger().__dict__, facts=FactsPayload.from_facts(make_facts()))
    with TestClient(app) as server:
        assert server.post('/internal/v1/remediate', json=payload.model_dump()).status_code == 401
