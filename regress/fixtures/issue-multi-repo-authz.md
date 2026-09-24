## Objective
Add a log-only authorisation sidecar service in front of the model server, so every request records which client called which model.

## Where
**modelsrv repo**
- New Go service + Dockerfile under `authz/`, built like `build/`.
- `deploy/`: Deployment + Service, a CUSTOM AuthorizationPolicy, a new image-tag variable in `dependencies.tf`.

**platform repo** (own PR, no-op until a policy references it)
- `ingress/helm/istiod.yaml`: add a `meshConfig.extensionProviders` entry pointing at the new service.
- `core/ecr/locals.tf`: new ECR repo entry.

**pipelines repo**
- `modelsrv-pipeline/settings.kts`: new image BuildType feeding the new variable into the deploy job.

## Acceptance criteria
- [ ] Every request produces one log line with client and model.
- [ ] If the service is down, requests still succeed (fail-open).
