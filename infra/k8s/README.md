# `infra/k8s` — the fde-sor container run target

The serverless layout (four Lambdas, EventBridge Scheduler, SQS event-source
mappings) is the primary target and is provisioned by `fde-sor deploy`. These
manifests are the same image on Kubernetes, for a deployment that already runs
a cluster and would rather not add Lambda to its operational surface.

**One image, five workloads.** Everything here runs
`$FDE_SOR_IMAGE` with a different `command:`. There is no second build, no
sidecar, and no local state — every workload's entire contract is:

| | |
|---|---|
| **input** | environment variables (`FDE_*`, `AWS_*`) and CLI arguments |
| **output** | JSON logs on stdout |
| **failure** | a non-zero exit code |
| **state** | Postgres and SQS. Nothing on disk, nothing in memory between runs. |

That is also the whole of the kagent-compatibility story: these are plain
single-entrypoint containers with an env-var contract, so anything that can run
a container on Kubernetes — a kagent-managed agent included — can run or invoke
them unchanged. Nothing kagent-specific is added, and nothing is required.

## What is here

| file | workload | notes |
|---|---|---|
| `namespace.yaml` | — | `fde-sor` namespace |
| `configmap.yaml` | — | non-secret `FDE_SOR_*` tuning |
| `externalsecret.yaml` | — | External Secrets Operator → Secrets Manager |
| `secret-plain-example.yaml` | — | for clusters without ESO. **Example only** |
| `cronjob-poll.yaml` | one per polled adapter | `${ADAPTER_KEY}` / `${SCHEDULE}` template |
| `deployment-stream.yaml` | one per event_stream adapter | long-poll consumer |
| `cronjob-drift-scan.yaml` | one, cluster-wide | `0 */6 * * *` |
| `job-backfill.yaml` | ad hoc | one-off historical import |

There is **no expiry CronJob**. Proposal expiry belongs to the gate service,
which owns the `hitl` domain and the `fde_gate_service` credential; running it
here would mean the workload holding customer system-of-record credentials also
held the one role that can merge into the graph.

## Applying

`cronjob-poll.yaml` and `job-backfill.yaml` are templates with `${VAR}`
placeholders — one CronJob per polled adapter, since `poll_cron` is per-adapter
in the database. Kubernetes CronJob takes 5-field cron directly, so the
adapter's `poll_cron` goes in verbatim (unlike the EventBridge path, which needs
`fde-sor deploy sync-schedules` to translate it).

```bash
kubectl apply -f infra/k8s/namespace.yaml
kubectl apply -f infra/k8s/configmap.yaml
kubectl apply -f infra/k8s/externalsecret.yaml

# One per polled adapter. Take ADAPTER_KEY and SCHEDULE from sor.adapter:
#   SELECT adapter_key, poll_cron FROM sor.adapter WHERE is_active AND poll_cron IS NOT NULL;
ENGAGEMENT_ID=... ADAPTER_KEY=jira-prod SCHEDULE='*/15 * * * *' \
  FDE_SOR_IMAGE=$ECR/fde-sor:$TAG \
  envsubst < infra/k8s/cronjob-poll.yaml | kubectl apply -f -

kubectl apply -f infra/k8s/cronjob-drift-scan.yaml
```

Written against verified Kubernetes API shapes; not validated against a live
cluster here (the same convention the deploy scripts use).
