I have enough to produce the plan. The ticket is thorough; my inspection confirms the file structure and surfaced one genuinely load-bearing detail the ticket understates (the sidecar-injection mechanism, and that a workload-scoped DENY on the router would also intercept kubelet probes and the NR `/metrics` scrape).

---

## Plan: vllm request attribution via X-Acme-Client / X-Acme-Tenant (issue #342)

Envs in scope: **dev-a** and **prod-a** (the two `tf.Environments` for `apps/vllm`). `vars/dev` is a decommissioned leftover — ignore it.

1. **Resolve the load-bearing unknown: how sidecar injection actually fires for the router, without meshing the GPU engines.** On dev-a (`aws-vault exec development` + `kubectx orion-dev-a`), read-only inspect: (a) the Istio sidecar-injector `MutatingWebhookConfiguration` — determine whether opt-in is by namespace label (`istio-injection=enabled` / `istio.io/rev`) or by the object-selector webhook matching the pod **label** `sidecar.istio.io/inject: "true"`; (b) whether the `production-stack` chart's `routerSpec` can set pod **labels** (not just `podAnnotations`) on the router — the annotation the ticket proposes will NOT trigger injection in an unlabeled namespace under the default object-selector webhook; (c) the router `Service` port name/`appProtocol` so Istio classifies inbound as HTTP (protocol sniffing). **Decision gate:** the injection mechanism must opt in the router pod ONLY. Do not label the `vllm` namespace (`main.tf:3`) — that would inject sidecars into the GPU engine pods on their next roll, which the ticket explicitly forbids. If the chart can't set router pod labels, plan a per-pod label via a `kubernetes_manifest`/patch or namespace-label + per-engine `sidecar.istio.io/inject:"false"` opt-out; pick whichever isolates cleanly.

2. **Create the branch** `342-vllm-client-attribution` off `master` (never commit on master).

3. **Define the contract in Terraform (source of truth).** Add a local in `apps/vllm/locals.tf` for the allowed client set (`["mobile","stream-svc","platform","mosaic","otto","beacon","atlas"]`) so both policies and any validation reference one list. No behavior yet.

4. **Observe first — add header capture to the gateway access log.** Edit `orion/aws-core/ingress/helm/istiod.yaml` `meshConfig` (currently `accessLogFile: /dev/stdout`, no `accessLogFormat`): add an `accessLogFormat` that reproduces Envoy's **full default text line** plus trailing `%REQ(X-ACME-CLIENT)%` and `%REQ(X-ACME-TENANT)%`. Keep text format, not `accessLogEncoding: JSON` — JSON reshapes every app's access-log line (broad blast radius); the two appended fields only add trailing tokens and NR stores the line as an unparsed `message` (nothing under `observability/` parses it — confirmed in the ticket). Apply `orion/aws-core` to **dev-a first, then prod-a** (this is a config push, no data-plane roll).
   - *Blast radius:* this is the shared gateway used by every HTTPRoute (`api.example.com`, workflow-svc, webapp, …). Their lines just gain two trailing `-` fields. Verify with a New Relic query (`keyset() FROM Log WHERE pod_name LIKE 'main-istio%'`) that the header fields now appear and no downstream log parser/dashboard broke.

5. **Document the contract and notify owners (non-code AC, and it must precede enforcement).** Write the allowed `X-Acme-Client` values and the `X-Acme-Tenant`-required-when-`platform` rule into `apps/vllm` (README or a new `CLAUDE.md` — there is none today). Communicate to the **platform**, **MOBILE** and **WEBAPP** owners that the header will become mandatory. This is the observe-window: real callers must start sending the header before any DENY.

6. **Add the router sidecar (in-cluster prerequisite), gated on step 1's mechanism.** Apply the chosen router-only injection change (`apps/vllm/values.yaml` `routerSpec` and/or a targeted label patch). Roll the router (CPU pod, seconds). **Verify before proceeding:** router goes `2/2`, `vllm:request_success_total` keeps incrementing (engines still reachable — AC), and Istio classifies the router's inbound port as HTTP so an L7 header rule can evaluate. GPU engine pods stay `1/1`, unmeshed.

7. **Add the in-cluster AuthorizationPolicy (router-scoped), still non-blocking for infra traffic.** New `kubernetes_manifest` in `apps/vllm/authorization-policy.tf`, in the `vllm` namespace, `selector` matching the router labels (`app.kubernetes.io/name=router`, `instance=vllm`, `component=router`, `part-of=vllm-stack`), `action: DENY`. **Critical scoping:** restrict rules to `to.operation.paths: ["/v1*"]` (mirroring `httproute.tf`) so the DENY does NOT intercept kubelet `/health` probes or the NR `/metrics` scrape on port 8000 — an unscoped workload DENY would block both and break the router. Same two rule shapes as the external policy (below).

