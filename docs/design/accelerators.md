# Accelerators: metering and placement beyond GPUs

**Status: proposal, revised after review (2026-10-02); not built.** Outerloop
meters, caps and places experiment work in GPUs. This note proposes adding
Cloud TPUs as a second, separately metered device kind, so the same kernel can
run TPU benchmarks under budgets, operator limits and refusals as strict as
the GPU ones. Budget caps are load-bearing safety features: nothing in this
proposal may let TPU work run unmetered, or let a GPU budget pay for it.

## Why

A benchmark that runs on TPUs today would have to declare `gpus: 0`. The
kernel would then charge no device-hours, apply no `gpu_hours_per_run` budget,
count nothing against `max_gpus`, and might dispatch its evals as plain CPU
jobs. The meter is the safety feature, so a new device kind has to be metered,
placed and capped before any TPU contract is accepted.

## What assumes a GPU today

- **Declaration.** `Benchmark.gpus: int (0..8)`; `Measure.gpus`,
  `JobSpec.gpus`; the GPU type lives on the operator's per-target lane
  (`OUTERLOOP_GPU_LANES`, `gpu_lanes.py`). Whether evals dispatch depends on
  `eval_minutes`, not on `gpus`.
- **Metering.** Launches charge minutes × array × gpus / 60; gates charge the
  main eval (one when a cached baseline applies, else two) and every suite
  sibling pair × walltime × gpus (`syscall.py`). The run meter is
  `stage.gpu_hours_used`; budgets are `gpu_hours_per_run` plus
  `review_topup.gpu_hours`; `max_concurrent_gpus` paces sweeps.
- **Placement.** `JobSpec.to_argv` emits `--gres=gpu:TYPE:N` or
  `--gpus-per-node=N`, single node; dispatched evals add `--nv` and size CPUs
  and memory per GPU.
- **Operator limits.** `limits.toml` `max_gpus`, counted from one
  `squeue --me --json` snapshot by parsing `gres/gpu` TRES, attributing jobs to
  this fleet by run id.
- **Local backend.** `nvidia-smi` detection (or `OUTERLOOP_LOCAL_GPUS`),
  `CUDA_VISIBLE_DEVICES` pinning.
- **Surfaces.** Briefs, wake lines, refusals, `budget.json`
  (`gpu_hours_remaining`), `outerloop status`, `outerloop limits` and the board
  carry GPU-named fields.

## Proposal

The first version is deliberately narrow: **existing GPU keys and behaviour
stay exactly as they are**, and TPU is added beside them with its own fields.

### Declaration

```yaml
benchmarks:
  - name: speedrun-tpu
    tpu_chips: 8          # one TPU node per job; mutually exclusive with gpus
```

`tpu_chips` and `gpus` cannot both be set. A suite gate's device demand is
the union over all its benchmarks, including the one that initiated it. In the
first version a gate may demand at most one device kind, and the initiating
benchmark must carry that kind: GPU and TPU never mix in one gate, and a
CPU-led gate with an accelerator sibling is refused at contract load. That
kind decides authorization, dispatch and the meter for the whole gate.

### Budgets and meters, per kind

- New contract field `budgets.tpu_chip_hours_per_run` (and
  `review_topup.tpu_chip_hours`). A TPU benchmark needs an explicit TPU budget;
  a contract with `tpu_chips` but no TPU budget is refused. GPU budgets never
  authorize TPU spending, and vice versa.
- New run-stage meter `stage.tpu_chip_hours_used`, retained everywhere
  `gpu_hours_used` is retained today (`STAGE_RETAINED_KEYS` and the stage
  reconstruction paths), and a persisted `stage.meter_kind`. The GPU meter is
  unchanged and stays authoritative for GPU runs, so a rollback never resets GPU
  spending.
- The arithmetic matches GPUs: chip-hours = minutes × array × chips / 60 for
  launches, evals × walltime × chips for gates.
- No conversion between kinds and no money in the kernel; operators set
  per-kind caps.

### Placement

One TPU node per job in the first version (a single-host slice, e.g. 8 chips):
the backend requests one whole node from the operator's TPU lane, with no GPU
GRES and no `--nv`. Multi-host slices are out of scope until the kernel owns
distributed worker start-up, topology and per-node environment. The TPU lane
is per target, like GPU lanes, and carries the node's chip count and the
container image; the kernel refuses a benchmark whose `tpu_chips` does not
equal the lane's node size.

