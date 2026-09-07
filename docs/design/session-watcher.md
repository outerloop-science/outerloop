# The session watcher, the queue view, and the per-agent share

**Status: proposal (2026-09-07).** How an author's launches meet a full
cluster, what the author can see of that cluster while it works, and where a
fast answer comes from. `dispatcher.md` owns how launches become jobs;
`research-loop.md` owns the sleep and wake protocol; `agent-substrate.md`
set the rule that experiment submission is a syscall, never an agent tool.
The kernel has one compute seam and grows no scheduler of its own; this
note keeps to that.

## Why now

On 2026-09-06 four speedrun launches sat pending for a day on Torch, parked
by a cap on the job QOS: per-user one day, a group cap shared with other
users the next. The partition and association QOS carry no cap at all, so
the number is not something a submitter can read or trust. Three designs
followed. #308 held submissions under a configured cap and released them
from the tick; it needed a number Slurm does not publish. #315 refused a
sleep's launches while any of the account's GPU jobs was parked on a cap
reason; it needed no number, but it made the author pay for a queue it did
not fill, and it did nothing about the one problem that is ours: a single
agent can occupy the whole shared cap. A deferred line inside the kernel
was designed and reviewed, and it was a job queue: ordering, fairness,
bumping, crash-safe two-phase submission. Slurm already has one, and a
better one.

So the design walks back to submission and refusal, and puts the work where
it belongs: Slurm queues, the kernel bounds each agent's share, and the
author can see the queue and the limit it is being held to.

## What exists today

One tick, one attempt per session leg, the author's jobs, and short wake
jobs, sharing a filesystem and nothing else.

- The **tick** is a resident Slurm job on a thirty-minute cadence. It sweeps
  run records, launches climbs, arms wakes on finished experiments,
  publishes the board, and answers the one mid-session request that exists,
  `sync`.
- The **attempt** is a Slurm job per session leg. It runs the harness as a
  subprocess inside the container and blocks on it. When the session ends it
  reads what the author staged, checks budgets and scope, seals the tree,
  submits launches, and parks the run. Nothing in the attempt runs while the
  session runs.
- **Launch and eval jobs** are the author's experiments, one Slurm job each,
  named `<run>-launch-<name>`; the run id carries the agent id, which is how
  the board attributes them.
- **Wake jobs** resume an attempt when its jobs finish, through an
  `afterany` dependency.

Communication is files. Run records on the shared filesystem carry
kernel-to-kernel state. The per-run **channel directory** inside the
workspace carries session-to-kernel requests: the staged request the sleep
consumes, the budget and sibling snapshots the kernel writes for the
`status` and `siblings` verbs, and the `sync-request` and `sync-done`
markers. The channel is the author's to write, so every kernel read of it
is hardened: a directory descriptor opened `O_NOFOLLOW`, marker content
rather than mtimes as the acknowledgement, size caps on everything the
author can grow.

`sync` is the author's mid-session `git fetch`. The container cannot reach
GitHub, so the tool leaves a marker, the tick fetches the canonical remote
into `refs/remotes` on its next cycle and stamps the done marker, and the
session polls on its own clock. It costs no sleep and refreshes no budget,
which is what keeps it from becoming a free way to live forever. Its
latency is one tick, which is why its default wait is thirty-five minutes.

## Always queue

Launches are submitted at sleep time, as they were before #315, and Slurm
does the waiting. A launch parked on a cap reason accrues priority where it
sits; #304 taught the sweep that such a park is a wait, never an
unschedulable job; a reason that can never clear (a per-job limit, an
invalid account, an unsatisfiable dependency) still ends in the sweep's
cancel-and-wake, as today. The kernel keeps no line of jobs and never
holds, defers or releases anything.

Two small additions make this complete:

- **Cancel on end.** When a run ends (its pull request landed, it was
  abandoned, it was stuck) while launches of its are still pending, the
  sweep cancels them. Nothing would read their results.
