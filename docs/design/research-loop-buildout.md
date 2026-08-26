# Building out the research loop: the author-directed syscall

**Status: plan (2026-08-22; re-based 2026-08-23), reviewable before code.** The
concrete build-out of the north-star in `research-loop.md`. The 2026-08-23
crystallization re-based this plan: the earlier phases built **orchestrator-
driven** depth (a kernel loop running k measured passes, selecting the best,
reworking the publish path) — the wrong model. Depth is the **author's**
experimentation budget on one author-triggered primitive: **dispatch → sleep →
wake-with-result.** This doc sequences building that primitive. Pairs with
`consolidation.md` (kernel-as-OS), `agent-substrate.md` (experiment submission
as an `act` syscall — this plan builds exactly that), `dispatcher.md`
(fire-and-wake), and `orchestrator-verify.md` (the gate).

## The thesis in one paragraph

The author lives in a sandbox; real experiments run outside it. The one
primitive the kernel owes the author is: *dispatch a job, sleep, wake with the
result back in the sandbox.* Everything the earlier plan phased out separately
— depth iteration, panel revision, parallel candidates — is the author using
that primitive under its own judgment: run experiments and sleep on them,
submit for review and sleep on it, read comments, run more experiments, revise,
resubmit. The kernel keeps exactly four things: the **sandbox**, the
**syscalls**, a **generous meter** (per-action `k` counts or minutes — a soft
ceiling, not a tight cap), and the **gate** (the untouchable ruler, run inside
the submit payload, plus the suite/no-regression defense). What to measure
between sleeps, what signal to trust, what to return — all author judgment; the
kernel prescribes none of it.

## What already exists (and is kept)

- **Park/wake substrate, orchestrator-triggered** — `ClimbParked`, the
  dispatcher, `afterany` wake jobs, `resume_run`. Proven live. The build hands
  the *trigger* to the author; the plumbing underneath is this.
- **Session resume** — `climb_once`'s resume-entry (`resume_session_id` +
  `improve_prompt`, the #129 primitives) is the wake-the-same-session
  mechanism; `supports_resume` gates backends that cannot (hermes).