8. **Extend the external DENY on gateway `main` — the enforcing step, applied last and phased.** In `apps/vllm/authorization-policy.tf`, add two rules to the existing host-scoped `DENY` (keep the same `to.hosts = [public_fqdn, "${public_fqdn}:*"]`):
   - Rule A: `when key=request.headers[x-acme-client] notValues=[<allowed set>]` → rejects missing or unrecognised client.
   - Rule B: `when` ANDs `request.headers[x-acme-client] values=["platform"]` **and** `request.headers[x-acme-tenant] notValues=["*"]` → rejects `platform` with no tenant.
   These are additional independent DENY rules (OR semantics), alongside the existing `authorization notValues ["*"]` rule.
   - **Preconditions before this step:** step 4 access logs confirm the live platform caller (~180 `/v1/chat/completions`/day today) is already sending a valid `X-Acme-Client`; owners (step 5) confirmed; rollback = revert this hunk and re-apply.
   - *Blast radius:* host-scoped, so other `main` HTTPRoutes are untouched.

9. **Apply enforcement dev-a → prod-a, validating at each stop.** For each env: `mage apply` (`apps/vllm` for the policies, `orion/aws-core` already done in step 4). Validate against the ACs:
   - External: `infer.<domain>` with no `X-Acme-Client` (and with a bogus value) → 403 at gateway; a valid value → passes to engine (401 only if token missing).
   - `X-Acme-Client: platform` without tenant → 403; with tenant → passes.
   - In-cluster: direct call to `vllm-router-service.vllm.svc.cluster.local/v1/...` without the header → rejected (bypass closed), while router probes/metrics still succeed.
   - Regression: one other host (`api.example.com`) still serves normally.

10. **Dashboard + NRQL (AC).** Add a panel to `apps/vllm/observability/dashboard/dashboards/vllm.json` (uid `abc1234`, US Grafana only) with NRQL faceting request counts by `X-Acme-Client`, and by `X-Acme-Tenant` for `platform` traffic — using `capture()` over the access-log `message` (text format, not JSON attributes). Apply via the `observability/dashboard` module (this module is the single owner of uid `abc1234` — do not re-add `vllm.json` to `observability/grafana-dashboards`).

11. **Commit (via /commit skill), push, open a DRAFT PR** against `master`, unsigned, no Test Plan section. Self-review, address findings.

12. **Record residual limitations in the PR/docs (AC):** this is attribution not authorization (shared API key, header is self-asserted); any bypass reaching an engine `Service` directly rather than the router is still open (router-only injection). Token-per-caller accounting and JWT `RequestAuthentication` are out of scope (separate tickets).

Acceptance criteria deliberately deferred to owners, not code: the actual per-app header values other teams send (platform/MOBILE/WEBAPP wiring lives in their repos) — this ticket only defines and enforces the contract.

### Risks & assumptions
- **Injection mechanism (step 1) is the make-or-break unknown.** The ticket asserts a pod *annotation* injects the sidecar; under the default object-selector webhook in an unlabeled namespace that usually requires a pod *label*. If neither the chart nor a targeted patch can label the router pod without labeling the namespace, the in-cluster AC can't be met without risking sidecars on the GPU engines. Verifying live is step 1 for this reason.
- **Unscoped router DENY would break the router** by blocking kubelet probes and the NR `/metrics` scrape — mitigated by `/v1*` path-scoping (step 7); must confirm the probe/scrape paths aren't under `/v1`.
- **Protocol sniffing:** if the router Service port isn't named/`appProtocol`'d as HTTP, Istio treats inbound as TCP and the L7 header rule silently never evaluates (policy becomes a no-op). Verified in step 6.
- **`enableAutoMtls: false`** in `istiod.yaml`: injecting a router sidecar into a mesh with autoMtls off should leave router→engine plaintext intact, but must be confirmed empirically (step 6) — a wrong assumption here silently drops all serving traffic.
- **Access-log format edit is cluster-wide.** Assumed safe because nothing parses the line (ticket-confirmed); if any unlisted consumer (an automated log parser outside `observability/`) depends on the exact line shape, the appended fields could surprise it.
- **Enforcement before callers migrate** would 403 live platform traffic (~180/day). The plan gates the external DENY (step 8) on access-log evidence that the platform already sends the header; if that evidence isn't there yet, step 8 waits on the platform team.