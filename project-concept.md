# Azure Kubernetes Agentic Ops

**An AI-first, human-backed operating model for Kubernetes and AKS incidents.**

Kubernetes detects and resolves safe, reversible issues first. Specialized agents then gather evidence, prepare RCA, and recommend a course of action. Human experts step in only when judgment and approval are genuinely required.

The platform operates through three layers: **K8s Self-Cure**, **Agent Support**, and **Human Support**. It does not replace operations teams—it removes repetitive work from on-call shifts and delivers a complete, grounded incident context when a decision is needed.

## Business problem

Cluster, networking, storage, RBAC, scheduling, and configuration incidents require engineers to switch between `kubectl`, Prometheus, Grafana, Loki, documentation, and ticketing systems. This leads to high MTTR, inconsistent responses, and significant on-call load.

**Goal:** reduce median time-to-RCA—from incident creation to a grounded diagnosis and recommendation—against the current manual baseline, and re-validate the result with every release.

**For:** Platform Engineering, SRE, Cloud Operations, and on-call teams, as well as technical leads who need fast, reliable RCA.

## Architecture — three support layers

```
                    ┌─────────────────────────┐
                    │ Incident Orchestrator    │
                    │ routing + contracts      │
                    └───────────┬─────────────┘
             ┌──────────────────┴──────────────────┐
             ▼                                     ▼
   ┌──────────────────────┐          ┌────────────────────────┐
   │ K8s Self-Cure +      │          │ Human Support          │
   │ Agent Support        │─────────▶│ approval + escalation   │
   │ diagnostics + facts  │  facts   │ review / decision       │
   └──────────────────────┘          └────────────────────────┘
```

### Incident Orchestrator

Routes signals and tickets, sequences the support layers, enforces the Facts → Recommendation contract, and deduplicates and rate-limits incidents.

### K8s Self-Cure + Agent Support

- **Tools:** read-only `kubectl`, Kubernetes API, Prometheus, Loki, Events, and metrics
- **Data sources:** cluster state, logs, metrics, events, and CRD/operator status
- **Knowledge base:** historic incidents and symptom → root-cause mappings
- Cluster state is never invented: tools are the source of truth, and every fact has a source.

### Human Support

- **Tools:** patches, manifests, diffs, runbooks, and RCA generation
- **Data sources:** facts from Agent Support and the knowledge base
- **Knowledge base:** Kubernetes/CNCF documentation, internal runbooks, CVEs, and best practices
- Recommendations are based only on evidence from Agent Support and grounded knowledge.

## Why multi-agent

| Benefit | Rationale |
|---|---|
| Safety | Agent Support is read-only. K8s Self-Cure can execute only whitelisted safe actions; Human Support approves everything else. |
| Specialization | Diagnostics gather and correlate facts; recommendation logic applies procedural knowledge and defines clear next steps. |
| Fewer hallucinations | Recommendations are generated only from tool-backed facts and grounded knowledge. |
| Auditability | Every diagnostic action is logged, and every recommendation has explicit sources. |
| Scalable on-call | Multiple incidents can run in parallel while people review only critical steps. |

## Trigger — how the system wakes up

The system does **not hunt** by polling without purpose. It follows an event-driven, push-based model and activates only when something actually goes wrong.

### Reference scenario: a container enters a crash loop

1. **A container enters a crash loop.** The kubelet restarts it several times and then sets the state to `CrashLoopBackOff`.
2. **A `CrashLoopBackOff` event fires.** A lightweight watcher listens to the Kubernetes Events API (`reason: BackOff`) and forwards the signal directly to the orchestrator.
   - `CrashLoopBackOff` is a useful trigger threshold because the kubelet has already made multiple attempts; the system does not react to a single transient restart.
3. **The orchestrator opens an incident.** It deduplicates by `namespace/pod/deployment` and rate-limits activity. If a deployment already has an open or recently closed incident, the existing ticket is updated instead of starting a new cycle.
4. **Agent Support gathers read-only facts:**
   - `kubectl logs --previous` for logs from the crashed container
   - `kubectl describe pod` for exit code, `OOMKilled`, signal, and reason
   - Pod and node events, including possible resource pressure
   - Time correlation with the latest workload rollout or deployment
   - Liveness and readiness probe configuration
   - Resource limits and requests versus actual usage
5. **K8s Self-Cure takes a safe action, or Agent Support prepares a recommendation.**
6. **Decision: is the issue resolved?**
   - **Yes** → the ticket is closed automatically and the full RCA is attached.
   - **No** → the incident is escalated with the full RCA and a list of actions already attempted.

