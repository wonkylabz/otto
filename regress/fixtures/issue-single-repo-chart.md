## Objective
Raise the model server's readiness probe timeout from 5s to 15s; cold starts fail the probe.

## Where
**modelsrv repo**
- `deploy/values.yaml`: `readinessProbe.timeoutSeconds: 15`.

Context: the platform repo's ingress is unaffected; no pipelines change.

## Acceptance criteria
- [ ] Pods pass readiness on a cold start in dev.
