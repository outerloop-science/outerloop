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
`eval_minutes` hint on the benchmark entry — a HINT the orchestrator may
distrust: it governs only the FIRST eval of a benchmark; after that the
orchestrator's own measured durations decide, so a target inflating the
hint buys one dispatched eval, not a standing queue tax, and an operator
knob can disable dispatch per target outright — per-benchmark because eval
cost is a property of the benchmark, not the run budget, so it is validated
with its own code-side ceiling rather than riding the Budgets clamp table.
Under the in-job threshold the eval runs in-job as today; above it the
orchestrator dispatches. The threshold is sized to the JOB'S runway, not to
EVAL_TIMEOUT_S: the climb job carries ~CLIMB_OVERHEAD_MINUTES beyond the
session for clone + two evals + publish, so in-job evals must fit a few
minutes each — EVAL_TIMEOUT_S stays what it is today, the in-job hard
kill, not the dispatch line. The
orchestrator chooses per eval site, not per repo.

**GPU benchmarks.** A second per-benchmark contract field, `gpus` (default
0), sizes every dispatched job of that benchmark — gate measures and author
launches alike: the JobSpec requests `--gpus=N`, and the jail adds `--nv` so
the allocation is visible inside the container; nothing else about the
containment changes. GPU jobs need their own lane, so the deployment names
one — `AUTORESEARCH_GPU_PARTITION`, optionally `AUTORESEARCH_GPU_ACCOUNT`
(default: the CPU account) — and a benchmark with `gpus > 0` on a deployment
without a lane is REFUSED at launch (both attempt lanes), never queued into
evals that can never run. GPUs exist only on DISPATCHED jobs: the contract
rejects a GPU benchmark whose `eval_minutes` would keep its evals in-job
(the climb job has no allocation), and the steward lane refuses GPU
benchmarks outright for now — a stewardship validates its rewrite in-job.
The first target in this shape is the speedrun (`gpt-speedrun`: one H200
per eval, ~3.5h — hence the 300-minute ceiling).

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

### The resume seam (stage B)

`climb_once` today fuses three things: baseline (pre-session tree), the
SESSION (edits the workspace), and candidate (dirty post-session workspace),
then decides. The session is the one part that must NOT re-run on wake — its
edits are done — and it is also the only non-idempotent part. So the seam is
AFTER the session:

1. Session runs, ends. Its output is the dirty workspace.
2. The workspace is SNAPSHOTTED (`dispatch.snapshot_tree`, which retains the
   commit behind a unique random ref so independent snapshots of the same
   tree never share a lifecycle): `candidate_sha` + its `candidate_ref`. The
   base is `base_sha` (the clone's pre-session HEAD — a real branch commit, so
   it needs no ref and never leaks). Both shas are checkoutable by the
   dispatcher.
3. The MEASURE-AND-DECIDE phase — a pure function of `(base_sha,
   candidate_sha, contract, seed, suite_seed)` — dispatches its measures
   through the `Measurer`, and on `MeasurementPending` the run parks as
   `waiting`. It measures LAZILY, in the same order `climb_once` did: first
   baseline@base_sha + candidate@candidate_sha (one wake); only if that pair
   clears the improvement threshold AND the diff touched shared code does it
   dispatch the sibling pairs (a second wake). A non-improving candidate never
   burns a sibling eval — the extra wake is cheap CPU, a wasted GPU sibling
   eval is not.
4. The record persists exactly what a fresh process needs to re-enter step 3
   without the session: `base_sha`, `candidate_sha` + `candidate_ref`, `seed`,
   `suite_seed`, the benchmark, and a `stage` marking "measures dispatched".
   The `candidate_ref` is essential and cannot be reconstructed from
   `candidate_sha` (it is random by design): it is what a terminal wake hands
   `dispatch.drop_snapshot` to release the snapshot once the run finishes (PR
   opened or abandoned) — omit it and every parked run leaks a ref and its
   commit. The snapshot must OUTLIVE all park/wake cycles (each eval checks out
   `candidate_sha`), so the drop is deferred to the terminal state, never a
   mid-cycle wake. Both seeds are persisted for the same reason: `seed` and
   `suite_seed` are DRAWN (random), so a wake cannot reproduce them — an
   unpersisted seed would re-measure an unpaired baseline/candidate. In
   contrast `measured_paths` (which drives the scope check and the suite
   trigger) is NOT persisted: it is re-derived from the committed diff
   `base_sha..candidate_sha`, the same set the session's `git add -A` staged.
   Everything else (contract, ruler) is re-fetched.

