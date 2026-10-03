# Cloud compute and the run-artifact store

**Status: design sketch, post-0.1 (2026-09-08). Nothing here ships in v0.1 —
v0.1 runs on the two battle-tested compute backends, Slurm and LocalCompute.
This note captures the shape a third-party/cloud compute backend would take and
the storage substrate it forces, so the picture is ready when that version comes
up. It is a deeper dive on two seams [external.md](external.md) already names —
the `compute` interface and the storage interface — plus the metric schema
sketched in the metric-taxonomy work.**

The whole design turns on one observation: Slurm gave us a shared filesystem for
free, and that shared filesystem was quietly doing three separate jobs — holding
the kernel's own state, carrying the channel between the resident and its jobs,
and storing every run's output. Cloud has no cheap fleet-wide shared filesystem,
so the design is really just: give each of those three jobs a home that does not
depend on a shared disk.

## The seam is already the right shape

A `compute` backend is about five operations: **submit** a job (resource spec +
image + command), **poll** its state, **fetch** its output, **cancel**, and
report **capacity/placement** (map a contract's `gpus:` to a machine). Slurm and
LocalCompute already implement exactly this. The kernel's fire-and-wake loop only
ever *submits then polls* — it never reaches into a running job — which is what
lets a new backend drop in with no kernel change. Backends are peers; adding a
cloud one never retires the Slurm one.

## No shared filesystem: git in, artifact out

The central move. A cloud experiment job runs self-contained on its own local
disk. Code crosses the boundary as **git** (the job clones the target + `pip
install outerloop`, runs the session, seals the candidate, and `git push`es the
branch — this is already how code flows). Results cross as **one artifact to an
object store** (S3/GCS/R2), keyed by run_id, which the kernel pulls on wake. The
syscall channel stays local to the box for the session's duration; only the
terminal result leaves. Both sides reach the object store outbound, so the
kernel keeps its no-inbound posture.

This is deliberately *not* a shared filesystem, because a cloud shared FS
(EFS/FSx/Filestore) is region-pinned: the compute that mounts it must sit in the
same region, often the same AZ. That would forfeit the main reason to be on
cloud — chasing cheap GPU capacity, which varies by region and is cheapest on
spot. git is global and the object store is touched only at a job's start and
end (never in the hot loop), so the substrate is **region-agnostic** and the
resident can scatter jobs at whatever region is cheapest. (Given we already run
Cloudflare, R2 is attractive here — no egress fees, unlike cross-region S3/GCS.)

A shared or parallel filesystem earns its keep in exactly one place: **inside a
single multi-node training job** (data-parallel GPUs reading the same shards,
checkpoint sharing — FSx Lustre for that node group). But that node group is
same-AZ by physical necessity anyway, and provisioning it is that one job's
concern, torn down with the job — never a fleet-wide substrate the resident
mounts. Big shared datasets are the same story: a per-region read cache (object
store → local NVMe), not a global FS.

**Rule:** the fleet substrate is region-agnostic (git + object store); a
shared/parallel FS, if it appears at all, is scoped inside a single multi-node
job in one AZ and dies with it.

## Where the resident lives

The resident and the compute backend are **orthogonal**. The resident is a cheap
CPU orchestrator — wake on cadence, read run records, submit, poll, wake parked
runs, publish the board. It needs outbound network to GitHub, the backend's API,
and the result store; it needs no GPU and no co-location with the experiments.
Today the two coincide (resident on Torch, experiments on Torch) only because it
is convenient and free.

So cloud does not move the resident — it moves the *experiments*, and the
resident just calls a different backend's submit/poll. The backend is a **per-run
choice** carried by the contract, so one resident can run cheap jobs on Slurm and
burst GPU-heavy ones to cloud at the same time. None of the resident-chain
hardening (handover, admission, sweep arrays, coalescing) is wasted.

For a cloud-only adopter with no cluster, the same loop lives either as:

- **A small always-on CPU VM.** Simplest. A cloud VM has no walltime, so the
  Slurm handover chain is unnecessary — just keep the box up. On a cheaper
  spot/preemptible instance you regain the need for preemption survival, which
  the tick already has (idempotent, reads state fresh).
- **A serverless cron tick.** A scheduled function fires the tick each cadence,
  submits, exits. Near-zero idle cost, viable because the tick is
  stateless-per-invocation. Since the kernel is cadence-driven anyway, this is
  the cost-optimal cloud-only form; the VM only wins for continuous
  responsiveness between cadences.

**Bootstrap:** `outerloop start` from the adopter's laptop provisions the
resident once ("job zero"), then the resident lives entirely in the cloud and the
laptop can go away — the same backend that launches experiments launches the
resident, with the one outside push being job zero. It stays outbound-only:
nothing reaches into the resident. The backend's lifecycle now covers
provisioning and tearing down the resident itself, not only experiment jobs.

The one honest coupling: the **Slurm** backend ties the resident to the cluster
(local `sbatch`/`squeue` + the shared FS). That is real and fine — for Slurm,
resident-on-cluster is the natural home. Cloud backends have no such coupling.

## Durable state without a database

An ephemeral or spot resident cannot lean on a shared disk for its own `state/`
— run records, markers, job handles, logs. The reflex is a database; resist it.
The kernel's state is **single-writer** (one resident writes), **small and
blob-shaped** (a handful of JSON files, not rows/relations), and **scanned fresh
each tick** (a directory walk, not a query). That is a filesystem/bucket
workload, not a database workload — a database would buy concurrency, queries,
and joins the kernel deliberately does not have, at the cost of a standing
service to run, back up, secure, and make every adopter provision (against the
lean-kernel and adoption-simplicity principles).

Durability is a **storage-backend choice**, in increasing weight:

- **Persistent volume (the default).** A cheap VM with an attached disk; the
  state root (`OUTERLOOP_HOME`) is a mounted POSIX directory, exactly as on
  Torch's shared FS. The VM restarts, re-attaches, state is there. Zero kernel
  change. "A small box with a disk" is not more complex than what we run today.
- **Object-store state root (only for the diskless serverless shape).** A small
  storage seam behind the state accessors (get/put keyed JSON), mapping almost
  1:1 onto the current file-per-record shape — and reusing the *same* object
  store the experiment results already need, so it is not a new dependency.

A lot of durable state already lives in git (the research-log branch holds the
board, curves, ledger); what needs a home off-disk is the volatile in-flight
state (parked runs, job handles, sweep markers). The rule: **state stays
blob-shaped and single-writer; durability is disk → volume → bucket, never a
database.**

## Providers, and not writing N of them

Three shapes:

- **Managed batch/queue** — AWS Batch, GCP Batch, a second Slurm, EmpireAI.
  Closest to today: submit + poll, they own scheduling.
- **Raw GPU rental** — RunPod, Lambda, vast.ai, Crusoe. You own provision +
  teardown + cost-per-second.
- **Serverless GPU** — Modal, Replicate. Function-shaped; they own the box and
  volumes.

Recommendation: **the first cloud backend is a meta-provider, not a specific
cloud.** [SkyPilot](https://skypilot.readthedocs.io) is OSS and abstracts
AWS/GCP/Azure/RunPod/Lambda/K8s behind one submit-job API, with spot and
auto-teardown built in — one backend buys most of the clouds (the unify move
rather than a backend per vendor). **Modal** is the strong second: elegant
serverless plus its own volumes solve the FS problem natively. Native
RunPod/Lambda only for a cloud SkyPilot does not cover.

## Lifecycle, cost, security

Slurm gave these to us for free; cloud makes them explicit.

- **Ephemeral per-job first** (spin up → run → tear down); a warm pool later if
  cold-start latency bites — the same resident-vs-per-cadence choice we already
  made on Slurm.
- **Teardown is load-bearing** for two reasons at once: you pay per second, and a
  box holding credentials should not outlive its job. Aggressive teardown + a
  per-run dollar cap (teardown-on-overrun). GPU-hours are already tracked; add a
  cost column.
- **Credentials:** the adopter's own creds on the adopter's rented box — same
  trust as their workstation — but ship **short-lived scoped tokens**, not
  long-lived ones, and reuse the existing container image for containment.

## The run-artifact store

The final scalar was always an impoverishment. A run produces a **bundle of
artifacts**, and they all fit the git+object-store substrate with no new
mechanism — results, reports, traces, and measurement trajectories are one thing:
artifacts a job emits, keyed by run_id. This is better even on Slurm, where
traces and curves live in run dirs today and get reaped (the inode-pressure
problem); making them first-class object-store artifacts makes them durable and
linkable.

Split by **shape**, onto the two tiers we already have:

- **Small, structured → the measurement record** (durable state, published to the
  board). The final scalar becomes a **metric map**: the climbed metric plus the
  diagnostic and gate metrics (the metric-taxonomy schema), each carrying its
  provenance — baseline pair, seed, tree_sha, image. `measure.py` already keys
  measurements by image+command+metric+seed+tree_sha; this widens the stored
  *value* from a scalar to a map + provenance.
- **Time-series and large → object-store artifacts** keyed by run_id, with a
  pointer in the record — the loss/throughput trajectory (`metrics.jsonl`),
  checkpoints, sample outputs, and the trace. The board pulls a downsampled
  curve; the raw stays in the bucket. Not in git (churn, public history), not in
  the small state (bloat).

### Traces

Store them, in the object store, keyed by run_id, next to the result. The small
run record carries only a pointer, so kernel state stays tiny and git stays free
of multi-MB blobs. Three deliberate choices:

- **Private by default.** A trace holds prompts, tool outputs, target code, and
  possibly secrets-in-context. Public sees the *report*, never the raw trace;
  run it through the existing sanitizer chain before any sharing. The honesty
  ledger is public; the underlying traces are access-gated.
- **Bench-shaped format.** Align with hermes' `save_sample` (`conversations`) so
  one store feeds the reviewer-bench (recorded rounds already do this), the
  lessons distiller, and the self-improvement-from-memory direction. One format,
  many consumers.
- **Retention**, because these accumulate: keep merged-win traces forever (the
  archival-science claim), age out aborted/negative ones after a window or
  downsample. Same policy class as the run-dir reaper, moved to the bucket.

Because a trace streams to a local file and flushes at checkpoints + session end,
a preempted spot job still leaves the trace it got through — the same crash
safety the result artifact gets.

### Richer measurement

Beyond the headline number, store the trajectory and the metric vector, because
four things already need them: the board (per-attempt loss curves, log-y),
**compute-normalization** of the metric (the anti-gaming lever — normalize by
wall-clock/FLOPs-to-target, which needs the resource-and-progress trajectory),
the agent's depth loop (reasoning over its own curve), and **verification** (a
full trajectory resists gaming a scalar — the recurring failure mode).

Two design calls:

- **Standardize the emission; do not parse stdout.** One measurement format the
  eval harness *writes* — a `metrics.jsonl` trajectory plus a final
  `measurement.json` metric map — that the kernel ingests. Uniform across
  benchmarks and **optional/graceful**: a scalar-only benchmark still works; one
  that emits a curve gets the richer board and verification for free.
- **No metrics database, and W&B stays optional.** The system of record stays
  blob-shaped (metric map + jsonl artifacts), not a TSDB. W&B is an optional live
  view a job may also log to (config-driven, never hardcoded) — an adopter should
  not need a W&B account to run. The object store is the durable truth.

## Open decisions (for when this version comes up)

1. **Result/state transport** — object store (R2, provider-agnostic, our
   recommendation) vs. lean on each provider's native volume (Modal-style).
2. **First backend** — SkyPilot-as-meta-provider (recommendation) vs. one native
   cloud to learn the shape on.
3. **Provisioning model** — ephemeral-per-job (recommendation) vs. a warm cloud
   resident.
4. **Retention policy** for traces, curves, and checkpoints (keep wins forever;
   age out the rest).
5. **Sequencing** — traces-as-artifacts and richer measurement are useful on
   Slurm independent of cloud; decide whether they ship earlier on their own or
   ride in with the cloud backend.

## Relation to existing notes

- [external.md](external.md) names the `compute` interface (consumer-side
  runners, verifiable rewards) and the storage interface (notebook-repo →
  walled store) at a high level; this note is the deeper cloud dive on both.
- [scaling.md](scaling.md) owns the planning/multi-agent scaling program;
  unaffected — this is about *where* jobs run and *what* they store, not *which*
  jobs to run.
- The metric-taxonomy vocabulary (climbed / diagnostic / gate / composite /
  group / suite) is the schema for the measurement record above; this note is a
  place it gets used.
