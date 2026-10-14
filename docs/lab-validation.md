# Individual lab validation report

This repository produces its **own** report. There is no cross-repository runner
or combined report. All four repositories use schema `1.0`, the same verdict
semantics and exit codes; the smoke assertions are specific to this project.

## Existing lab: smoke

From this repository's root, after its setup and dependency installation:

```sh
python scripts/validate_lab.py smoke
```

Smoke checks: Image-typo repair: a matching agent audit, original Deployment spec, healthy rollout and exactly two Deployment generations (injection + repair). The diagnostic agent is restarted to clear incident deduplication. Smoke mode requires a healthy, single-container baseline and does not manually repair a failed trial.

The existing lab remains provisioned after smoke. A failed AKS trial can leave
the workload faulty for inspection. A readiness failure prevents fault injection.
Run the normal cleanup command after saving the report/evidence.

## Dedicated lab: full cycle

The target group must initially be absent. This command creates billable Azure
resources, tests generation 1, deletes and verifies it, recreates with fresh
resource names, tests generation 2, then deletes and verifies it again:

```sh
python scripts/validate_lab.py cycle --subscription '<YOUR_SUBSCRIPTION_ID>' --confirm-delete rg-agentic-ops-poc-lab
```

The group name is explicit consent for this command's two cleanup operations.
A pre-existing group is refused; cycle mode does not delete an existing lab to
make room. Keep the recorded `.local/` state and cleanup inventory. Cleanup is
attempted after partial setup, test failures and interruption. Failed cleanup
prevents recreation. Inspect the report and rerun the normal cleanup script if
verification fails. Check region/model/SKU availability and permissions first.
Cycle uses the setup defaults; use smoke for an existing customized lab.

## Files and verdicts

Each execution writes `.local/reports/<UTC-run-id>/report.json` and `report.md`
in **this checkout**, with phase evidence in the same run directory. Reports
link local evidence and include UTC times, durations, assertions and verdicts.
They survive cleanup and are separate from scenario/evaluation evidence.

| Verdict | Meaning | Exit code |
| --- | --- | ---: |
| PASS | All requested phases completed and all smoke assertions passed; cycle also verified both deletions and fresh resource names. | 0 |
| FAIL | Observed evidence violates an assertion (for example wrong answer, unverified repair, export after rejection, reused names or resources remaining). | 1 |
| INCONCLUSIVE | Execution, prerequisites, networking or evidence collection prevented a determination; not a pass. | 2 |

A smoke PASS verifies that smoke's limited scenario, not production readiness or
all evaluation scenarios. A report produced during an offline mocked test is
not live Azure evidence. The new lifecycle must still be run on Azure; no live
result is implied by the presence of these scripts. Raw logs and SDK/evaluation
evidence remain local and must be reviewed before sharing.
