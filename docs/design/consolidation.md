# Consolidation: the kernel as a multi-agent OS

Status: proposal (2026-08-12). Staged; each stage stops for review.

## Principle

**Everything is an agent except the smallest set of mechanisms that make an
agent's output trustworthy.** The kernel is a thin, verifiable boundary *around*
untrusted intelligence, never a manager *of* it — the microkernel idea applied to
intelligence: a minimal privileged core, everything smart in userspace.

Only four mechanisms are irreducible, because an agent cannot provide them about
itself:

1. **Disinterested verification** (measure + gate) — the graded thing can't
   produce its own grade.
2. **Outside-enforced isolation** (credentials, scope) — you can't ask the
   guarded to guard itself; the agent is the untrusted party.
3. **Crash-proof liveness** (the wake guarantee) — an agent can't guarantee its
   own progress if it hangs or dies.
4. **Reproducible audit** — a fixed, replayable procedure is what makes an
   autonomous PR mergeable at all.

Everything else is an agent. The compass:

- **Agents by default, mechanism by exception.** If you can't name the guarantee,
  it isn't kernel.
- **Minimize the trusted base.** The kernel is the TCB — every line is attack
  surface and audit burden. Small enough to hold in your head.
- **Mechanism, not policy.** The kernel only says *no*; it never says *do this
  next*. Decisions are agentic.
- **Guarantees, not guardrails.** Don't make agents *correct*; make their output
  *verifiable, contained, recoverable*.

The bound that keeps it honest: as agentic as possible *while every autonomous
action stays verifiable, contained, and recoverable*. The one place a mechanism
must exist is exactly where you otherwise couldn't verify, contain, or recover
what an agent did.

## Why

The system grew by accretion. Each recent pain — session budgets, outages,
review length, noise floors — was fixed by adding a module or a flag. Every fix
was defensible; the sum is over-fragmented. Reading it now, a lot of the weight
is bespoke Python re-implementing what an agent harness already does: the loop,
tools, context management, resume, structured output, skills, memory.

The good news is the hard part is already right. The backend seam exists
(`architecture.md`, "The backend seam"): `Harness` takes a brief + workspace and
returns a `SessionResult`; `brief.build` owns context policy, shared by all
backends. So this is not a rewrite. It is finishing a separation the design
already started.

## The two layers

**Kernel — the multi-agent OS.** The deterministic core that earns trust: it
must be ungameable and reproducible. It exposes a small, stable surface and
nothing more:

- **run-a-role** — dispatch one role session to a backend (the `Harness` seam)
- **measure** — re-run the contract eval on the candidate tree; the number is
  never the agent's claim
- **gate** — apply an explicit policy (floor, scope-clean, no-regression) to a
  measurement or a verdict
- **act** — mechanical I/O: open a PR, post a comment, update the ledger
- **isolate** — per-role key and scope; scrubbed session env
- **persist** — run state, notebook, memory on the shared filesystem

**Roles — the apps.** Everything that thinks. A role is not a module; it is
data + skills:

- a **RoleSpec** — instructions, skill set, allowed tools, key, scope, memory
  scope, output schema
- a **result-policy** — what the kernel does with the role's output (author:
  measure+floor+PR; reviewer: post findings, blocking gates merge; steward:
  validate+PR). These are trust-critical *kernel* code and form a small, reused
  set — see the invariant below.

## The design invariant

> Adding a new role, backend, or benchmark requires **zero kernel changes** —
> as long as it reuses an existing result-policy.

Most new roles do: another judge reuses "post findings, blocking gates merge";
another climb-like role reuses "measure + floor + PR". A role that needs a
genuinely *new* result-policy is the honest exception — a result-policy is
trust-critical kernel code (it measures, gates, acts), so a new one is a new
kernel primitive, added deliberately and rarely. The health check still bites:
if a *routine* new role, backend, or benchmark forces a kernel edit, an app has
leaked into the OS — which is exactly how the accretion happened.

## The kernel never judges

"Interpret → act" hides two kinds of interpretation. Only one is the kernel's.

- *Measurement* ("did the number move, is the diff in scope") is arithmetic on a
  re-run eval. Deterministic — the trust anchor.
- *Judgment* ("is this sound, interesting, overfit, what next") is agentic. The
  kernel never does it. When judgment is needed it **calls a role** — that is
  what the reviewer and verifier are — and applies a mechanical policy to the
  role's structured verdict.

So: measure (kernel) + judge (invoked role) + gate (policy) + execute (plumbing)
+ human merge. When the author must interpret mid-run — experiment done, now
what — the kernel doesn't; it wakes the session. Every time real judgment is
required, it is a role, not the kernel.

## Syscalls: sync, yield, and no interrupt

Agents don't touch the trust-critical machinery directly — they call syscalls,
and the kernel mediates only where a guarantee depends on it. `act` is every
outward effect the kernel performs for a role: open a bot PR, post a comment,
update the ledger, and **launch an experiment**. So agents already draft PRs
(the kernel opens them; humans **merge**) and already launch experiments (the
kernel submits them, with the paired wake job so no run is stranded). A role can
also **fan out subagents** inside its session; they inherit its RoleSpec (tools,
scope, key), and their spend counts against its budget.

