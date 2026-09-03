# The resident tick

*Design note, 2026-09-02. Status: built as the opt-in mode
(`AUTORESEARCH_RESIDENT=1`, `scripts/tick_resident.sh`); the chain restarts of
2026-09-02 are the evidence.*

## The problem

The tick chain schedules **one Slurm job per cadence**: each tick maintains two
queued successors (`--dependency=singleton`, `--begin` on the cadence grid) and
exits.
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
  armed wakes included — cause unknown, but a reminder that pending jobs depend
  on the site scheduler.

Mitigations shipped (#234: a running tick requeues a moved successor; the
sweep redelivers a moved wake) only help while a tick runs. The root cause is
structural: the chain's liveness depends on the scheduler starting a fresh
job every thirty minutes.

## The design

**One long-lived tick job that loops** — deploy, tick, sleep to the next slot —
so the chain needs a handful of scheduling events per *day*, not one per
cadence. `cpu_short` accepts at most six hours (`sbatch --test-only`: 06:00:00
accepted, 06:01:00 rejected — the partition's QoS), so a resident job lives
six hours and hands over four times a day: twelve times fewer scheduling
events than the 48 per-cadence jobs.

```
resident job (cpu_short, --time=06:00:00 passed at start, singleton)
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

Starting it is `autoresearch start` (`src/autoresearch/cli.py`): it fills in the
walltime, job name, placement, and exports from flags, the environment, or
`~/.config/autoresearch/.env`, and refuses to submit beside a live resident.

## What it does not fix

If the resident job itself is pending (first start, or a handover during
congestion) the chain is down until it starts — the same exposure as today,
four times a day instead of 48. `cpu_short` has `PreemptMode=OFF`, so a running
resident job is not preempted. A dead node kills the job; the successor
covers it.

## Rollout

1. Opt-in mode in `scripts/tick_chain.sbatch`: `AUTORESEARCH_RESIDENT=1` in
   the chain's environment selects the loop; unset keeps the per-cadence
   chain. The walltime is fixed by Slurm before the script runs, so the
   resident chain is STARTED with an explicit walltime and its own job name:

   ```
   sbatch --time=360 --job-name=autoresearch-resident --dependency=singleton \
     --account=… --partition=cpu_short \
     --export=ALL,AUTORESEARCH_RESIDENT=1,AUTORESEARCH_HOME=…,AUTORESEARCH_ROOT=…,\
   AUTORESEARCH_ACCOUNT=…,AUTORESEARCH_PARTITION=cpu_short,AUTORESEARCH_PAT_FILE=… \
     scripts/tick_chain.sbatch
   ```

   The two modes have different job names, so the switch needs no cancel:
   the per-cadence chain stops topping up as soon as a resident job exists
   and drains within a cadence (the resident also cancels its pending
   successors on start; a still-running one finishes its tick, and the
   tick's coalescing guard makes an overlap a no-op). Switching back: set the
   pause sentinel (the resident cancels its successor and exits), clear it,
   start a per-cadence chain.
2. Watch a day of handovers in the tick log (four, at six-hour walltime).
3. Make resident the default; keep the per-cadence mode as the LocalCompute /
   dev path.

Related: `docs/design/architecture.md` (Scheduling), #234, #235/#237.