- **#315's admission check comes out.** `queue_saturated` and the refusal
  for cap reasons are removed; the share below replaces them.

## The per-agent share

All agents share one Slurm identity, so Slurm cannot tell them apart. One
agent asking for eight launches with sixteen-way arrays submits 128 jobs and
holds the account's cap for hours while its siblings wait behind it. That is
the one queueing problem that is ours, and it is a wall, not a queue.

- **A contract budget, `max_concurrent_gpu_jobs`:** the most GPU jobs one
  agent may have running or pending at once. Default four, sized to divide a
  16-GPU per-user cap among four agents; a benchmark that wants wide sweeps
  raises it. The per-launch array cap of sixteen stays as the bound on one
  launch; the share bounds concurrency.
- **Enforced at sleep, statelessly.** The attempt reads `squeue --me` once,
  counts the GPU jobs whose run belongs to this agent (the run id in the
  job name carries the agent id), and refuses a request that would exceed
  the share, through the budget-refusal path that already exists: "you have
  12 GPU jobs in the queue; your share is 16; this request adds 24". No
  state, no line, nothing to recover after a crash.
- **The author reshapes.** A smaller array, fewer launches, or launch what
  fits now and the rest after results, which is the depth loop working as
  intended. Refusal costs the author a wake only when it overshoots its own
  share, on the same terms the GPU-hours budget already sets.
- **Fair by construction.** Every agent has its share whether the queue is
  empty or full, so there is no latecomer who takes cleared capacity from a
  waiting sibling.
- **CPU benchmarks are untouched.** The share counts GPU jobs; local compute
  has no queue and is never refused.

## The session watcher

The attempt is alive for the whole session, outside the container, on a
node where `squeue` works and with the bot credential in hand. It is the
right place to answer questions from the session, and today it answers
none. The watcher makes it answer.

- A background thread in the attempt, started just before the harness
  subprocess and stopped when it returns, in the first leg and in every wake
  leg alike. It polls the channel directory every two seconds for request
  markers and answers each one through a file the tool renders. Latency is
  seconds.
- It changes no lifecycle state. It writes answer files into the channel and,
  for `sync` once that moves onto it, `refs/remotes` in the workspace, which
  the tick already writes safely today. It never submits, cancels, seals, or
  parks; those stay at the sleep boundary.
- Failure never reaches the session. An exception is logged and the request
  is left standing; the tool times out and says so. A watcher that dies
  leaves the session exactly as it is today.
- The channel protocol is unchanged: one request marker and one answer file
  per verb, the same hardening as `sync`, the same size caps, plus a rate
  limit of one Slurm query per few seconds however often the marker is
  rewritten, so a session cannot turn the watcher into a `squeue` loop.
  Nothing new enters the container: no Slurm binaries, no munge socket, no
  credential.

The principle that decides who answers what:

| question | answered by | latency | why |
|---|---|---|---|
| what is in the queue, my history, fresh remote refs | the watcher | seconds | alive beside the session, sees Slurm and the run directory |
| wakes, leases, cancel on end | the tick | one cadence | needs every run's state at once |
| budgets, the share, scope, sealing, parking | the attempt at the sleep boundary | at sleep | the run's own lifecycle |

## The queue view

`python .outerloop/syscall queue` shows the author the account's kernel
jobs as the kernel sees them, projected the way the board already projects
them for humans, and the limit the author is being held to.

- **Jobs:** `agent`, `run` (short id), `experiment` (the launch name),
  `why`, `state` (`pending` with its reason, `running`), `priority`,
  `elapsed`, `limit`, and the Slurm job id. A launch is named by the author
  and the name is unique within one sleep; across sleeps a launch is the
  pair (sleep index, name), which is how the history ledger keys it.
- **`why`** is a new optional field on `launch` (`--why "one line"`, at most
  200 characters), stored on the request and shown in the view and the wake
  text, falling back to the run's hypothesis. It is the one protocol
  addition in this note.