**Our OS is single-threaded, cooperative, and non-preemptive.** The tick is the
scheduler: it runs each lane to completion and exits, and run state on the shared
filesystem is the context switch. There is one blocking syscall —
launch-experiment, which runs for hours — and it is a **yield**, not a preemptive
block: the session ends, the kernel records the continuation (what it waits for,
plus resume state), and a later tick wakes it. The wake cycle is coroutine
yield/resume, which is why no agent is ever a long-lived process.

**Nothing is interrupted mid-flight.** There are no signals into a running
session. You steer at yield boundaries — a wake prompt can supersede a
task-level instruction — or you cancel the job (the coarse `kill`). Between
yields the agent honors its standing brief. A single-threaded OS that can't be
interrupted is a real constraint; it is also what makes every step recoverable
from a file on disk.

## What is kernel, what is app

| Now | Fate |
| --- | --- |
| `climb`, `steward`, `followup`, `review_cli`, `verifier_cli` | Five copies of "prep → run a role → act on the result." Collapse into **one role-runner + a RoleSpec + a result-policy** each. The measure/gate/PR halves stay kernel; the brief/skills halves become app config. |
| `review`, `verifier` (rendering, verdict/blocking machinery) | Move onto the agent-session path; shared finding-rendering becomes a skill. |
| `harness`, `llm`, `brief` | The seam. Keep. Add adapters. |
| `orchestrator`, `contract`, `github`, `compute`, `runstate`, `disk`, `limits`, `intake` | Kernel. Barely moves — the point. |
| `tick` | Kernel, but bloated from the same accretion. Its own diet, later. |

Rough size: ~2.5–2.8k of ~8.7k LOC is role-layer that collapses toward ~1k of
plumbing plus skill/instruction files. The kernel (~5k) is intentionally
untouched — you do not refactor the part that earns trust.

## Backends (the seam)

Any backend must meet a capability contract, or its adapter synthesizes the
gap:

- **resume** (wake a session with its working context)
- **structured output** (schema-constrained verdict for judge roles)
- **tool restriction** (read-only toolset for the reviewer boundary)
- **cost / turn accounting**

Lineup: **Claude Code** (primary), **Codex** (second, confirmed), **Hermes
Agent** (Nous, MIT, full CLI harness, model-agnostic, Singularity/apptainer
backend, native skills + learning loop) as the open-source candidate. Bench
honestly — same brief, same task pool, diff the outcomes — never rewrite around
one. A self-improving backend (Hermes) must be pinned for controlled roles so
runs stay comparable.

## Reviewer and verifier are this consolidation

Today they sit on the one-shot `Completer` seam: read a diff, emit findings, no
tools, no checkout. The planned upgrade — make them agentic — *is* moving them
onto the agent-session path with a read-only checkout of the PR head (Read,
Grep, Glob; no Bash, no Write; egress limited to the model API plus the curated
retriever). The two seams collapse to one substrate; roles differ only by
RoleSpec.

Two tracked upgrades fall out for free: **inline verifier findings** (same
render path as the reviewer) and **the retriever seam** (a tool in the reviewer's
RoleSpec, not a bolted-on API context). The security boundary becomes
`allowed_tools` — kernel-enforced by the toolset, not asked for in a prompt.
See `reviewer-infra.md`.

## Learning

Skills, memory, and the notebook are how roles learn — the native substrate, not
a bolt-on. Two levers:

- **Capture.** The notebook carries the agent's own reports forward; maintainer
  PR feedback and the merge/close decision currently do not feed it. Add a step
  that distills a closed/merged PR's review thread + outcome into one short,
  per-target lesson. The strongest signal is the *delta* — what a maintainer
  changed before merging — not the prose comment.
- **Affordance.** Prefer shaping the environment over instructing. Give `models/`
  a real init/LR seam and µP-type findings land there, instead of being smuggled
  into `forward` — no rule required. Affordances teach more reliably than
  instructions, and a frozen model only "learns" in-context anyway.

## Staged plan

1. **Reviewer/verifier onto the agent-session path, Codex as the proving
   backend.** Lowest-stakes place to validate a second backend (a read-only judge
   is far safer to run on an alternate stack than a file-editing author), and it
   ships the agentic-review upgrade at the same time.
2. **Introduce `RoleSpec` + the role-runner**; move author, steward, follow-up
   onto it; fold prompt content into skills; wire the lesson-capture step.
3. **Diet `tick`.**

## Costs and non-goals

- An agentic review is a session — slower and pricier than one completion. Keep a
  no-tools RoleSpec as a cheap fast-pass.
- Keep the trust story *legible*: a reviewer must still be able to point at
  "measured here, gated there." Do not trade auditability for brevity.
- Going lighter deepens harness coupling; the seam and honest backend benching
  are the hedge.
- Not in scope: touching the measure/gate/isolate core, or the scheduler's
  fail-safety (`architecture.md`, "Wake delivery and fail-safety").
