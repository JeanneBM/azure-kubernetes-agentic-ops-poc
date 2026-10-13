from __future__ import annotations

import json
import logging
import threading
import time

import httpx
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from .agents import RemediationAgent
from .contracts import ActionRequest, Facts, Incident, IncidentStatus, IncidentTrigger, Recommendation
from .safety import OutOfScope, PolicyViolation

audit = logging.getLogger("agentic_ops.audit")


class DiagnosticProvider(Protocol):
    """First agent: collect read-only facts and a proposed action."""

    def collect(self, trigger: IncidentTrigger) -> Facts:
        """Collect source-backed facts."""


@dataclass(frozen=True)
class IncidentResult:
    incident: Incident
    deduplicated: bool = False
    action_executed: bool = False


class IncidentOrchestrator:
    """Coordinate a research-capable diagnostic agent and isolated executor.

    Agent 1 may use research sources to produce a typed proposal. Agent 2
    receives only Facts, has no search or browser dependency, and independently
    authorizes, executes, and verifies it. A failure at either stage escalates.
    """

    def __init__(
        self,
        diagnostics: DiagnosticProvider,
        *,
        remediation_agent: RemediationAgent,
        groundedness_threshold: float = 0.85,
        cooldown: timedelta = timedelta(minutes=15),
        clock: Callable[[], datetime] | None = None,
        diagnostic_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 0 <= groundedness_threshold <= 1:
            raise ValueError("groundedness_threshold must be between 0 and 1")
        if cooldown < timedelta(0):
            raise ValueError("cooldown cannot be negative")
        if diagnostic_attempts < 1:
            raise ValueError("diagnostic_attempts must be positive")
        self._diagnostic_attempts = diagnostic_attempts
        self._sleep = sleep
        self._diagnostics = diagnostics
        self._remediation_agent = remediation_agent
        self._groundedness_threshold = groundedness_threshold
        self._cooldown = cooldown
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._incidents: dict[str, tuple[Incident, datetime]] = {}
        self._lock = threading.Lock()

    def manages(self, namespace: str) -> bool:
        return self._remediation_agent.manages(namespace)

    def handle(self, trigger: IncidentTrigger) -> IncidentResult:
        if not self.manages(trigger.namespace):
            raise OutOfScope(trigger.namespace)

        key = trigger.deduplication_key
        now = self._clock()
        with self._lock:
            existing = self._incidents.get(key)
            if existing and (
                existing[0].status is IncidentStatus.OPEN or now - existing[1] < self._cooldown
            ):
                return IncidentResult(existing[0], deduplicated=True)
            self._incidents[key] = (
                Incident(trigger, IncidentStatus.OPEN, Facts((), 0.0)),
                now,
            )

        incident, executed = self._process(trigger)
        with self._lock:
            self._incidents[key] = (incident, self._clock())
        self._audit(incident, executed)
        return IncidentResult(incident, action_executed=executed)

    def _process(self, trigger: IncidentTrigger) -> tuple[Incident, bool]:
        facts = Facts((), 0.0)
        action: ActionRequest | None = None
        executed = False
        stage = "diagnostic agent"
        try:
            facts = self._collect_with_retry(trigger)
            if facts.groundedness < self._groundedness_threshold:
                return self._escalate(
                    trigger, facts,
                    f"diagnostic agent groundedness {facts.groundedness:.2f} is below "
                    f"{self._groundedness_threshold:.2f}",
                ), False
            if facts.safe_action is None:
                return self._escalate(
                    trigger, facts, "diagnostic agent proposed no safe action"
                ), False

            stage = "second agent"
            outcome = self._remediation_agent.remediate(trigger, facts)
            action, executed = outcome.action, outcome.executed
            if outcome.successful:
                return Incident(trigger, IncidentStatus.RESOLVED, facts, action=action), executed
            return self._escalate(
                trigger,
                facts,
                outcome.reason or "second agent could not complete remediation",
                action,
            ), executed
        except PolicyViolation as violation:
            return self._escalate(trigger, facts, f"{stage} policy: {violation}", action), executed
        except Exception as error:  # noqa: BLE001
            audit.exception("incident handling failed")
            return self._escalate(
                trigger, facts, f"{stage} {type(error).__name__}: {error}", action
            ), executed

    def _collect_with_retry(self, trigger: IncidentTrigger) -> Facts:
        # Diagnosis is read-only. Never retry a handoff whose write outcome is unknown.
        for attempt in range(self._diagnostic_attempts):
            try:
                return self._diagnostics.collect(trigger)
            except (httpx.TransportError, httpx.HTTPStatusError) as error:
                retryable = isinstance(error, httpx.TransportError) or (
                    error.response.status_code in {408, 429, 500, 502, 503, 504}
                )
                if not retryable or attempt + 1 == self._diagnostic_attempts:
                    raise
                self._sleep(2 ** attempt)
        raise AssertionError("unreachable")

    @staticmethod
    def _escalate(
        trigger: IncidentTrigger,
        facts: Facts,
        reason: str,
        action: ActionRequest | None = None,
    ) -> Incident:
        recommendation = None
        if facts.items:
            recommendation = Recommendation(
                summary=facts.summary or f"Investigate {trigger.reason} for {trigger.deduplication_key}",
                action=ActionRequest(
                    "human_review",
                    {"namespace": trigger.namespace, "workload": trigger.workload},
                ),
                evidence_sources=tuple(item.source for item in facts.items),
            )
        return Incident(
            trigger, IncidentStatus.ESCALATED, facts, recommendation, reason=reason, action=action
        )

    @staticmethod
    def _audit(incident: Incident, executed: bool) -> None:
        audit.info(json.dumps({
            "event": "two_agent_incident_decision",
            "correlation_id": incident.trigger.correlation_id,
            "key": incident.trigger.deduplication_key,
            "trigger_reason": incident.trigger.reason,
            "status": incident.status.value,
            "reason": incident.reason,
            "groundedness": incident.facts.groundedness,
            "summary": incident.facts.summary,
            "action": (
                {"name": incident.action.name, "parameters": dict(incident.action.parameters)}
                if incident.action else None
            ),
            "action_executed": executed,
            "evidence_sources": [item.source for item in incident.facts.items],
        }))

