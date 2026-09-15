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

The scheduler shows in five places: `compute.py` (`SlurmCompute` calls
`sbatch`, `sacct`, `squeue`, `sinfo`, `scancel`; `JobSpec.to_argv` writes
`--time`, `--mem`, `--gpus-per-node`, `--nice`, `--dependency`, `--begin`,
`--array`), `scripts/tick_chain.sbatch` and `scripts/tick_resident.sh`
(`singleton`, `afterany:self`, `--begin`, `squeue`), `cli.py` (`start` picks
Slurm when `sbatch` is on PATH), and a few reads in `tick.py`. The container
runtime shows in four: `dispatch.py` (launched jobs), `orchestrator.py` (the
inline evaluator measurement), `harness.py` (author and judge sessions) and
`image.py` (the image probe), each building `apptainer exec --containall
--cleanenv ...` by hand.

## The three tiers

**Tier 1, independent deployments.** Each cluster runs its own kernel on its
own target. Two deployments on one target collide today: each allocates
`agent-01` first from its own state root and pushes `agents/<agent-id>` and
`feat/auto/<agent-id>` refs under that id, so sharing a target is tier 2. The
only coordination is what GitHub already provides: a second kernel reads the
same `research-log` branch, so its briefs carry the other fleet's reports.
Nothing to build beyond portability, and this is the try-out.

**Tier 2, one target from several clusters.** Several kernels climb one
benchmark. What that needs, in order of how soon it bites:

- A fleet name in every deployment (`OUTERLOOP_FLEET`, for example `torch`
  or `empire`), carried on run ids, agent branches (`agents/<fleet>/agent-NN`,
  as the research-lines note anticipated) and the board, so two fleets'
  `agent-01` never collide.
- Pacing per fleet. `max_active_attempts` and `runs_per_week` are per
  target in the contract; with N kernels they multiply by N. Either the
  contract declares a share per fleet, or each deployment sets its own
  lower value under the contract's ceiling. The second keeps the contract
  fleet-agnostic and puts the knob where the operator of that cluster is.
  Neither exists yet: `tick.py` reads both values from the contract alone,
  so either shape is a build.
- Publishes are safe across kernels: the ledger and board are pushed with an
  expected-head guard, and PR merges go through GitHub. Issue claims are
  not: `pick_issue` reads an issue's comments and then posts the claim
  marker, so two kernels can both read it as unclaimed inside that window
  and start duplicate work. Tier 2 needs an atomic claim; the cheapest is a
  git ref on `research-log` (`refs/claims/<issue>`) pushed with the same
  expected-head guard, which GitHub rejects for the loser. Self-initiated
  direction picking reads `status.json` on `research-log`, which a second
  fleet refreshes at its own cadence, so duplicate hypotheses are possible
  in the window between two ticks. That is the same gap the plan-writing
  planner (agent-protocols.md, stage 3) addresses; it is not fleet-specific.
- Messages across fleets. `message --to agent-NN` resolves the recipient in
  the local state root, so a sibling on another cluster is unreachable.
  First version: the kernel refuses with the usual context-only note
  ("agent-05 runs on another fleet"). Second version: GitHub as the bus, a
  `mail/<fleet>/<run>/` directory on `research-log` that every tick polls
  and appends into the recipient's inbox, delivered at the next wake like
  any message. That is the kernel-to-kernel item agent-protocols.md defers;
  it needs no new transport, only a poll and the existing inbox.

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
partition when nothing else exists (wasteful; a last resort). This is a
deployment choice, not a kernel one, so it belongs in `outerloop start`:
`OUTERLOOP_TICK_HOST=resident|scrontab|login`, with `resident` the default
that exists today.

## The try-out: Empire AI Alpha, tier 1

1. Access. The owner logs in once and runs a ten-line probe: `sinfo` for
   partitions and their time limits, `sacctmgr show assoc user=$USER` for
   the subaccount, `module load apptainer && apptainer --version`, an
   outbound `curl -sI https://api.github.com` from a login node and from a
   one-minute job, `python3 --version`, and the quotas on home and scratch.
   The probe's output decides whether anything in the previous section is
   needed before the first tick.
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

## Decisions for the owner

1. **Tick host.** Accept `OUTERLOOP_TICK_HOST` as the shape, with the
   resident chain the default, `scrontab` for NERSC, and a login-node loop
   only where the cluster's policy allows it? This is the one build that
   every non-Torch cluster touches.
2. **Bot identity per fleet.** One App for all fleets of the lab, or one
   App per deployment? One App is simpler; per-deployment Apps make the
   fleet visible in every PR and let an institution revoke one without the
   others.
3. **Order.** Empire AI first (nothing to build), NERSC second (container
   seam and `scrontab`), ALCF last (PBS backend). Or a different order if an
   allocation's clock is running.
4. **Pacing per fleet (tier 2).** Contract-declared shares, or each
   deployment's own lower value under the contract's ceiling. The second
   is recommended: the contract stays fleet-agnostic.
5. **Cross-fleet messages (tier 2).** Refuse-with-a-note first, GitHub as
   the bus second, as sketched above; both wait until two fleets share a
   target.

## Sources

- Empire AI Alpha: University at Buffalo CCR guide, https://docs.ccr.buffalo.edu/en/latest/howto/empireai/ ; Mount Sinai Minerva guide, https://labs.icahn.mssm.edu/minervalab/documentation-new-york-states-empire-ai/ ; Cornell call for proposals, https://ai.cornell.edu/empire-ai-cornell-call-for-compute-resource-proposals
- NERSC: scrontab, https://docs.nersc.gov/jobs/workflow/scrontab/ ; containers, https://docs.nersc.gov/development/containers/ ; running jobs, https://docs.nersc.gov/systems/perlmutter/running-jobs/ ; resource usage policies, https://docs.nersc.gov/policies/resource-usage/
- ALCF Polaris (indexed titles only; the pages refuse automated reads): containers, https://docs.alcf.anl.gov/polaris/containers/containers/ ; running jobs, https://docs.alcf.anl.gov/polaris/running-jobs/ ; PBS qsub options, https://docs.alcf.anl.gov/running-jobs/not_in_nav/pbs-qsub-options-table/
