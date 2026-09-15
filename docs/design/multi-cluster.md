# Outerloop on several clusters

Status: design note, 2026-09-15, for the owner to read before anything is
built. The question: try Outerloop on a second cluster while the 0.2.0
release candidate runs on both fleets (Empire AI Alpha, NERSC Perlmutter,
possibly ALCF Polaris), and say what
multi-cluster coordination would take later. The roadmap's cross-cluster
section names the tiers; this note fills them in against the code as of
main `697b0b8` and against each cluster's public documentation.

## What a deployment is today

One deployment is one state root on a shared filesystem, one resident tick
(a Slurm job that loops and keeps one successor queued behind
`afterany:self,singleton`), one compute backend (`SlurmCompute` or
`LocalCompute`), one operator config under `~/.config/outerloop/`, one bot
identity, and one target. Everything it shares with the world goes through
GitHub: the contract, issues and their claim markers, PRs, and the target's
`research-log` branch (ledger, board, reports, the siblings' `status.json`,
research lines). Job state never leaves the cluster.

The scheduler shows in `compute.py` (`SlurmCompute` calls `sbatch`,
`sacct`, `squeue`, `sinfo`, `scancel`; `JobSpec.to_argv` writes `--time`,
`--mem`, `--gpus-per-node`, `--nice`, `--dependency`, `--begin`, `--array`;
backend selection is Slurm unless `OUTERLOOP_COMPUTE=local`), in
`scripts/tick_chain.sbatch`, `scripts/tick_resident.sh` and
`scripts/requeue_moved_successors.sh` (`singleton`, `afterany:self`,
`--begin`, `squeue`, `scontrol`, `scancel`), in `cli.py` (`start` picks Slurm
when `sbatch` is on PATH and forces `OUTERLOOP_COMPUTE=local` otherwise), and
in a few reads in `tick.py`. The container runtime shows in four modules, at
several call sites each: `dispatch.py` (launched jobs), `orchestrator.py`
(the inline evaluator measurement), `harness.py` (one builder per contained
backend) and `image.py` (the image probe), all building `apptainer exec
--containall --cleanenv ...` by hand.

## The three tiers

**Tier 1, independent deployments.** Each cluster runs its own kernel on its
own target. Two deployments on one target collide today: each allocates
`agent-01` first from its own state root and pushes `agents/<agent-id>` and
`feat/auto/<agent-id>` refs under that id, so sharing a target is tier 2.
With targets kept apart there is nothing to coordinate: each fleet reads and
writes its own target's `research-log` branch, and the two never see each
other's reports. Nothing to build beyond portability, and this is the
try-out.

**Tier 2, one target from several clusters.** Several kernels climb one
benchmark. What that needs, in order of how soon it bites:

- Agent ids unique across fleets, so two fleets' `agent-01` never collide.
  Slot ranges per deployment (`OUTERLOOP_AGENT_SLOTS=05-08`) keep ids flat
  and branch names (`agents/agent-NN`) unchanged. The range must bind every
  launch path: today issue-requested launches pass no `--agent-id` and land
  on the default `agent-01`. Each fleet publishes its range in its status
  file, and a kernel that sees another fleet's range overlap its own refuses
  to start new runs and says so, since two operators can misconfigure. A
  fleet name (`OUTERLOOP_FLEET`) rides on run ids, the board and the status
  files, so a reader can tell where a run lives; the board gains that
  attribution.
- Pacing per fleet. `max_active_attempts` and `runs_per_week` are read from
  the contract alone, and a deployment-side lower value does not preserve
  the target's ceiling: a contract allowance of ten with two fleets set to
  eight permits sixteen. The contract therefore declares the shares
  (`budgets.fleets: {torch: {max_active_attempts: 2, runs_per_week: 5},
  empire: {...}}`), each kernel enforces its own share, and the target's
  owner keeps one place that bounds total spend. New work either way.
- Claims. Publishes are safe across kernels: the ledger batch is pushed with
  an expected-head guard and PR merges go through GitHub. Issue claims are
  not, for two reasons. `pick_issue` counts only claim markers written under
  the bot's own login, so two fleets with different bot identities never see
  each other's claims at all; and even under one identity the claim is a
  read-then-post with a window in which both kernels read an issue as
  unclaimed. So either every fleet of a target runs under one bot identity,
  or claims move off comments to something identity-independent and atomic:
  a `refs/claims/<issue>` ref on `research-log` created with the refs API's
  create-if-absent, which the loser's request fails; the existing
  expected-head helper updates branch heads and does not cover this, and
  crash and release semantics for the ref are new work.
