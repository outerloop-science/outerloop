# The experiment dispatcher

**Status: proposal (2026-08-17).** How experiments and evals become their own
jobs instead of running inside a role session's walltime. `architecture.md`
owns the wake fail-safety layers this builds on; `agent-substrate.md` set the
constitution (experiment submission is an `act` syscall, not an agent tool);
`orchestrator-verify.md` owns the panel, which stays in-job. This note
settles the three questions queued at the panel-flip review: when the
dispatcher kicks in, how it interops with the orchestrator, and what the
session clock and the credit boundary do while a job runs.

## Why now

The trigger criterion was always "the first ruler that wants more than the
in-job eval allows" — and it fired the weekend the panel flipped on:

- The yolo-jepa steward burned two 57-minute sessions against the 1800s
  in-job eval timeout trying to build a richer ruler (4 regularizer forms x
  2 encoders x 63 runs ~ 30+ min). The ruler had to be redesigned around
  the timeout (pinned baselines read at eval time, full grids exiled to a
  `BENCH_FULL` mode with nowhere to run).
- Robustness axes that decide real scientific questions (the form ordering
  FLIPS at high noise) cannot run on the auto path at all.
- Every eval so far is CPU-seconds. The first GPU benchmark makes
  eval-inside-a-CPU-job not slow but impossible.

The panel-era time model (contract-clamped job + panel allowance, clamped at
the partition cap) is pilot-scale by design. The dispatcher is the piece
that was always going to replace its central assumption: that measuring is
cheap enough to happen inside the climb job.

## The design in one paragraph

Dispatch is a **syscall on the existing compute seam** — `submit(JobSpec)` +
an `afterany` wake job, the same two primitives the tick chain already uses,
zero new kernel concepts. When the orchestrator (or, in phase 2, an author
session through a tool) needs an experiment run, it submits the job, writes
the run record into `waiting` with `experiment_job_id` and a deadline, and
**ends the session**. Results arrive by wake: the afterany job is the
primary delivery, the tick's waiting-sweep is the backup (both exist; the
sweep runs dry today). Nothing polls, and no clock runs while a job queues.

## The three settled questions

**When it kicks in.** Phase 1 ships now — the eval-timeout class is live and
recurring. The in-job evaluator remains the default: an eval that finishes
in minutes on the job's own allocation is not worth a queue round-trip.
The switch is a per-benchmark contract knob (schema change, phase 1): an
`eval_minutes` hint on the benchmark entry, clamped like every budget knob.
Under the in-job threshold (code-side constant, of order EVAL_TIMEOUT_S) the
eval runs in-job as today; above it the orchestrator dispatches. The
orchestrator chooses per eval site, not per repo.

**Interop.** The orchestrator keeps a single seam: `Evaluator` grows a
dispatched implementation that *stages* instead of blocking. `climb_once`'s
transaction (session -> measure -> gates -> panel -> publish) becomes
resumable at its measure points: reaching one submits the eval job, persists
the stage in the run record, and returns; the wake re-enters the transaction
at the recorded stage with the job's result file. The states, records,
deadlines, lease machinery, and `MAX_WAKE_ATTEMPTS` stuck-run ending all
exist — phase 1 is wiring the climb's stages into them, not building a
second event plane. The panel stays in-job: judge sessions are
API-bound minutes, and moving them buys nothing.

**Clock pause, and the credit boundary.** There is no pause mechanism —
there is a state transition. The session ends at dispatch, so the session
budget prices *thinking*, never waiting, and nothing GPU-shaped ever idles
under a reasoning model. On credit: **dispatching never promotes an
experiment to official.** The official measure remains exactly what it is
today — the orchestrator re-running the contract's eval command on a
committed, sha-fingerprinted tree in a throwaway worktree. A dispatched
author experiment is exploratory whatever hardware it used; its artifacts
earn evidential weight only by being committed to the tree where the
verifier can read them. The dispatcher moves WHERE compute runs, never who
is believed.

