# The resident tick

*Design note, 2026-09-02. Status: proposed — the chain restarts of 2026-09-02
are the evidence.*

## The problem

The tick chain schedules **one Slurm job per cadence**: every tick queues two
successors (`--dependency=singleton`, `--begin` on the cadence grid) and exits.
That is 48 scheduling events a day, each one an opportunity for the scheduler
to do something we cannot control. On Torch, 2026-09-02, it did:

- Under `cpu_short` congestion the site **moved eligible pending jobs into the
  lower-tier catch-all partition `all`**, where they starved for hours. Our
  jobs may request only `cpu_short`; `all` and `cpu_prem` are rejected at
  submit, and `scontrol update Partition` is refused. We cannot route around
  the move.
- A moved successor never starts and never ends, so its twin waits on
  `singleton` forever: **the chain stopped for five hours** until a human
  cancelled the moved job.
- Giving successors a `--deadline` (#235) made it worse: Slurm enforces the
  deadline against its own *estimated* start, which under congestion sits
  hours out, so it **cancelled every successor within minutes** (#237 reverted).
- At 19:02 ET the scheduler also cancelled every pending job of ours at once,
  armed wakes included — cause unknown, but a reminder that pending jobs are
  hostage to the site.

Mitigations shipped (#234: a running tick requeues a moved successor; the
sweep redelivers a moved wake) only help while a tick runs. The root cause is
structural: the chain's liveness depends on the scheduler starting a fresh
job every thirty minutes.

## The design

**One long-lived tick job that loops** — deploy, tick, sleep to the next slot —
so the chain needs one scheduling event per *day*, not per cadence:

```
resident job (cpu_short, --time=1-00:00:00, singleton)
  submit ONE successor: --dependency=afterany:<self>,singleton   # continuity
  loop until 20 minutes before walltime:
    deploy (fetch main + reset, uv sync, re-read .env)            # as today
    run one tick as a CHILD process with a 15-minute timeout      # never exec
    sleep until the next cadence slot
```

- **Continuity.** The single successor waits on `afterany:self`, so it is
  ineligible (not a candidate for the site's moves) until this job ends —
  walltime, node death, or preemption — and then starts as soon as the
  scheduler gives it a node. One handover per day; during it, the armed
  per-run wakes keep firing on their own, exactly as they did today.
- **Deploy-at-tick stays.** Each iteration re-deploys and re-reads the operator
  `.env` before the tick, so merges to `main` and live config changes still
  land at the next cadence. The successor also runs the script from the
  checkout, so shim changes propagate at handover instead of one generation
  later.
- **A hung or crashed tick never takes the loop with it.** The tick runs as a
  child under `timeout`; the loop logs the exit and sleeps to the next slot.
  The pause sentinel is honored per iteration.
- **Idempotent by construction.** Nothing in the tick changes: same records,
  leases, markers, coalescing guard. A tick that runs twice or late is already
  safe (the lease makes restarts safe); the resident loop only changes *who
  starts it*.
- **Logs.** The loop reopens `logs/tick-YYYYMMDD.log` per iteration, so the
  daily files keep their shape and the watchdog keeps its heartbeat.

## What it does not fix

If the resident job itself is pending (first start, or a handover during
congestion) the chain is down until it starts — the same exposure as today,
once a day instead of 48 times. `cpu_short` has `PreemptMode=OFF`, so a running
resident job is not preempted. A dead node kills the job; the successor
covers it.

## Rollout

1. Opt-in mode in `scripts/tick_chain.sbatch`: `AUTORESEARCH_RESIDENT_HOURS=24`
   selects the loop; unset keeps today's per-cadence chain, byte for byte.
   The loop's slot arithmetic and the tick timeout are small helpers with
   tests (PATH shims), like the lock sweep.
2. Start one resident chain beside nothing else (singleton makes the two
   modes exclusive); watch a day of handovers in the tick log.
3. Make resident the default; keep the per-cadence mode as the LocalCompute /
   dev path.

Related: `docs/design/architecture.md` (Scheduling), #234, #235/#237.
