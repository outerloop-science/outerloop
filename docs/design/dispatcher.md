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
`eval_minutes` hint on the benchmark entry — per-benchmark because eval
cost is a property of the benchmark, not the run budget, so it is validated
with its own code-side ceiling rather than riding the Budgets clamp table.
Under the in-job threshold the eval runs in-job as today; above it the
orchestrator dispatches. The threshold is sized to the JOB'S runway, not to
EVAL_TIMEOUT_S: the climb job carries ~CLIMB_OVERHEAD_MINUTES beyond the
session for clone + two evals + publish, so in-job evals must fit a few
minutes each — EVAL_TIMEOUT_S stays what it is today, the in-job hard
kill, not the dispatch line. The
orchestrator chooses per eval site, not per repo.

**Interop.** The orchestrator keeps a single seam: `Evaluator` grows a
dispatched implementation that *stages* instead of blocking. `climb_once`'s
transaction (session -> measure -> gates -> panel -> publish) becomes
resumable at its measure points: reaching one submits the eval job, persists
the stage in the run record, and returns; the wake re-enters the transaction
at the recorded stage with the job's result file. The states, records,
deadlines, lease machinery, sweep, and `MAX_WAKE_ATTEMPTS` stuck-run
ending all exist and are tested; what is a STUB today is the production
`WakeDispatcher` — the tick's sweep runs dry and logs WOULD-WAKE. Phase 1
fills that stub (a wake re-invokes the parked transaction) and wires the
climb's stages into the rest — it does not build a second event plane. The panel stays in-job: judge sessions are
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
sha — where "the measured sha" is created if it does not exist yet: the
candidate measure happens on a dirty workspace before anything is committed,
so dispatch snapshots the tree first: add -A / write-tree / commit-tree
parented on the base, run against a TEMPORARY index (`GIT_INDEX_FILE`), so
the working index is never touched — a session that resumes later finds its
staged state intact. (The panel's current snapshot uses the working index
plus a reset; it should adopt the temporary index too.) The fingerprint for
the existing drift check is the snapshot's TREE hash — `write-tree` is
deterministic for identical trees, which is what climb.py already compares —
while the commit wrapping it exists only so a worktree can materialize the
tree. Suite-gate baseline jobs use real commits as today. The job runs under the SAME containment contract as
today's in-job evaluator —
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
dies -> the `eval-error` outcome at wake (ending `aborted`, exactly as the
in-job eval failure maps today); a LOST wake -> the sweep's primary backup,
which wakes any run whose job reads terminal after the grace period —
plus GONE past the deadline (vanished) and PENDING past the deadline
(cancel, then wake as unschedulable); a still-RUNNING job is deliberately
left alone, bounded by its own walltime; wakes that fire without producing
progress -> `stuck` at MAX_WAKE_ATTEMPTS. A moved base during the wait -> the existing
merged-tree re-gate covers it, since re-entry re-checks freshness like any
other resumption. Worktree cleanup has a named owner at every exit: the
wake's own finally (primary, as in-job measures do today), the sweep's
run-ending path when a run dies waiting (best-effort rmtree of the run
dir's measure worktrees, same never-silent discipline), and ended-run
retention as the floor.

Suite gates and panel re-measures ride the same staging: a suite
re-measure over N siblings becomes 2N parallel eval jobs — baseline and
candidate per sibling, paired seed via `extra_env`, exactly the comparison
the gate computes today — instead of 2N sequential in-job runs. The first
place dispatch makes something better, not just possible.

## Phase 2 — the author experiment syscall

An `act` syscall exposed to the session as `run_experiment` — the session
INVOKES it; the validation, snapshot, submission, and session end are
orchestrator code, per the substrate constitution (the agent directs, the
kernel acts). It validates the request against the contract's budgets,
submits, and ends the session with a resume marker. The
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

## Appendix: record and channel formats

No message bus, no database, no RPC: durable files on the shared filesystem
carry state, Slurm itself carries events, GitHub markers carry the
cross-trust-boundary record. Everything below is one of those three.

**The run record** (`state/runs/<run_id>/state.json`, atomic tmp+replace,
single writer via lease):

| field | role |
|---|---|
| `run_id` `target` `benchmark` `agent_id` `task_title` `issue_number` `pr_url` | identity/topology |
| `climb_job_id` | the transaction's own job — lets the sweep tell KILLED from crashed; must be re-stamped by any path re-entering `implementing` from a new job |
| `experiment_job_id` `wake_job_id` `followup_job_id` | Slurm handles for the dispatched work, its afterany wake, and review servicing |
| `resume_session_id` | the harness session a wake reconstructs — the entire "pause" state for a session's mind |
| `state` `deadline` `terminal_seen` `wake_attempts` | wake bookkeeping; a waiting record REQUIRES a deadline |
| `stage` (phase 1) | which measure point the transaction parked at; the wake re-enters there |
| `last_comment_id` `last_review_id` `last_review_comment_id` | three cursors because GitHub's three comment collections have independent id sequences |
| `ending` `ending_note` `created` `updated` | terminal record |

**Job -> orchestrator**: the LAST single-line JSON object on stdout carrying
the metric — `{"<metric>": v}` or `{"metric": name, "value": v}`; no regex
fallback (a fuzzy match that reads the wrong number is worse than a clean
failure). Dispatched jobs write the same JSON to a result file in the run
directory — stdout dies with the job; the file is what the wake reads.
Extra keys (margins, per-condition tables) ride along; the parser takes
only its metric.

**Orchestrator -> job**: argv (the contract command), a worktree of the
snapshot sha as cwd, allowlisted env + the call site's `extra_env` (paired
seeds). One-way by construction — the jail keeps records and credentials
out of reach.

**Orchestrator <-> session**: briefs are files (the query is a pointer,
never argv); wake prompts carry results data-fenced with the standing
DATA-not-instructions framing; sessions return structured output against
role schemas; cost/turns come from harness session metadata.

**Coordination files**: pending marker `state/pending/<target>.json`
(`{benchmark, job_id, submitted_at}`; liveness-first, TTL breaks ties),
lease (`{holder, holder_job_id, acquired}`, TTL-reaped), heartbeat.

**GitHub markers** (cross-host, survive the cluster; authenticated by
AUTHOR — only bot-posted markers count): claim / claim-released /
outage-release, the advisory marker + per-reviewer round stamps, and the
review findings envelope (`{repo, number, kind: findings|skip-stub|
skip-clean, data, detail, reviewed_by}`), re-validated by the posting job.

## Acceptance

Phase 1 lands when: a steward eval that times out in-job today completes as
a dispatched job with the run resuming correctly across the wake; a killed
eval job ends its run as `eval-error` with a report; and the waiting-sweep's
backup wake fires when the afterany job is scancel'd. The yolo ruler-v2
`BENCH_FULL` grid is the first real workload.