## Phase 1 — eval as a job (climb + steward)

The submitted eval job: the contract command, on a worktree of the measured
sha, under the SAME containment contract as today's in-job evaluator —
apptainer `--containall --cleanenv` in the climb's single `--image` (session
and eval share it today; that stays), seeing only the worktree, a throwaway
HOME, the node-local uv cache bind, and the evaluator's environment
allowlist plus the call site's `extra_env` (paired seeds ride there). The command and tree
and jail are exactly today's evaluator inputs — the only change is the
allocation. No orchestrator credential enters the job: agent-written eval
commands run on a shared filesystem next to the PAT, so the jail is
load-bearing, not hygiene. It writes `{metric, value}` JSON to the run
directory; the afterany wake re-enters the climb stage that was parked. Failure modes map
to the existing layers, exactly as the sweep implements them: a job that
dies -> `eval-error` at wake; a LOST wake -> the sweep's primary backup,
which wakes any run whose job reads terminal after the grace period —
plus GONE past the deadline (vanished) and PENDING past the deadline
(cancel, then wake as unschedulable); a still-RUNNING job is deliberately
left alone, bounded by its own walltime; wakes that fire without producing
progress -> `stuck` at MAX_WAKE_ATTEMPTS. A moved base during the wait -> the existing
merged-tree re-gate covers it, since re-entry re-checks freshness like any
other resumption.

Suite gates and panel re-measures ride the same staging: a suite
re-measure over N siblings becomes 2N parallel eval jobs — baseline and
candidate per sibling, paired seed via `extra_env`, exactly the comparison
the gate computes today — instead of 2N sequential in-job runs. The first
place dispatch makes something better, not just possible.

## Phase 2 — the author experiment syscall

A session tool (`run_experiment`) that validates the request against the
contract's budgets, submits, and ends the session with a resume marker. The
wake resumes THE SAME session (the panel's wake mechanics) with the result
file path. Budget enforcement lives at the syscall: `gpu_hours_per_run`
decrements at submit time from the job's ceiling, refunds unused time at
wake, refuses when exhausted — and a new `experiments_per_run` budget knob
(contract schema, clamped like every knob) caps the ROUNDS. That cap is
what bounds total thinking: the session budget is per segment, so a run
thinks for at most `session_minutes x (experiments_per_run + 1)` — a
cumulative thinking clock was considered and rejected, because it starves
the post-results analysis in late rounds, which is the highest-value
thinking in the run. On CPU-cheap benchmarks the round cap is the ONLY
effective limit (gpu-hours is zero there), and the cost driver is the
thinking segments, not the experiments. The author never touches Slurm and
never holds credentials; the syscall is orchestrator code. Resumed context
grows per round; long runs lean on the standing compaction story
(backend-native compaction + externalized findings).

## Phase 3 — scale interactions

GPU benchmarks and portfolio climbs both multiply eval load; both were
designed against this seam. Portfolio's N concurrent climbs become N
waiting records with independent wakes — the serialization question stays
in the picker, not the dispatcher. The tick's job-partition knobs
(`AUTORESEARCH_JOB_PARTITION`) already route work jobs; per-benchmark
partition/GPU allocation fields are a phase-3 contract-schema change
(`Benchmark` rejects unknown fields today, deliberately), validated and
clamped by operator ceilings like every other budget.

## Non-goals

Not a workflow engine: one job per dispatch, one wake per job, no DAGs. Not
a promotion path: no dispatched result feeds credit without the
orchestrator's own re-run. Not a scheduler: Slurm queues; the dispatcher
submits and hibernates.

## Acceptance

Phase 1 lands when: a steward eval that times out in-job today completes as
a dispatched job with the run resuming correctly across the wake; a killed
eval job ends its run as `eval-error` with a report; and the waiting-sweep's
backup wake fires when the afterany job is scancel'd. The yolo ruler-v2
`BENCH_FULL` grid is the first real workload.