- The sibling view per fleet, with run identity. Each kernel writes
  `status/<fleet>.json` in the shared tree, readers merge, and every entry
  carries its run id and fleet (today's entries carry the agent id only).
  Self-initiated direction picking reads this view, which another fleet
  refreshes at its own cadence, so duplicate hypotheses stay possible in the
  window between two passes; that is the gap the plan-writing planner
  addresses, not a fleet-specific one.
- Messages across fleets: the section "Cross-fleet messages on one target"
  below.

**Tier 3, one kernel driving remote compute.** Still a non-goal: it needs a
file transport and cross-scheduler dependencies for no gain over tier 2.

## The clusters

Facts from public documentation as of this note; "confirm" marks what the
documentation does not say and a first login must answer.

| | Torch (today) | Empire AI Alpha | NERSC Perlmutter | ALCF Polaris |
| --- | --- | --- | --- | --- |
| Scheduler | Slurm | Slurm | Slurm | PBS Pro |
| Login | `login.torch.hpc.nyu.edu`, 2FA | `alpha.empire-ai.org` (2FA: confirm) | NERSC MFA | ALCF MFA |
| Accounts | `torch_pr_36_*` | `su_<PI>_<tag>` subaccounts; institutions also have their own partitions and accounts | `-A m<project>` | project allocation |
| GPUs | H200, L40S | 8×H100 80GB per HGX node (13 nodes, growing to 144 GPUs); Grace ARM nodes on a separate partition | 4×A100 per GPU node | 4×A100 per node |
| CPU partition for the tick | `cpu_short` (6h) | a `cpu` partition is documented for at least one institution; confirm for ours | login-node pool via `scrontab` (`cron` QOS; `workflow` QOS for long jobs) | confirm; login nodes run PBS clients |
| Containers | Apptainer on host | Apptainer, `module load apptainer` | Shifter and podman-hpc; no Apptainer | Apptainer, compute nodes only |
| Egress | outbound HTTPS from login and compute nodes | confirm | login yes; compute nodes confirm | proxy only (`http_proxy`/`https_proxy` to `proxy.alcf.anl.gov:3128`) |
| Storage | scratch, 60-day purge, 5M-inode quota | home 100 GB; `/mnt/lustre/<institution>` scratch; no project directories | `$SCRATCH` purged; `$CFS` project space | project filesystems (`-l filesystems=`) |
| Long-running login processes | login nodes are ephemeral pods; forbidden | confirm | cgroup-limited (56 GB); `scrontab` is the sanctioned way | confirm |

## What each cluster asks of the kernel

**Empire AI Alpha.** Slurm and Apptainer, the two things the kernel assumes,
so tier 1 should run with the current code. Three things to settle on the
machine: the chain must find `apptainer` (the module in the operator's
shell profile, or the explicit binary path the dispatcher already accepts);
the tick needs a CPU partition it may occupy for six hours at a time (if our
subaccount has none, see the tick-host decision below); and the `.env` must
name the GPU lane by hand, `OUTERLOOP_GPU_PARTITION` and, when it differs,
`OUTERLOOP_GPU_ACCOUNT`, because `outerloop init` asks only for the CPU
placement and GPU benchmarks are refused without a lane. GPU jobs use
`--gpus-per-node`, which is what `JobSpec` already writes.

**NERSC Perlmutter.** Slurm, so `SlurmCompute` and the chain scripts carry
over, and `scrontab` is a better home for the tick than a job chain: it
runs on the login-node pool under the `cron` QOS, recurs on a schedule, and
NERSC itself recommends `--dependency=singleton` for it, which is exactly
our chain's guard. Two builds: a container-runtime seam, since the kernel
hard-codes `apptainer exec` in four places and Perlmutter offers Shifter and
podman-hpc instead (one `ContainerRuntime` with an apptainer and a shifter
implementation behind all four sites; the image needs an OCI publication
next to the `.sif`); and
a `scrontab` mode for `outerloop start`. Whether compute nodes reach GitHub
and the model APIs directly must be confirmed; if not, the same proxy
pass-through Polaris needs applies here.

**ALCF Polaris.** The largest build, and last: a `PbsCompute` behind the
`Compute` protocol (`qsub`/`qstat`/`qdel`; `-W depend=afterany:<id>`,
`-J` arrays, `-a` for a deferred start, `-l select=1:ncpus=..:ngpus=..`,
`-l walltime=`, `-q`, `-A`, `-N`; the exact ALCF conventions to confirm on
the cluster since its documentation refuses automated reads), a
replacement for `singleton` (PBS has none; the kernel's own lease in the
state root can guard the resident instead), and proxy variables passed into
every session and job, which today's `--cleanenv` scrubs. Apptainer runs
only on compute nodes there, which suits author sessions (they are jobs)
and does not affect the tick (which uses no container).

**Common to all three: where the tick lives.** Torch's answer (a six-hour
resident job on a cheap CPU partition, chained by `singleton`) is the only
one the code knows. The alternatives are a `scrontab` entry (NERSC), a loop
process on a login node where policy allows it (the `tick --loop` that local
mode already runs, but with `SlurmCompute`), or a resident on a GPU
partition when nothing else exists (wasteful; a last resort). Two things the
kernel lacks for any of them: the tick host must be chosen separately from
the compute backend (today the local `start` forces `OUTERLOOP_COMPUTE=local`
along with the loop), and residents that `singleton` no longer serializes
need a tick-level lease in the state root (the existing lease guards one
run's wake, and tick coalescing is a timer, not mutual exclusion). So:
`OUTERLOOP_TICK_HOST=resident|scrontab|login` in `outerloop start`, with
`resident` the default that exists today, plus a tick lease. Empire AI needs
none of it if our subaccount has a CPU partition.

## The try-out: Empire AI Alpha, tier 1

1. Access. The owner logs in once and runs a probe: `sinfo` for partitions
   and their time limits, `sacctmgr show assoc user=$USER` for the
   subaccount, `module load apptainer && apptainer --version`, an outbound
   `curl -sI https://api.github.com` and one to the model API from a login
   node and from a one-minute job (the contained session on a compute node
   is what needs the model API), `python3 --version` (3.12 or newer) and
   `which uv`, the quotas on home and scratch, GPU visibility under
   `apptainer exec --nv` in a GPU job, and a two-node check that the shared
   filesystem honors the primitives the kernel leans on (atomic rename,
   `O_EXCL` creates and `flock` visible across nodes). It also asks the
   policy question the documentation does not answer: whether a long-lived
   process may sit on a login node. The probe's output decides whether
   anything in the previous section is needed before the first tick.
2. Install. A source checkout on Alpha (`git clone` and `uv sync`, as on
   Torch): in Slurm mode `outerloop start` submits `scripts/tick_chain.sbatch`
   from the checkout and refuses a bare PyPI install (shipping the chain
   script inside the wheel is a queued item). `outerloop init` writes
   `~/.config/outerloop/.env` with the subaccount, the CPU partition, the
   image path and the App file; the GPU lane is added by hand as above.
   The App: the same `outerloop-science` App can serve a second deployment
   when the target lives in the same org, or the deployment gets its own
   App through the manifest flow; the bot login on PRs is the visible
   difference. A decision below.
3. Target. Tier 1 wants a target the Torch fleet is not climbing. A copy
   of `quickstart-trial` proves the plumbing in an afternoon (small GPU
   task, cheap evals); a real benchmark follows once a tick cycle, one
   climb with a launch, a sleep and a wake, a published report and a ledger
   row have all been seen on Alpha.
4. Operate. Torch's operations run over SSH with keys from the Mac; Alpha
   needs the same, or the owner runs the probe and the start and shares the
   logs. The kernel's own evidence (tick log, run directories, the board)
   is what "seen working" means.

## Cross-fleet messages on one target

The owner asked (2026-09-15) whether `message --to agent-NN` could reach a
sibling on another cluster, and set the shape: local files stay the store,
and the kernel replicates them asynchronously to a shared medium. A second
reader (codex) reviewed the first draft against the code; its findings are
folded in here.

**Local files stay authoritative.** Every kernel keeps writing what it needs
to run its own runs into its own state root: records, leases, inboxes,
outboxes, ledgers. None of that waits on the network.

**One sync.** The tick's board pass already turns local state into files on
`research-log` and every attempt fetches that branch back. The sync
generalizes it: each fleet has an outgoing tree under its state root
(`shared/<fleet>/`) that the pass pushes, and an incoming tree that the pass
pulls and ingests (mail into the inboxes of the runs this kernel hosts,
forum posts into the brief's context, other fleets' status into the sibling
view). Mail, forum and status are directories the sync knows, not
mechanisms of their own. Today the pass is not one commit: the ledger batch
is pushed with the expected-head guard, while the status file and report
archival each go through `put_file` on their own; folding them into one
guarded commit per pass is part of this build. The medium behind the sync is
a backend, `research-log` now; a bucket later is a per-directory choice (the
forum stays on GitHub as research content; mail may move if latency asks).

**Mail is immutable; receivers keep cursors.** A message to a run on another
fleet is written to `shared/<fleet>/mail/<recipient run>/<sender run>/
<counter>.json`, the inbox envelope as-is, addressed to a run id, never to
an agent id: the sender's kernel resolves the agent to a run through the
merged sibling view at send time, so a reused slot or a later run under the
same agent id never receives mail meant for an earlier one. The receiver
keeps a cursor per sender run beside its inbox, the way it keeps a position
per GitHub collection, appends every file past the cursor through the same
`append`, and advances the cursor after the append. Nothing is deleted from
the medium: the kernel has no delete primitive on the branch, a deleted file
stays in history anyway, and a receiver deleting the sender's file would race
the sender republishing it. The sender prunes its own outgoing files after
they are acknowledged and a retention window has passed, in its own commit.

**Acknowledgments carry the caps.** Each fleet's status file publishes, per
sender run, the cursor its receivers have reached. The sender's kernel reads
it, so in-flight is counter minus acknowledged, and the per-pair cap of four
applies to in-flight mail (unacknowledged transport), which is a different
quantity from the local cap, which reads the recipient's `inbox_seq`. A
message unacknowledged past an expiry window (a recipient that ended, a
fleet that went away, a run no fleet hosts) is bounced by the sender's own
kernel as a context-only note to its author; no other kernel need act.

**Groups.** `--to all`, or a search line, is expanded by the sender's kernel
into explicit recipient run ids at send time and the list is persisted in
the delivery journal, one file per recipient, so a retry after a crash
reuses the same list instead of recomputing "all" against a changed fleet.

**Local delivery stays direct.** `deliver_messages` already resolves
recipients in its own state root; a local recipient gets the envelope
appended straight into its inbox and that path survives any outage. Only a
recipient the merged view places on another fleet takes the mail path.

**Crash windows, each with a test.** Sender: local write of the outgoing
file, then push at the next pass (the file is durable, the push retries).
Receiver: pull, append, cursor advance (a crash after append re-appends on
the next pass and `append` dedupes within that inbox by the global key
`agent-msg:<sender run>:<n>`; a crash before append re-reads). Medium: two
fleets committing at once (the expected-head retry; with several fleets the
pass may need a bounded retry loop within a tick so a fleet is not starved
until its next cadence). Ids: an overlap of slot ranges could make two
distinct messages share a key; the overlap check above is what prevents it.

**Forum.** A forum post is publication, not delivery: anything a run that
starts next week should read cannot live in inboxes. Posts go to
`shared/<fleet>/forum/<topic>/<message_id>.json`, every fleet pulls the
whole `forum/` tree, the brief inlines the newest posts as it inlines
reports, a `reports`-style verb browses them, nothing wakes. Retention: posts
older than a window are compacted into a digest, the way reports distill
into lessons, so the tree and the fetch stay bounded.

**Latency, honestly.** One to two cadences when both fleets and GitHub are
healthy. A recipient that started after the sender's kernel last pulled the
view is not in it yet and the message is refused with a note saying to try
again next leg. Conflicts, outages and a recipient parked on long jobs all
stretch it; arrival in the inbox is not the agent reading it.

**Load and ceilings.** A commit through the API costs one blob request per
file plus a tree, a commit and a ref update; a pass with a handful of files
is a dozen requests, against an App budget of five thousand an hour.
Attempts fetch `research-log` in full at every wake today, so history growth
from mail and forum is paid by every wake: a shallow or blob-filtered fetch
for that branch is a work item before the volume exists. The ceilings, in
order: commit contention with many fleets (the bounded retry above);
cadence latency; GitHub as the one medium for cross-fleet traffic (an
outage delays it while local delivery and every job continue).

**Persistence.** The same sync can replicate a run's durable artifacts
(report, transcripts, launch ledger, inbox history) to an artifact store, a
bucket, for backup and cross-cluster forensics. It never replicates records
and leases: they churn every tick and a stale copy elsewhere is a hazard. A
deployment with no shared filesystem at all replaces the filesystem
primitives themselves through the storage interface compute-cloud.md
describes; that is outside this note.

**What a GitHub outage does today, for the record.** Launched jobs and the
wakes that carry their results continue; a wake whose origin fetch fails
logs it and resumes on the local clone; the outbox holds public posts. But a
failed contract fetch idles the launch lanes for that tick, and a publish
that raises (a network failure included) ends the run as `aborted` with
`publish-error`, the branch left on the remote. Runs mid-submit during an
outage are therefore at risk now, before any of this design. A GitHub outage
latch mirroring the model-API latch (classify the failure, re-park, retry,
spend no attempt) is a small hardening PR and comes first.

**Build.** The slot-range setting and its overlap check on every launch
path; contract fleet shares; per-fleet status with run ids and
acknowledgment cursors, merged on read; one guarded commit per pass; the
outgoing and incoming trees with push and pull; receiver cursors; the remote
branch in `deliver_messages` with run-id addressing and persisted group
expansion; expiry bounces; sender-side pruning; the forum verb and digest;
a shallow fetch of `research-log` for attempts; a tick lease for
non-singleton tick hosts; the GitHub outage latch. Tests for each crash
window above. It waits for a second fleet to share a target, which in turn
waits for the Empire AI tier-1 try-out, except the outage latch, which
waits for nothing.

## Decisions for the owner

Settled on 2026-09-15: local files stay authoritative, local delivery stays
direct, and one generic sync replicates the shared tree to `research-log`
for cross-fleet mail, forum and status, with the medium swappable per
directory later. Still the owner's:

1. **Tick host.** Accept `OUTERLOOP_TICK_HOST=resident|scrontab|login` with
   a tick lease, `resident` staying the default? Every non-Torch cluster
   without a cheap CPU partition needs it; Empire AI may not.
2. **Bot identity.** One App for every fleet of a target is now the
   recommendation, because claim markers count only under the bot's own
   login; per-deployment Apps need identity-independent claims first.
3. **Order.** Empire AI first (nothing to build if the probe is clean),
   NERSC second (container seam and `scrontab`), ALCF last (PBS backend).
4. **Pacing.** Contract-declared fleet shares whose sum is the target's
   ceiling, enforced per kernel; the earlier "each deployment sets a lower
   value" does not hold the ceiling.
5. **Addressing.** Slot ranges with the overlap check (recommended) or
   fleet-prefixed ids.
6. **Mail semantics.** Immutable files, receiver cursors, acknowledgments in
   the status file, in-flight cap of four, expiry bounce by the sender's
   kernel, sender-side pruning after a retention window (recommended); the
   windows are numbers to pick.
7. **Forum placement and retention.** Permanently on GitHub as research
   content, compacted into digests after a window.
8. **The outage latch first.** A publish that meets a GitHub failure today
   ends the run aborted; fix that before any messaging code.

## Sources

- Empire AI Alpha: University at Buffalo CCR guide, https://docs.ccr.buffalo.edu/en/latest/howto/empireai/ ; Mount Sinai Minerva guide, https://labs.icahn.mssm.edu/minervalab/documentation-new-york-states-empire-ai/ ; Cornell call for proposals, https://ai.cornell.edu/empire-ai-cornell-call-for-compute-resource-proposals
- NERSC: scrontab, https://docs.nersc.gov/jobs/workflow/scrontab/ ; containers, https://docs.nersc.gov/development/containers/ ; running jobs, https://docs.nersc.gov/systems/perlmutter/running-jobs/ ; resource usage policies, https://docs.nersc.gov/policies/resource-usage/
- ALCF Polaris (indexed titles only; the pages refuse automated reads): containers, https://docs.alcf.anl.gov/polaris/containers/containers/ ; running jobs, https://docs.alcf.anl.gov/polaris/running-jobs/ ; PBS qsub options, https://docs.alcf.anl.gov/running-jobs/not_in_nav/pbs-qsub-options-table/
