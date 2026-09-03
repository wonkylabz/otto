**TLDR** — All six environments were swept for the expiring `*.example-internal.test` certificate; four renew automatically, one needs a manual rotation before 2026-09-04, and one turned out not to use the cert at all. No production impact expected.

## What was checked

Every cluster in the fleet was queried for Secrets of type `kubernetes.io/tls` whose certificate chain terminates in the `*.example-internal.test` SAN, then cross-referenced against the issuer recorded on each Ingress. The sweep covered the CPU node groups and the GPU pool in each region, including the cluster that was added last quarter and is not yet in the standing inventory document.

| Environment | Region | Cert source | Expires | Action |
|---|---|---|---|---|
| dev | ap-southeast-2 | cert-manager (ACME) | 2026-11-02 | none — auto-renews at 30 days |
| dev-a | us-east-1 | cert-manager (ACME) | 2026-11-02 | none — auto-renews at 30 days |
| stg | ap-southeast-2 | cert-manager (ACME) | 2026-10-28 | none — auto-renews at 30 days |
| ent-b | eu-west-1 | manual import | **2026-09-04** | **rotate — see below** |
| prod-b | eu-west-1 | cert-manager (ACME) | 2026-11-14 | none — auto-renews at 30 days |
| prod-a | us-east-1 | n/a | n/a | none — terminates TLS at the load balancer, no in-cluster cert |

## The one that needs a human

`ent-b` is the only environment whose certificate was imported by hand rather than issued by cert-manager. The Secret carries no `cert-manager.io/issuer-name` annotation and there is no matching Certificate resource, so nothing will renew it — it will simply expire at 00:41 UTC on 2026-09-04 and the gateway will begin serving an expired chain to every client that resolves to that endpoint.

The reason it is manual appears to be historical: the environment was stood up before the ACME solver had a delegated zone in that account, so the operator at the time imported a certificate issued out-of-band and left a note in the runbook saying it would be migrated "once the zone lands". The zone landed in March and the migration was never done.

Two options, in order of preference:

1. **Migrate it to cert-manager**, matching every other environment. The delegated zone now exists in the same account, so the DNS-01 solver has everything it needs. This is a one-time change and the problem never recurs. It requires a short window where the Ingress is reconciled, which in practice means a few seconds of connection churn on new connections only.
2. **Re-import a fresh certificate manually**, which buys another year and leaves the same trap set for whoever is on call next August. Faster to execute today, worse in every other respect.

Recommendation is option 1, scheduled in the next maintenance window rather than done ad hoc, because the reconcile touches the shared gateway rather than a single service.

## Things worth noting that were not asked about

- The `prod-a` result is not a gap. That environment terminates TLS at the load balancer with a certificate managed by the cloud provider, so the absence of an in-cluster Secret is the expected shape, not a missing one. It is called out here only because a naive sweep of in-cluster Secrets reports it as "no certificate found", which reads alarmingly in a table.
- Three of the auto-renewing environments share a single ACME account. That is fine operationally, but it does mean the rate limit is shared across them, so a mass re-issue triggered by, say, a bulk Secret deletion would be throttled fleet-wide rather than per-environment.
- The standing inventory document lists five environments. There are six. `dev-a` is missing from it, which is the same omission that made last quarter's audit come out one row short.

## Verification

Each row in the table above was read directly from the live cluster rather than from configuration in the repository, because the repository describes intent and the question asked was about actual expiry. The expiry dates come from decoding the leaf certificate in each Secret; the issuer attribution comes from the annotations on the Secret and the presence or absence of a corresponding Certificate resource. Where the two disagreed — they did not, in any environment — the certificate itself would have been treated as authoritative.

**What you need to do** — Decide between migrating `ent-b` to cert-manager (recommended) or re-importing by hand, and schedule it before 2026-09-04.
