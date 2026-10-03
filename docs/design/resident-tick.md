# The resident tick

*Design note, 2026-09-02. Status: built, and the default for Slurm
deployments: `outerloop start` submits a 360-minute resident job
(`scripts/tick_resident.sh`). Two other tick hosts exist: the foreground local
loop, and the login-host loop described under [Tick hosts](#tick-hosts).*

## The problem

The tick chain schedules **one Slurm job per cadence**: each tick maintains two
queued successors (`--dependency=singleton`, `--begin` on the cadence grid) and
exits.
That is 48 scheduling events a day, each one an opportunity for the scheduler
to do something we cannot control. On one site, 2026-09-02, it did:

- Under congestion the site **moved eligible pending jobs into a lower-tier
  catch-all partition**, where they starved for hours. Our jobs could not
  request that partition or move themselves back (`scontrol update Partition`
  is refused), so we could not route around the move.
- A moved successor never starts and never ends, so its twin waits on
  `singleton` forever: **the chain stopped for five hours** until a human
  cancelled the moved job.
- Giving successors a `--deadline` (#235) made it worse: Slurm enforces the
  deadline against its own *estimated* start, which under congestion sits
  hours out, so it **cancelled every successor within minutes** (#237 reverted).
- At 19:02 ET the scheduler also cancelled every pending job of ours at once,
  armed wakes included — cause unknown, but a reminder that pending jobs depend
  on the site scheduler.

Mitigations shipped (#234: a running tick requeues a moved successor; the
sweep redelivers a moved wake) only help while a tick runs. The root cause is
structural: the chain's liveness depends on the scheduler starting a fresh
job every thirty minutes.

## The design

**One long-lived tick job that loops** — deploy, tick, sleep to the next slot —
so the chain needs a handful of scheduling events per *day*, not one per
cadence. The partition we used accepts at most six hours, so a resident job
lives six hours and hands over four times a day: twelve times fewer
scheduling events than the 48 per-cadence jobs. `OUTERLOOP_RESIDENT_MINUTES`
sets the walltime for partitions with a different limit.

```
resident job (--time=<resident minutes> passed at start, singleton)
  submit ONE successor: --dependency=afterany:<self>,singleton   # continuity
  loop until 20 minutes before walltime:
    if the pause sentinel is set: cancel the successor, exit     # no resubmit
    deploy (fetch main + reset, uv sync, re-read .env)            # as today
    if the shim's hash changed: cancel + resubmit the successor   # fresh shim
    run one tick as a CHILD: timeout --kill-after=60s 15m         # never exec
    sleep until the next cadence slot
```

- **Continuity.** The single successor waits on `afterany:self`, so it is
  ineligible (not a candidate for the site's moves) until this job ends —
  walltime, node death, or preemption — and then starts as soon as the
  scheduler gives it a node. Four handovers a day; during one, the armed
  per-run wakes keep firing on their own, exactly as they did today.
- **Pause exits without resubmitting.** The sentinel is read at the top of
  every iteration; when set, the loop cancels its queued successor and exits,
  which is the architecture's rule for the pause and what a paused chain must
  mean: nothing queued.
- **Deploy-at-tick stays.** Each iteration re-deploys and re-reads the operator
  `.env` before the tick, so merges to `main` and live config changes still
  land at the next cadence. Slurm spools a batch script at submission, so the
  queued successor carries the shim as it was when queued: after a deploy that
  changed `tick_chain.sbatch` (hash compare), the loop cancels and resubmits
  the successor, and handover runs the current shim — no extra generation.
- **A hung or crashed tick never takes the loop with it.** The tick runs as a
  child under `timeout --kill-after=60s 15m` (TERM, then KILL a minute later
  if it ignores TERM); the loop logs the exit and sleeps to the next slot.
- **Idempotent by construction.** Nothing in the tick changes: same records,
  leases, markers, coalescing guard. A tick that runs twice or late is already
  safe (the lease makes restarts safe); the resident loop only changes *who
  starts it*.
- **Logs.** The loop reopens `logs/tick-YYYYMMDD.log` per iteration, so the
  daily files keep their shape and the watchdog keeps its heartbeat.

Starting it is `outerloop start` (`src/outerloop/cli.py`): it fills in the
walltime, job name, placement, and exports from flags, the environment, or
`~/.config/outerloop/.env`, and refuses to submit beside a live resident.

## What it does not fix

If the resident job itself is pending (first start, or a handover during
congestion) the chain is down until it starts — the same exposure as today,
four times a day instead of 48. Use a partition without preemption for the
resident job; a dead node kills the job and the successor covers it.

## Tick hosts

`outerloop start` picks where the loop runs:

- **Resident job** (the default when `sbatch` is on PATH): the design above.
- **Login host** (`OUTERLOOP_TICK_HOST=login`): the same loop in the
  foreground on a machine that can submit to Slurm, for sites where a
  long-lived CPU job is unavailable or slow to start. Experiments and evals
  still go to Slurm; only the tick stays on the host. It refuses to start
  beside a live resident job.
- **Local** (`OUTERLOOP_COMPUTE=local`, or no `sbatch`): the foreground loop
  with local compute.

The per-cadence chain is still in `scripts/tick_chain.sbatch` with
`OUTERLOOP_RESIDENT` unset, and drains itself once a resident job exists.

Related: `docs/design/architecture.md` (Scheduling), #234, #235/#237.
