# External-facing evolution

Nothing here changes current work. These are the seams to keep clean so the lab
tool can become an external artifact, and the order to pull them.

## Identity: machine user → GitHub App

Right for one org today: bot account + fine-grained PAT. External consumers
install a **GitHub App**: per-installation tokens, permissions declared once,
no seat, revocable per org. Seam kept clean now: all auth in the `github`
module flows through a token-provider interface — an App installation-token
provider replaces one constructor call, not the callers.

## Knowledge: notebook repo → walled research store

The markdown notebook is the single-tenant backend of a storage interface.
External consumers need per-tenant walls (their runs/lessons/plans are their
confidential research), retention, and query — a database with tenant
isolation. Seam: `report` writes through a storage interface; notebook-repo is
backend #1. Cross-tenant lesson sharing is a product decision — default walled.

## Experimentation: Torch sbatch → pluggable feedback backends

The loop's invariant core is **hypothesis → experiment → verifiable feedback**.
Torch is backend #1 behind the `compute` interface. The external shape:

- **Consumer-side runners**: the consumer installs a runner in their own infra
  (their CI, cluster, or robot) that executes the contract's benchmark
  commands and reports results. Access = they grant a runner, not credentials
  to their systems; and external code **never executes on lab or university
  hardware** — the runner is also the sandboxing answer.
- **Verifiable reward**: benchmark commands are deterministic and re-runnable,
  so a metric claimed in a PR is re-verified by the target's own CI at merge —
  a receipt anyone with the repo can regenerate, not an assertion by the
  agent. Attestation/signing layers on later if trust demands it.

## Multi-tenancy hardening

- Per-tenant credentials, budgets, ledgers — and strict **session isolation**:
  one tenant's code or issue text never enters another tenant's LLM context
  (context contamination is a data-leak channel specific to LLM infra).
- External repos are adversarial by default: the org-member gate on task
  sources generalizes to maintainer-of-that-installation per tenant.
- Lab code on lab infra remains the special case; external code runs only
  consumer-side.

## Sequencing

Post-1.0, after the pilot proves the loop: App identity → storage interface
extraction → experiment-backend interface extraction. Each is an interface
swap, not a redesign, provided the seams above stay clean.