## Autonomy model: K8s Self-Cure → Agent Support → Human Support

The system starts with low-risk automation, then moves to agent recommendations, while higher-impact decisions remain with a human. Autonomy is not an end in itself—it is a controlled way to reduce time to service recovery.

- **Whitelist of safe, reversible actions—executed without waiting for approval:**
  - `kubectl rollout restart` for suspected transient issues
  - Scaling up or down
  - Evicting a single pod
  - Every action has a defined automatic rollback if it does not help
- **Everything outside the whitelist**—for example changes to an image, configuration, secrets, resource limits, or RBAC policies—requires a **recommendation and human approval**.

### Human escalation criteria

Escalation happens when any of the following is true:

- A whitelisted recovery attempt did not help and the pod still crashes after restart
- The root cause is outside the whitelist, such as an incorrect image name, missing secret, or configuration defect
- Groundedness or confidence is below the quality threshold, so the agent cannot identify the cause reliably

## System workflow beyond crash loops

1. Signal from an alert, event, or ticket
2. Incident Orchestrator → K8s Self-Cure / Agent Support
3. Safe, read-only cluster queries
4. Facts and correlation
5. K8s Self-Cure action or a recommendation for Human Support
6. RCA, runbook, and next-step recommendations

Every step is instrumented: latency, tool success, groundedness, and token cost. Outside the whitelist, the system only proposes a change; Human Support owns its execution.

## Observability

**Traces:** complete incident path (input → tool calls → output), correlation ID, duration of every step and `kubectl`/API call, plus tool status, tokens, and cost per call.

**Metrics:** support-layer latency (p50/p95/p99), tool success and error rates, groundedness score (facts ↔ sources), operator feedback, and time-to-RCA.

**Cost governance:** a token budget per ticket with alerts for runaway queries, such as overly wide Loki time windows. Expensive tool calls are cached and rate-limited before they reach the cluster.

## Evaluation

**Test datasets:** Golden (realistic operational incidents), Synthetic (failure variants), Live (anonymized), and Adversarial (attempts to trigger unsafe actions).

**Criteria:** diagnostic accuracy, groundedness, role adherence, safety, RCA quality, runbook usefulness, tool usage, consistency, and time-to-insight.

**Integration:** automated evaluation in CI/CD, quality gates (`groundedness ≥ 0.85`, `safety = 100%`), a weekly review of the worst incidents, and A/B tests of new agent versions.

## Governance, safety, and grounding

- **Consistency:** low temperature, cached diagnostic results, prompt versioning, and structured JSON outputs between layers
- **Safety:** guardrails block write/exec operations outside the whitelist; read-only RBAC, human-in-the-loop for critical recommendations, and a complete audit log
- **Maintainability:** modular layers and tools, feature flags, regression tests, and documented Facts → Recommendation → Approval contracts
- **Grounding:** Agent Support never invents cluster state. Tools are the source of truth, every fact has a source, and recommendations rely only on facts and the knowledge base.

## Demo scenario: incorrect image name on AKS

A deployment uses an intentionally incorrect image name. The system must identify the issue and propose the fix.

| Faulty image in the manifest | Corrected image |
|---|---|
| `acrprod.azurecr.io/tset-app:1.4.2` (typo: `tset`) | `acrprod.azurecr.io/test-app:1.4.2` |

**Flow:**

1. Ticket `INC-4821` — pods are in `ImagePullBackOff`
2. Agent Support runs `kubectl get/describe pods`, checks Events and ACR tags, and gathers facts
3. Facts show that `tset-app:1.4.2` does not exist, while tag `1.4.2` exists in the `test-app` repository
4. Agent Support generates the RCA, a `kubectl set image` command, and corrected YAML
5. Human Support approves and applies the change; changing an image is outside the whitelist

**Beyond the happy path:** if Agent Support cannot reach high confidence in the root cause—groundedness is below the quality-gate threshold—it does not force a recommendation. The incident goes to Human Support with partial facts attached instead of a low-confidence guess.

## Production-readiness checklist

The system is production-ready when:

- [ ] Full observability is implemented: traces, metrics, and alerts
- [ ] Automated evaluation with clear quality gates runs in CI/CD
- [ ] Tools and sources are the only source of truth, with strong grounding
- [ ] No write or exec operation is possible without human approval outside the defined whitelist; feature flags and versioning are in place

## Key concepts

K8s Self-Cure for safe, reversible actions · Agent Support for diagnostics and RCA · Human Support for higher-impact decisions · observability from day zero · continuous evaluation with quality gates · grounding and safety (tools are the source of truth) · event-driven triggers instead of polling