- **The composition seam** (#128) — the decide-next policy extraction. Retained
  as internal structure; no further orchestrator decision policies get built on
  it (the author decides next moves now).
- **`Benchmark.depth_k`** (#129) — re-purposed verbatim: it is the
  experiment-launch budget (`1` = today's one-shot; the `[1, 8]` bound can
  widen when Phase A lands, and the submit + sleep counts are its sibling
  knobs).
- **The gate + panel** — unchanged as measurement machinery; re-seated as the
  inside of the submit payload rather than stages the run passes through.

## The syscall surface (reviewed with Mengye, 2026-08-23)

Three author-facing syscalls; the kernel binds them to Slurm. The agent
controls the actions; the kernel performs them.

| Syscall | Blocking? | Metered? | Semantics |
| --- | --- | --- | --- |
| `launch(cmd, resources) → handle` | no | 1 experiment count | run agent-named work outside the sandbox, eval-grade containment; author keeps working |
| `submit() → handle` | no | 1 submit count | the review payload: kernel seals the snapshot, runs the gate (its own private seed) + panel as jobs |
| `sleep(handles) → results` | hibernates | 1 sleep count | park the session; wake the *same session* when the named jobs finish, results delivered as the call's return |

Design rulings:

- **Split is the primitive; fusion is sugar.** A blocking `run(cmd)` may exist
  as `sleep([launch(cmd)])`, but launch/sleep stay separate so *not* sleeping
  is always an option — launch several, keep coding, sleep once.
- **Submits batch too.** Multiple candidates can be in review at once; sleep
  until all verdicts are back.
- **Independent generous counts per kind: launches, submits, sleeps** (or a
  minute budget). Launches meter external compute; the sleep count meters wake
  cycles — without it an author could clock-refresh forever and never launch —
  and batching is still rewarded (ten launches under one sleep burn one sleep).
  Counts are visible in the prompt; the author knows a submit burns its count;
  running low is a **warning in the prompt, never an enforced reserve**.
  Exhaustion refuses further calls of that kind; the session always gets to
  conclude honestly.
- **Results are data.** Exit code + bounded stdout/stderr + author-declared
  artifact paths copied back into the sandbox; all data-fenced (job output is
  untrusted text, never instructions).
- **The session clock is visible; sleep refreshes it.** The hosting job has a
  walltime; each harness round reminds the author of the time remaining before
  a forced sleep. `sleep([])` — nothing to wait on — is a legitimate
  checkpoint-and-reschedule: wake in a fresh job with a fresh clock (it burns a
  sleep count like any other — the bound on living forever). This replaces the
  self-deadline kill (attempt.py's walltime alarm) with an author-managed
  handoff.

## Phase A — launch/sleep: suspend-on-syscall, wake-with-result

**The crux, and the only genuinely new capability**: the session hibernates on
`sleep` (no GPU, no held job — the same park the orchestrator does today) and
the substrate later resumes the *same session* with the jobs' results.

Build notes, not spec (spec lands with the PR):

- **Trigger inversion only.** Reuse `ClimbParked`-style park records, the
  dispatcher, and `afterany` wakes; what changes is *who* raises the park (a
  syscall inside the session, surfaced by the harness) and *what* the wake
  delivers (job output into the resumed session, not a gate decision).
- **The harness seam.** The wake resumes via `resume_session_id` with the
  results (data-fenced) as the continuation. Per-backend: claude and codex
  both resume; hermes (`supports_resume=False`) cannot host this.
- **Containment unchanged.** Launched jobs run agent-directed code with
  eval-grade containment (the `SubprocessEvaluator` posture).

**Acceptance.** An author session launches a command, hibernates through a
real Slurm job, resumes with the output, and continues — observed end-to-end;
exhaustion surfaces as a refused launch, not a killed session; an author that
never launches is byte-identical to today.

## Phase B — submit as a payload — LANDED

Surfaced to the author as the `submit` verb on the role CLI (`role-cli.md`,
Phase 1 — the tool from #133 grows the verb; the JSON stays internal ABI).
`submit` is a launch whose job is the **gate** (paired baseline/candidate +
suite on the sealed snapshot, kernel-private seed) and the **panel**; the wake
returns verdict + comments, and the author decides — revise, run more
experiments, resubmit, or conclude. The orchestrator-driven panel-revision
loop (`panel_revisions`, policy-driven re-entry) is retired in favor of this;
the finish stays agent-driven (`research-loop.md`, "the finish is agent-driven
too").

**Acceptance.** An author submits, sleeps, wakes with a blocking finding, runs
a *further experiment*, revises, resubmits, and lands a PR — the interleaving
the old stage model could not express. A clean first submit still opens a PR
with no extra machinery.

## What falls out for free

- **Parallel dispatch** is no longer a phase: launch several (or submit
  several) before one sleep — a portfolio within an attempt is the author's
  choice on the same syscalls. If coordination patterns recur, lift helpers
  later — earned, not designed up front.
- **Cross-session depth** (the old Phase 2b) *is* Phase A — every sleep is
  cross-session by construction.
- **A flow DSL** stays rejected: pushing the flow into the author dissolved the
  static role-graph a language would describe. The syscall is the abstraction;
  extension = new payloads, new benchmarks, new backends — zero kernel change.
  (A thin declarative layer might someday earn its keep at the *outer* loop —
  fan out M attempts / reduce to portfolio — only after those ops exist
  imperatively.)

## The governor, unchanged in principle

More launches and submits = more chances to game the benchmark. The defenses
do not move:
the author never gets the frozen test/seed to iterate against (ruler-fishing);
the gate re-measures on sealed snapshots inside submit; the suite /
no-regression gate must be in force before budgets get generous (the metric
taxonomy remains the companion doc). Every unit of spend passes the same bar.

## Dependency: the dispatcher must go live

Unchanged from the original plan: Phase A's real form stands on the dispatcher
running for real (a benchmark whose jobs genuinely park). The dispatcher is
proven but dark; lighting it up on a long-eval benchmark is the entry gate to
observing Phase A end-to-end on Slurm. (Phase A can be *built* and tested
against fakes before that.)

## Sequencing summary

1. **Phase A — the syscall** (suspend-on-tool-call, wake-with-result), built on
   the existing park/wake plumbing + #129 resume primitives; tested on fakes.
2. **Light up the dispatcher** on a real long-eval benchmark; observe Phase A
   end-to-end.
3. **Phase B — submit-for-review as a payload** — LANDED (the orchestrator
   panel-revision loop is retired); suite gate + metric taxonomy still to come.
4. Parallel/coordination helpers only if recurring author patterns earn them.
