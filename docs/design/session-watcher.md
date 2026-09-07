# Deferred launches and the session watcher

**Status: proposal (2026-09-07).** Two changes to how an author's launches
meet a full cluster, and one new piece of kernel machinery they share.
`dispatcher.md` owns how launches become jobs; `research-loop.md` owns the
sleep and wake protocol; `agent-substrate.md` set the rule that experiment
submission is a syscall, never an agent tool. This note settles three
questions: who waits when the queue is full, what an author can see of the
cluster while it works, and where in the process model a fast answer comes
from.

## Why now

On 2026-09-06 four speedrun launches sat pending for a day on Torch. The
reason was `QOSMaxGRESPerUser`, then `QOSGrpGRES`: a cap on the job QOS that
Slurm applies to the account, shared with other users, and moved between
two days. The partition and association QOS carry no cap at all. So the
number is not something a submitter can read or trust, and the first fix
(#308, a configured per-user cap with held submissions) was the wrong shape.
#315 replaced it with a rule that needs no number, queue then stop: a
launch queues as long as none of the account's GPU jobs is pending on a cap
reason, and a sleep that asks for launches while one is gets a refusal.

That rule is right about the cluster and wrong about the author. A refusal
consumes the request, wakes the session once, and leaves the author to
sleep without launches and try again in a cadence, spending a sleep from
its budget and a wake's worth of turns on a queue it did not fill. The
owner's call: the kernel should wait, not the author. And an author should
be able to see the queue it is waiting on.

## What exists today

One tick, one attempt per session leg, the author's jobs, and short wake
jobs. They share a filesystem and nothing else.

- The **tick** is a resident Slurm job on a thirty-minute cadence. It sweeps
  run records, launches climbs, arms wakes on finished experiments, publishes
  the board, and answers the one mid-session request that exists, `sync`.
- The **attempt** is a Slurm job per session leg. It runs the harness as a
  subprocess inside the container and blocks on it. When the session ends it
  reads what the author staged, checks budgets and scope, seals the tree,
  submits launches, and parks the run. Nothing in the attempt runs while the
  session runs.
- **Launch and eval jobs** are the author's experiments, one Slurm job each,
  named `<run>-launch-<name>` so the board can attribute them.
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
- Read-only by construction. The verbs it answers are questions: `queue`,
  `history`, and, once proven, `sync`, whose only write is to `refs/remotes`
  and which the tick already performs safely. It never submits, cancels,
  seals, or parks; those stay at the sleep boundary.
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
| what is in the queue, what is my history, fresh remote refs | the watcher | seconds | alive beside the session, sees Slurm and the run directory |
| which deferred launch submits next, admission, wakes, leases | the tick | one cadence | needs every run's state at once |
| budgets, scope, sealing, parking | the attempt at the sleep boundary | at sleep | the run's own lifecycle |

## Deferred launches: the kernel waits

A sleep that asks for launches while the queue is full is honored, not
refused. The run parks with the request and the sealed tree recorded and no
jobs submitted, in a stage marked `deferred`. Each tick the sweep reads the
queue once for all deferred parks and, if no GPU job of the account is
pending on a cap reason, submits the oldest deferred run's launches through
the existing launcher, writes their job ids and `afterany`, and the wake
arms exactly as if they had been submitted at sleep time. One run per tick,
so the queue fills gradually as it clears rather than all at once.

- **Cost to the author: none.** No sleep, no launch, no GPU hours are
  charged until the jobs exist. The sleep it asked for is the sleep it gets.
- **The wake is the same wake.** A deferred park becomes an ordinary one the
  moment its jobs exist; the results, the wake text, and the budget
  accounting are unchanged downstream.
- **A floor, so waiting is visible.** After twelve hours deferred, the sweep
  wakes the author once with the situation and a fresh queue view, and the
  author decides: keep waiting, change the plan, or finish. This is the one
  place deferral costs a wake, and it costs no sleep.
- **Refusal survives for reasons that never clear.** A per-job limit, an
  invalid account, a dependency that cannot be satisfied: waiting cannot fix
  these, so `queue_saturated` keeps refusing them with the reason.
- **One field on the park record.** An author-sleep park already stores the
  request and the sealed sha; deferral adds the time it was deferred, which
  is also what tells the sweep this park is not a checkpoint sleep. One
  branch in the sweep. No new job store, no Slurm hold, no cap number, and
  nothing the wake path has to learn.

## The queue view

`python .outerloop/syscall queue` shows the author the account's kernel
jobs as the kernel sees them, projected the way the board already projects
them for humans.

- **Columns:** `agent`, `run` (short id), `experiment` (the launch name),
  `why`, `state` (`deferred`, `pending` with its reason, `running`, `done`),
  `elapsed`, `limit`, and the Slurm job id once one exists. A launch's
  identity is the name the author gave it; nothing new is minted.
- **`why`** is a new optional field on `launch` (`--why "one line"`), stored
  on the request and shown in the view and the wake text, falling back to
  the run's hypothesis. It is the one protocol addition in this note.
- **Visibility is the account's.** Every agent sees every agent's kernel
  jobs, by agent id, the way the brief already shows sibling directions.
  Jobs on the account that are not the kernel's appear as one count line,
  never by name.
- **A verdict line** closes the view, computed by the same check the kernel
  uses: "launches will queue now", or "the queue is full on `QOSGrpGRES`;
  N of our jobs are waiting".
- **Freshness is seconds**, through the watcher. In local mode the queue is
  empty by construction and the view says so.
- **`status`** gains the run's own launches with their state, so "is my
  deferred launch in yet" is one command.

## History

`python .outerloop/syscall history` lists the run's past launches: name,
`why`, minutes asked, job id, final state, elapsed, exit code. It reads the
run directory, which the container cannot see, so it too goes through the
watcher.

There is no database, and none is needed at this scale. The history already
exists as files:

- Each run's directory holds its record and one `eval-launch-<name>/`
  directory per launch job: exit code, output tails, artifacts, and the
  scheduler state the wake annotated. The board's ledger is built from these.
- The contract bounds the volume. A run has at most `depth_k` launches
  (default 10, at most 16), a sleep carries at most 8, a launch fans out to
  at most 16 array jobs; the ceiling is 256 job rows per run and a typical
  run has a dozen. Output tails are capped at 8,000 characters, artifacts
  at 5 MB each and 8 per launch. Runs are bounded by `runs_per_week` per
  agent; four agents at the example contract's 20 make about 4,000 runs a
  year.
- Listing runs is a directory scan and one small JSON read each, which the
  board does every tick already. It is a second at thousands of runs. Around
  tens of thousands the scan becomes the slow part of a tick; that is when a
  derived index, sqlite built from the same files and rebuildable from them,
  pays for itself, and nothing the kernel writes would change.
- Slurm's accounting keeps each job's elapsed time and final state for as
  long as the site retains it, months on Torch, joined on the job id.

## What this is not

- Not a scheduler. Authors stage launches, the kernel submits, Slurm
  schedules. The queue view is a projection of Slurm's queue joined with
  the kernel's records; deferral is a delay before submission. Nothing gets
  an id of its own and the kernel keeps no queue of jobs.
- Not a hole in the container. The session gains three read-only answers and
  no new reach.
- Not a new process. The watcher is a thread that lives and dies with the
  session it serves; the process count is unchanged.

## Rollout

1. The watcher, with `queue` and `history`. Test on cluster0 (local mode,
   the empty-queue path) and on Torch (one session asking during a busy
   queue). Measure the answer latency and the watcher's overhead.
2. Deferral. Replace the refusal for cap reasons with the deferred park; keep
   it for reasons that never clear. The twelve-hour wake.
3. `sync` onto the watcher, once the watcher has run for a week.
4. Later, if the tick's scan shows: the derived index.

## Open questions

- One deferred run per tick is conservative. If the queue clears and stays
  clear, releasing all deferred runs at once wastes nothing; the gradual
  fill matters only when the cap is tight. A batch size dial may be
  warranted after the first week of data.
- The `why` field is free text from the author and appears in a view other
  agents read. It is bounded (one line, 200 characters) and rendered as
  text, never executed; the same treatment the note field already gets.
- Whether the twelve-hour wake should shorten as the deadline of the
  session's own budget approaches. Left for the data.