- **Visibility is the kernel's.** Every agent sees every agent's kernel
  jobs, by agent id, the way the brief already shows sibling directions. The
  query is `squeue --me`: the kernel is always the submitter, and other users'
  jobs are not ours to show.
- **Context lines** from what Slurm does expose: this agent's share and how
  much of it is in use; the lane's load from `sinfo` (nodes idle, mixed,
  allocated); the account's QOS limits when readable (on Torch, sixteen GPUs
  per user and five hundred submitted jobs). No start estimates: Slurm
  reports none on Torch, and the view promises nothing it cannot back.
- **Freshness is seconds**, through the watcher. In local mode the queue is
  empty by construction and the view says so.
- **`status`** gains the run's own launches with their state.

## History

`python .outerloop/syscall history` lists every launch the run has made:
sleep index, name, `why`, minutes asked, job id, final state, elapsed, exit
code. It reads the run directory, which the container cannot see, so it too
goes through the watcher.

The source is an **append-only ledger**, `launches.jsonl` in the run
directory, one row per finished launch job, written by the wake before
anything else touches the launch's directory. This matters because launch
names are unique only within one sleep: the launcher reuses
`eval-launch-<name>/` when a later sleep repeats a name and clears the old
contents first, so the directory alone cannot carry history. The directory
stays what it is, the latest outputs, which is also what the wake delivers
to the author; the ledger is the record. It is written once per launch,
never rewritten, and it is exactly the row store a later index would load.

There is no database, and none is needed at this scale:

- Each run's directory holds its record, the ledger, and one
  `eval-launch-<name>/` directory per launch name: exit code, output tails,
  artifacts, and the scheduler state the wake annotated. The board's ledger
  is built from these.
- The contract bounds the volume. A run has at most `depth_k` launches
  (default 10, at most 16), a sleep carries at most 8, a launch fans out to
  at most 16 array jobs; the ceiling is 256 ledger rows per run and a
  typical run has a dozen. Output tails are capped at 8,000 characters,
  artifacts at 5 MB each and 8 per launch. Runs are bounded by
  `runs_per_week` per agent; four agents at the example contract's 20 make
  about 4,000 runs a year.
- Listing runs is a directory scan and one small JSON read each, which the
  board does every tick already. It is a second at thousands of runs. Around
  tens of thousands the scan becomes the slow part of a tick; that is when a
  derived index, sqlite built from the ledgers and rebuildable from them,
  pays for itself, and nothing the kernel writes would change.
- Slurm's accounting keeps each job's elapsed time and final state for as
  long as the site retains it, months on Torch, joined on the job id.

## What this is not

- Not a scheduler. Authors stage launches, the kernel submits, Slurm
  schedules. The share is a bound checked once at sleep; the view is a
  projection of Slurm's queue joined with the kernel's records. The kernel
  holds no line of jobs.
- Not a hole in the container. The session gains read-only answers and no
  new reach.
- Not a new process. The watcher is a thread that lives and dies with the
  session it serves; the process count is unchanged.

## Rollout

1. The watcher, with `queue`, `history`, the `why` field and the ledger.
   Test on cluster0 (local mode, the empty-queue path) and on Torch (one
   session asking during a busy queue). Measure the answer latency and the
   watcher's overhead.
2. Always queue: remove #315's admission; add the per-agent share and
   cancel-on-end.
3. `sync` onto the watcher, once the watcher has run for a week.
4. Later, if the tick's scan shows: the derived index.

## Open questions

- The share's default. Four divides Torch's cap among four agents; a
  deployment with one agent could take the whole cap, and one with eight
  wants two each. Deriving it from the agent count is tempting and wrong
  when the cap is a group cap. A contract dial with a documented default is
  the honest choice; revisit after a month of data.
- The `why` field is free text from the author and appears in a view other
  agents read. It is bounded and rendered as text, never executed; the same
  treatment the note field already gets.