On the wake, a fresh process loads the record, re-enters measure-and-decide:
the `DispatchedMeasurer` reads both cached results, `improved()` /
suite-gate / panel run (panel in-job, cheap), and the PR opens — or another
measure (a panel-revision re-measure) dispatches and it parks again. N
park/wake cycles, each cheap because prior measures are cached. The session
never re-runs because step 1's completion is durable: the candidate snapshot
IS the session's persisted output.

Sub-parts, each its own PR through the panel:
- **B.1 (merged):** `DispatchedMeasurer` — submit/park/resume over committed
  measures, cluster-authoritative liveness.
- **B.2a:** the resumable `measure_and_decide` — extract the post-session
  logic behind a re-enterable function over committed shas; pure, tested
  with a fake measurer.
- **B.2b:** wire `live_climb` — session -> snapshot -> measure_and_decide;
  on park write the `waiting` record (base_sha/candidate_sha/`candidate_ref`/
  seed/stage + `experiment_job_id` = the afterany set) and end. A terminal
  wake `drop_snapshot`s `candidate_ref` (the snapshot outlives every park/wake
  cycle, so the drop is deferred to run end, never mid-cycle). When a revision
  supersedes
  a candidate, the revision loop `scancel`s the superseded candidate's still-
  running eval jobs before dispatching the new ones — the measurer keys each
  measure by its full determinant (so the results never alias), but only the
  loop that owns the revision knows a prior candidate is now dead weight.
