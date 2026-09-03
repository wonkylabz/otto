Here is the plan for platform#342 (vLLM request attribution via `X-Acme-Client` / `X-Acme-Tenant`).

## Context
vLLM serves in prod-a but nothing on the ingress path identifies the calling app: engine metrics carry no caller dimension, the logged client IP is the NLB's own ENI (proxy-protocol v2 off), and `model` doesn't map to callers. The fix asserts identity with two headers set on the ingress path — `X-Acme-Client` (every caller, closed value set) and `X-Acme-Tenant` (platform only, when client=platform). This is **attribution, not authz** (one shared API key, self-asserted values). Both the external gateway `main` and the in-cluster router service must be covered.

## Load-bearing decisions to resolve FIRST
1. **Access-log capture — cluster-wide format vs vLLM-scoped EnvoyFilter.** Editing `orion/aws-core/ingress/helm/istiod.yaml` `meshConfig` to append `%REQ(X-ACME-CLIENT)%`/`%REQ(X-ACME-TENANT)%` to the default format reshapes every gateway host's log line **and** every injected sidecar's, but it is the single change that covers both the gateway and the future router sidecar; the ticket verified nothing under `observability/` parses these lines. Recommend cluster-wide `accessLogFormat` that **preserves the current default fields** and appends the two `%REQ%` fields — NOT `accessLogEncoding: JSON` (reshapes every app's line). Facet via `capture()` in NRQL.
2. **Router sidecar protocol sniffing.** Injecting a sidecar only enforces an L7 rule if Istio classifies the router inbound port as HTTP. Must be proven in dev-a before prod-a and before any in-cluster DENY.

## Phasing rule
Enforcement (reject missing/unknown header) is the LAST step, gated on every live caller — platform (~180 req/day real traffic), MOBILE, WEBAPP, and internal apps — actually sending the header. Enforcing before adoption breaks live traffic.

## Steps

**Phase 1 — Contract + owner comms (no infra change)**
1. Document allowed `X-Acme-Client` values (`mobile`, `stream-svc`, `platform`, `mosaic`, `otto`, `beacon`, `atlas`), the `X-Acme-Tenant` opaque-id rule, and the attribution-not-authz limitation in a new `apps/vllm/CLAUDE.md` (none exists; add a pointer from root `.claude/CLAUDE.md`).
2. Add `allowed_clients` (list) variable in `apps/vllm/parameters.tf` as the single source for both DENY policies and docs.
3. Communicate the contract to platform, MOBILE, WEBAPP owners (AC) — note the platform must set an **opaque** `X-Acme-Tenant` (lands in NR logs).

**Phase 2 — Observability (observe-only)**
4. Add cluster-wide `accessLogFormat` to `orion/aws-core/ingress/helm/istiod.yaml` `meshConfig` (append two `%REQ%` fields). Apply dev-a → confirm fields in `main-istio` logs in NR → prod-a. Config push, no data-plane roll.
5. Write NRQL breaking counts down by `X-Acme-Client` (via `capture()`) and by `X-Acme-Tenant` for `platform`; add a panel to Grafana dashboard uid `abc1234` (`apps/vllm/observability/dashboard/`, US Grafana). Export JSON, commit (GitOps).

**Phase 3 — In-cluster plumbing (mesh router, no rejection)**
6. Add `sidecar.istio.io/inject: "true"` under `routerSpec.podAnnotations` in `apps/vllm/values.yaml`. Apply dev-a; verify router `2/2`, engines reachable, requests succeed, inbound port classified HTTP (fix Service port naming/`appProtocol` if sniffing misclassifies). Then prod-a.

**Phase 4 — Adoption gate (external dependency)**
7. Wait until NR shows live traffic on both paths carrying a valid `X-Acme-Client` (and `platform` traffic carrying `X-Acme-Tenant`). Record any caller still bypassing via a direct engine Service call (AC). Proceed only when no live caller is missing the header.

**Phase 5 — Enforcement (dev-a → prod-a)**
8. Extend the external DENY in `apps/vllm/authorization-policy.tf` (host-scoped on gateway `main`, keep the existing `authorization notValues ["*"]` rule) with two OR'd rules: `x-acme-client notValues = var.allowed_clients` (denies absent OR unrecognised); and a rule ANDing `x-acme-client values ["platform"]` with `x-acme-tenant notValues ["*"]`.
9. Add a NEW in-cluster `AuthorizationPolicy` in the `vllm` namespace, `action DENY`, `selector` on router labels (`app.kubernetes.io/component=router`, `.../instance=vllm`, `.../part-of=vllm-stack`), same two client/tenant `when` rules (no host scoping — selects the workload).
10. Apply dev-a, verify all ACs, then prod-a.

**Phase 6 — Acceptance verification**
11. External: `infer.example.com` missing/bad `X-Acme-Client` → 403, valid → pass; `platform` without `X-Acme-Tenant` → 403, with it → pass.
12. In-cluster: direct `vllm-router-service.vllm.svc.cluster.local` without header → rejected (bypass closed).
13. Blast radius: confirm another shared-gateway host (e.g. `api.example.com`) unaffected by both the DENY and the access-log change.
14. Confirm `vllm:request_success_total` keeps incrementing post router roll; record any residual bypass (direct engine Service hit).
15. `mage fmt`, self-review, open DRAFT PR(s). If split, merge order: **P(access-log+docs) → P(router sidecar) → P(enforcement)**.

## Risks & assumptions
- **No live cluster/NR state verified from here** (read-only preview). Router labels, port naming, and access-log field availability come from the ticket's 2026-08-03 verification; re-confirm before applying.
- **Access-log format is shared** — appending fields could break anything parsing `main-istio`'s default line; JSON would break it harder. Mitigation: append-only, preserve defaults, verify one other host.
- **Router protocol sniffing** is the top technical risk: if the inbound port isn't seen as HTTP, the L7 rule silently enforces nothing (same trap as CLAUDE.md's `targetRefs`-binds-to-nothing). Prove in dev-a first.
- **Adoption is an external dependency** — enforcement cannot land until platform/MOBILE/WEBAPP ship the header, else live customer traffic 403s. The plan gates enforcement on Phase 4.
- **`X-Acme-Tenant` values land in NR logs** — relies on owners using opaque ids; the ingress path can't validate opacity.