The first backend provisions one Cloud TPU VM per job (Slurm on Google Cloud
does not support current TPU generations out of the box). It is a bounded
adapter behind general compute interfaces the kernel gains once: accelerator
demand, opaque job ids, lifecycle and accounting observations, artifact
staging, and capability refusals. Cloud API calls, state translation,
transport and cleanup stay inside the adapter, which owns deterministic
resource names, reconciliation after an ambiguous create, queue and execution
deadlines, and deletion confirmed independently of the worker. Author sessions
and wakes never run on a TPU allocation; the deployment gives them a CPU lane.

### Operator limits

`limits.toml` gains `max_tpu_chips` (fleet default and per target). Admission
checks every kind a request demands; a ceiling on one kind is not skipped
because another kind has none. The kernel persists what it submitted for each
TPU job (allocation id, chips, array concurrency) and counts running and
pending allocations from that record, reconciled with the backend. A fleet
job whose TPU allocation cannot be bounded from either source blocks further
TPU admission until it is resolved; it is never counted as zero or as a
guess. A finite ceiling with no usable snapshot fails closed, as today. A
capacity refusal parks the run (#453). The limits bound this fleet's own jobs
(attributed by run id); they are not a cloud-project-wide cap, which the
deployment enforces separately.

### Refusal until complete

TPU support ships as one feature flag that is off until declaration, budgets,
meters, placement, limits, caches and surfaces are all in place. Until then a
contract with `tpu_chips` is refused with a clear message, never run as CPU.
The local backend refuses TPU benchmarks explicitly.

### Caches

Both measurement caches (the baseline cache and the dispatched-eval
determinant) include the device kind, count, resolved image and runtime
identity. A legacy cache entry never satisfies a TPU measurement.

### Preemption

Spot TPU and spot GPU nodes can be reclaimed. The first version submits TPU
jobs with no automatic requeue: a preempted job fails, the author sees it on
wake, and its time is charged. Automatic retries come later and only with
cumulative accounting (every execution's time charged, the retry reserved
before it starts).

### Surfaces

Text names the unit in front of the reader ("GPU-hours", "TPU chip-hours")
from one helper. `budget.json`, `outerloop status` and the board gain
`tpu_chip_hours_*` fields beside the GPU ones; durable board rows record the
meter kind with the value.

## Existing GPU accounting gaps (separate fixes, found in review)

- The dispatched-eval cache determinant omits the GPU count, so a result can
  be reused across different GPU counts (`measure.py`).
- Jobs allocate declared minutes plus ten setup minutes, but only the declared
  minutes are charged (`dispatch.py` `eval_job_spec`); refunds never debit an
  overrun.
- Launch-hour reconciliation reads one `sacct` record, so a requeued job's
  earlier executions can drop out of accounting.
- `max_concurrent_gpus` is a per-launch pacing hint (at least one task runs),
  not an aggregate cap; concurrent admissions can overshoot `max_gpus`
  (documented in `operator_limits.submit_batch`).

These are fixed for GPUs first (they are the template the TPU meter copies),
each in its own PR.

## Compatibility

New persisted fields only: `tpu_chips` and TPU budget fields in contracts,
`tpu_chip_hours_used` and `meter_kind` in run stages, `max_tpu_chips` in
`limits.toml`, device identity in cache keys. GPU records, contracts and limits
are byte-identical. Rolling back: an older kernel rejects contracts and
`limits.toml` files carrying the new keys (unknown fields are errors there).
Before rolling back: end every TPU run, parked ones included (`outerloop end`),
and confirm no TPU allocation remains; then restore contracts without
`tpu_chips`, `budgets.tpu_chip_hours_per_run` and `review_topup.tpu_chip_hours`,
and `limits.toml` without `max_tpu_chips`. Older kernels load ended TPU run
records (unknown fields are ignored there) and show them without TPU usage.
GPU runs are unaffected. Release fixtures cover legacy records, an interrupted
TPU run, and an idempotent rollback.

## Phases

0. **GPU accounting fixes** above (independent PRs).
1. **TPU, end to end behind the flag**: declaration, budgets, meters, limits,
   caches, placement on one backend, surfaces, refusals; fixtures for legacy
   records and contracts.
2. **First TPU fleet**: one TPU benchmark, one smoke run, then real runs.
3. Later, only if needed: multi-host slices, automatic retries with
   cumulative accounting, local TPU.

## Decisions

1. Cross-kind work in one run: no. A benchmark has one kind; suites are
   homogeneous.
2. Money in the kernel: no. Per-kind caps; cost reporting stays operational.
3. Preemption: fail and report on wake, all time charged; retries later with
   cumulative accounting.