- **B.2c:** the wake-entry CLI + the production `WakeDispatcher` (fills the
  tick's WOULD-WAKE stub with a real dependency wake job). The existing
  wake-fail-safety layers (afterany primary, sweep backup, deadline floor)
  carry it unchanged.


The submitted eval job: the contract command, on a worktree of the measured
sha — where "the measured sha" is created if it does not exist yet: the
candidate measure happens on a dirty workspace before anything is committed,
so dispatch snapshots the tree first: a TEMPORARY index (`GIT_INDEX_FILE`)
seeded with `read-tree` from the base commit — so tracked-but-ignored files
behave exactly as they do in attempt.py's populated-index fingerprint, which a
fresh empty index would silently drop — then add -A / write-tree /
commit-tree parented on the base; the working index is never touched — a session that resumes later finds its
staged state intact. (The panel's current snapshot uses the working index
plus a reset; it should adopt the temporary index too.) The fingerprint for
the existing drift check is the snapshot's TREE hash — `write-tree` is
deterministic for identical trees, which is what attempt.py already compares —
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
load-bearing, not hygiene. The jailed process never sees the run directory —
it prints its JSON to stdout exactly as the in-job contract requires, and
the job's sbatch wrapper (orchestrator-authored, OUTSIDE the containment,
running no agent-written code) captures stdout and writes the result file
into the run directory — the same stdout-capture split the in-job evaluator
performs in-process today. The afterany wake re-enters the climb stage that
was parked and reads that file. Failure modes map
to the existing layers, exactly as the sweep implements them: a job that
dies -> the `eval-error` outcome at wake (ending `aborted`, exactly as the
in-job eval failure maps today); a LOST wake -> the sweep's primary backup,
which wakes any run whose job reads terminal after the grace period —
plus GONE past the deadline (vanished) and PENDING past the deadline
(cancel, then wake as unschedulable); a still-RUNNING job is deliberately
left alone, bounded by its own walltime; wakes that fire without producing
progress -> `stuck` at MAX_WAKE_ATTEMPTS. A moved base during the wait is
review's to handle — the sealed candidate publishes as-is (research-loop.md:
a stale PR is a re-wake, never an orchestrator auto-merge). Worktree cleanup has a named owner at every exit: the
wake's own finally (primary, as in-job measures do today), the sweep's
run-ending path when a run dies waiting (best-effort rmtree of the run
dir's measure worktrees, same never-silent discipline), and ended-run
retention as the floor.

Suite gates and panel re-measures ride the same staging: a suite
re-measure over N siblings becomes 2N parallel eval jobs — baseline and
candidate per sibling, paired seed via `extra_env`, exactly the comparison
the gate computes today — instead of 2N sequential in-job runs. Fan-out
changes the record contract: `experiment_job_id` carries the COLON-JOINED
job ids (the same syntax `--dependency=afterany:` takes, so ONE wake job
covers the set), and the sweep's liveness read treats the set as alive
while any member is, terminal when all are. The first
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
wake, refuses when exhausted — and joins limits.py's clamp table first
(phase-2 prerequisite): today it is validated only as non-negative, and a
target-supplied number that escapes the clamp grammar would let a contract
raise our compute spend, the exact thing the ceilings exist to prevent — and a new `experiments_per_run` budget knob
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

**Eval-job durability under preemption is part of that same resource
policy.** What resumes across a park/wake is the SESSION, not the eval's
computation: the snapshot is a *code* tree (`candidate_sha`, checked out
fresh per attempt on node-local scratch), never a training checkpoint, and
an eval attempt is atomic — it either finishes and atomically writes its
result, or the attempt is lost. So a preempt or a walltime timeout that
leaves no clean result is an `eval-error` for that round (the TERM trap
writes exit 143 → nonzero → error; a hard SIGKILL leaves no exit-code →
"vanished"), never a from-checkpoint continue, and the measurer never
auto-resubmits a vanished job (a genuinely broken eval would loop forever).
Today's design therefore assumes an eval fits one walltime on a partition
where it will not be preempted. Making that explicit is a resource-policy
field alongside partition/GPU: either (a) route evals to a NON-preemptible
partition — simplest, and a single measurement is short next to a real
train — or (b) submit with `--requeue` plus a longer walltime: a requeued
job KEEPS its id and name, so the cluster-authoritative liveness check sees
it as still-live and re-parks until it lands. But (b) does NOT work against
today's eval-job script unchanged — its TERM trap writes a terminal exit
code and exits 0, which Slurm reads as a clean completion and will not
requeue. Making `--requeue` fire needs the script to distinguish a
preemption TERM from a walltime TERM (e.g. re-raise the signal on
preemption so the job exits non-zero / killed, letting Slurm requeue, while
still writing exit 143 on the walltime deadline). That trap change ships
WITH the requeue policy, not before it. Per-workload checkpointing to shared
storage is the heavy
last resort, warranted only once an eval grows long enough that a
from-scratch rerun is too costly (the 8-seed ruler eval at ~68 min is the
first workload that approaches it). Both (a) and (b) are operator-ceiling-
clamped knobs on the same phase-3 contract schema.

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
| `stage` (phase 1) | a small object, not a label: the parked measure point PLUS the process-local state re-entry needs — `base_sha`, `candidate_sha` and the candidate snapshot's `candidate_ref` (random, so it MUST be stored — a terminal wake hands it to `drop_snapshot` or the snapshot leaks), the drawn `seed` and `suite_seed` (both random, both stored so the wake re-measures PAIRED), the pre-eval tree fingerprints the drift check compares (today locals; a resumed process without them would fail the drift check closed on every dispatch), and the expected result-file names. The scope/suite `measured_paths` are NOT in here — they are re-derived from the `base_sha..candidate_sha` diff (step 4), not stored |
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
backup wake fires when the afterany job is scancel'd. The named first workloads are yolo's:
the ruler-v2 `BENCH_FULL` grid, and ruler v3 — the maintainer wants T
(trajectory length, fixed at 500 today) promoted from the diagnostic grid
into the CLIMBED grid at 8 seeds, which triples the eval past any in-job
budget (~1000 runs) and is exactly the shape phase 1 exists to serve.
