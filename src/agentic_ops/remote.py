from __future__ import annotations

import hmac
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .agents import RemediationAgent, RemediationOutcome, SafeRemediationAgent
from .contracts import ActionRequest, Evidence, Facts, IncidentTrigger, PullFailure
from .safety import OutOfScope, PolicyViolation


class ActionPayload(BaseModel):
    name: str = Field(min_length=1)
    parameters: dict[str, str] = {}


class EvidencePayload(BaseModel):
    name: str = Field(min_length=1)
    value: str = Field(min_length=1)
    source: str = Field(min_length=1)


class PullFailurePayload(BaseModel):
    container: str = Field(min_length=1)
    image: str = Field(min_length=1)


class FactsPayload(BaseModel):
    evidence: list[EvidencePayload]
    groundedness: float = Field(ge=0, le=1)
    safe_action: ActionPayload | None = None
    summary: str = ""
    pull_failures: list[PullFailurePayload] = []

    @classmethod
    def from_facts(cls, facts: Facts) -> "FactsPayload":
        return cls(
            evidence=[EvidencePayload(**item.__dict__) for item in facts.items],
            groundedness=facts.groundedness,
            safe_action=ActionPayload(
                name=facts.safe_action.name,
                parameters=dict(facts.safe_action.parameters),
            ) if facts.safe_action else None,
            summary=facts.summary,
            pull_failures=[PullFailurePayload(**item.__dict__) for item in facts.pull_failures],
        )

    def to_facts(self) -> Facts:
        return Facts(
            tuple(Evidence(item.name, item.value, item.source) for item in self.evidence),
            self.groundedness,
            ActionRequest(self.safe_action.name, self.safe_action.parameters)
            if self.safe_action else None,
            self.summary,
            tuple(PullFailure(item.container, item.image) for item in self.pull_failures),
        )


class HandoffPayload(BaseModel):
    namespace: str = Field(min_length=1)
    pod: str = Field(min_length=1)
    workload: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    facts: FactsPayload

    def trigger(self) -> IncidentTrigger:
        return IncidentTrigger(
            self.namespace, self.pod, self.workload, self.reason, self.correlation_id
        )


class HttpRemediationAgent(RemediationAgent):
    """Client used by the diagnostic workload to hand off typed facts internally."""

    def __init__(
        self, endpoint: str, token: str, namespace: str, *, http_client: httpx.Client | None = None
    ) -> None:
        if not endpoint or not token or not namespace:
            raise ValueError("endpoint, token, and namespace are required")
        self._endpoint = endpoint.rstrip("/")
        self._token = token
        self._namespace = namespace
        self._client = http_client or httpx.Client(timeout=httpx.Timeout(300, connect=10))

    def manages(self, namespace: str) -> bool:
        return namespace == self._namespace

    def remediate(self, trigger: IncidentTrigger, facts: Facts) -> RemediationOutcome:
        payload = HandoffPayload(
            namespace=trigger.namespace,
            pod=trigger.pod,
            workload=trigger.workload,
            reason=trigger.reason,
            correlation_id=trigger.correlation_id,
            facts=FactsPayload.from_facts(facts),
        )
        response = self._client.post(
            f"{self._endpoint}/internal/v1/remediate",
            headers={"X-Agent-Token": self._token},
            json=payload.model_dump(),
        )
        if response.status_code == 422:
            raise PolicyViolation(str(response.json().get("detail", "policy rejected proposal")))
        if response.status_code == 403:
            raise OutOfScope(str(response.json().get("detail", "namespace outside scope")))
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        action_data = data.get("action")
        action = None
        if action_data is not None:
            action = ActionRequest(str(action_data["name"]), dict(action_data["parameters"]))
        return RemediationOutcome(
            action=action,
            executed=bool(data["executed"]),
            successful=bool(data["successful"]),
            reason=str(data.get("reason", "")),
        )


def create_remediation_app(agent: SafeRemediationAgent, *, agent_token: str) -> FastAPI:
    """Serve the network-isolated remediation workload's internal API."""
    if not agent_token:
        raise ValueError("AGENTIC_OPS_REMEDIATION_TOKEN is required")

    app = FastAPI(title="AKS Agentic Ops Remediation Agent")

    def require_token(x_agent_token: str | None = Header(default=None)) -> None:
        if not hmac.compare_digest(x_agent_token or "", agent_token):
            raise HTTPException(status_code=401, detail="invalid agent token")

    @app.get("/healthz")
    def health():
        return {"status": "ok", "role": "remediation"}

    @app.post("/internal/v1/remediate", dependencies=[Depends(require_token)])
    def remediate(payload: HandoffPayload):
        try:
            outcome = agent.remediate(payload.trigger(), payload.facts.to_facts())
        except PolicyViolation as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except OutOfScope as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return {
            "executed": outcome.executed,
            "successful": outcome.successful,
            "reason": outcome.reason,
            "action": (
                {"name": outcome.action.name, "parameters": dict(outcome.action.parameters)}
                if outcome.action else None
            ),
        }

    return app
