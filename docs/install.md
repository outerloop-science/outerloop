# Install

This page explains how to install and deploy Outerloop to run the climber: what you need, where the loop runs, the bot identity, the operator settings file, and the less common shapes such as two instances on one account. For a first run on one machine, follow [Getting started](getting-started.md) instead.

Outerloop is self-hosted. You run it, with your keys, on your compute, against your repos. Nothing reports back to us and there is no service to sign up for.

## Requirements

- Python 3.12 or later.
- `uv` on PATH, or in the directory its installer uses. Evaluations and launches run through `uv run`, and `outerloop start` stops with a message when it cannot find it.
- A GitHub repository you administer.
- An API key for the author's model, and separate keys for the judges.
- For contained runs, Linux with [Apptainer](https://apptainer.org). macOS has no Apptainer, so runs there stay uncontained.

The orchestrator is CPU-only and makes outbound connections only. Anywhere that can reach GitHub and your model provider works.

## Deployment shapes

The first-class home is a Slurm cluster, where the loop is a chain of short jobs that resubmit themselves on a cadence you set. Nothing listens and no inbound SSH is needed, so a cluster that requires 2FA is fine. Author sessions, gate evals, experiment launches, sweeps and wakes are each their own scheduler job, placed where you say. A run that is waiting on jobs parks with no process alive, and its wake runs when they finish.

A single machine works too, running the same job scripts as subprocesses. A workstation with a GPU can climb a cheap benchmark before you point the loop at a cluster.

### The resident loop on Slurm

With `sbatch` on PATH, `outerloop start` submits a resident job named `outerloop-resident` that hands over to itself, by default with 360 minutes of walltime (`OUTERLOOP_RESIDENT_MINUTES`). It needs the state root on the shared filesystem (`OUTERLOOP_ROOT`) and a source checkout of the kernel: run `start` from a clone, or set `OUTERLOOP_HOME` to one. The deploy step moves the checkout per your update policy (see [Upgrading](#upgrading)) and syncs its environment.

`OUTERLOOP_ACCOUNT` and `OUTERLOOP_PARTITION` place the tick. Both are optional, and unset lets Slurm bill the default association and pick the default partition. On success `start` prints the job id, the logs directory and the stop and pause commands. Only one loop may own a state root, enforced by a tick lease (see [Operate](operate.md)).

### The login-node loop

Use this when no partition can start a resident tick within your cadence and your site permits a long-lived process on the login node. In `~/.config/outerloop/.env`, set:

```bash
OUTERLOOP_TICK_HOST=login
OUTERLOOP_QOS=my-qos
OUTERLOOP_APPTAINER_BIN=/path/to/apptainer
```

Then run `outerloop start` from your checkout. The loop runs in the foreground at nice +10, requires `sbatch` on PATH, and submits compute to Slurm. The QOS applies to every submitted job. The Apptainer path is used on the compute nodes, and the loop only checks that the image file exists. Empty values fall back to Slurm's default QOS and to `apptainer`. The `--tick-host login` flag overrides the environment, which overrides `.env`.

The loop refuses to start while a resident chain is queued or running, and a resident submission that finds the lease taken withdraws its job. The login loop does not auto-update, even with `OUTERLOOP_AUTO_UPDATE=main`. To restart or upgrade, stop the process, run `git pull`, run `uv sync`, and run `outerloop start` again. Settings from `.env` are exported only at launch.

### The local loop

With `OUTERLOOP_COMPUTE=local`, or with no `sbatch` on PATH, `outerloop start` runs the local loop in the foreground from the installed package. Ctrl-C stops it, and its saved records let it continue the work when you start it again. On a headless machine, run it under `nohup` or in tmux, or as a user service.

Local compute has no lanes, so GPU benchmarks need no GPU partition: evaluations and the author's launches run on the machine's own GPUs, shared first come first served. Launch arrays run their tasks in parallel as GPUs become available, and `OUTERLOOP_LOCAL_GPUS` overrides automatic detection through `nvidia-smi` (`0` disables GPU allocation). The author's session itself starts with `CUDA_VISIBLE_DEVICES` empty, so an experiment that needs a GPU goes through the author's `launch` syscall, where it is recorded and metered against `gpu_hours_per_run`. Uncontained, that is a default the session could reset; containment is what keeps the GPU out of the session, which is one more reason to run with the image.

Without a container image, sessions run under the harness's own sandbox and evaluations run bare on your machine with your keys, and the loop says so once at start. The verification panel is off in this mode unless you set `OUTERLOOP_PANEL_UNCONTAINED=1`, because an uncontained judge holds a shell next to its own key file. A pull request opened without a panel says so. A Codex or Hermes author needs the image in every mode. For priorities, walltime accounting, or several machines, use Slurm.

### One machine as its own Slurm cluster

A workstation can be its only Slurm node. On Ubuntu, once, as root:

```bash
sudo apt-get install -y slurm-wlm munge
sudo slurmd -C   # prints this machine's NodeName line: name, CPUs, memory
```

Use the node name `slurmd -C` printed, `mybox` below, in every file. In `/etc/slurm/slurm.conf`:

```
ClusterName=onebox
SlurmctldHost=mybox
SelectType=select/cons_tres
SelectTypeParameters=CR_Core_Memory
GresTypes=gpu
ProctrackType=proctrack/cgroup
TaskPlugin=task/cgroup
NodeName=mybox CPUs=64 RealMemory=250000 Gres=gpu:8 State=UNKNOWN
PartitionName=gpu Nodes=mybox Default=YES MaxTime=INFINITE State=UP
```

`/etc/slurm/gres.conf`:

```
NodeName=mybox Name=gpu File=/dev/nvidia[0-7]
```

And `/etc/slurm/cgroup.conf`, so a job allocated one GPU sees only that GPU:

```
ConstrainCores=yes
ConstrainRAMSpace=yes
ConstrainDevices=yes
```

Start the services with `sudo systemctl enable --now munge slurmctld slurmd`, check with `sinfo` and `srun --gres=gpu:1 nvidia-smi -L`, then run `outerloop init --compute slurm` with `--root` on a local directory and `--partition gpu`. init does not ask for the GPU lane, so add `OUTERLOOP_GPU_PARTITION=gpu` to `~/.config/outerloop/.env`.

## The bot identity

The agents open pull requests, comment and push branches as a GitHub identity that is not yours. The tick and the attempt hold the credential and perform every GitHub write on the agents' behalf, and a contained session cannot reach it (see [the trust boundary](how-it-works.md#the-trust-boundary)). Either kind of identity works, and `outerloop init` sets both up.

### A GitHub App

Pick `app` at init's auth prompt, or run `outerloop init --github-app` on its own. init creates the App under your account or under an organization you name, through GitHub's one-click manifest flow. It prints one URL. Open it in any browser, click **Create GitHub App**, and paste the code the page shows back into init. init then points you at the page that installs the App on the target repo and checks that the installation can write it. The browser can be anywhere, so a headless cluster works too.

init writes the App's private key and `github_app.<slug>.json` under `~/.config/outerloop/` (0600). The bot login is `<slug>[bot]`, recorded as `OUTERLOOP_BOT_LOGIN`. Tokens are minted from the key an hour at a time and scoped to the installed repos, and the App takes no seat and needs no collaborator grant. The manifest declares contents, issues and pull requests read-write, metadata, members, actions and checks read.

After a kernel upgrade that needs new permissions, run `outerloop permissions --open` and follow the page it opens: first edit the App's permissions if needed, then run it again to open the installation page and accept them. Run `outerloop permissions` to verify. It exits 0 when complete, or when using a PAT.

If you are a member, not an owner, of the organization, GitHub creates the App under your personal account instead, and a personal App does not install on an org repo by default. Make the App public in its settings, request its installation on your repo, have an org owner approve the request, then rerun `outerloop init --force --github-app`. To let the lab own the App centrally later, transfer it to the org from its Advanced settings, and the same key keeps working.

### A fine-grained PAT

For an organization that already runs a machine user, or one where you cannot create Apps, pick `pat`. Mint the token on the machine user with these settings:

- Resource owner: your organization, not the bot's personal account.
- Repository access: only the repos you opt in.
- Permissions: contents, pull requests, issues, read and write. No workflow permission.
- Expiration: 90 days, with a rotation reminder.

Add the bot as a direct collaborator with **Write** on every target repo, not to a team, which grants more than it needs and inherits future grants. Without that grant the tick cannot even read the contract and idles silently on that target. A pasted token is stored at `~/.config/outerloop/bot_pat` (0600), and `--pat-file` records a path you give. When the check against the target succeeds, init records the token's login as `OUTERLOOP_BOT_LOGIN`, and the tick does not service a target without it.

## The operator settings file

Everything above lands in `~/.config/outerloop/.env`, written by `outerloop init`, with every file owner-only (0600). `start` and the chain's deploy step refuse a file that is not owned by you or is group- or world-writable, so `chmod 600` it.

Startup settings are read when the chain starts: the mode, the tick's placement, the state root, the cadence and the tick host. Restart the chain to change them. The resident chain re-reads an allowlist of author, model, panel, image and GPU-lane settings from the file each tick, including QOS, and a present entry wins over the inherited environment even when it is empty. A change there applies to every run started after the next cadence, and to a parked run the next time its wake is submitted.

A local or login loop reads the file once at launch, and the shell's values win there. The author's backend and model are fixed per run in its record and never change on their own; the only way to change them mid-run is an explicit `outerloop rebind` request (see [Operate](operate.md)).

Every variable, its default and when it is required is in the [environment reference](reference/environment.md).

## Placement and lanes

CPU roles (ticks, author sessions) take `OUTERLOOP_ACCOUNT` and `OUTERLOOP_PARTITION`. GPU evals and author launches take the GPU lane, `OUTERLOOP_GPU_PARTITION` and optionally `OUTERLOOP_GPU_ACCOUNT`. A comma-separated partition list lets Slurm start each job wherever it fits first.

For a target-specific GPU lane, add a JSON mapping to `.env`:

```bash
OUTERLOOP_GPU_LANES='{"owner/repo":{"partition":"gpu-large","account":"my-account","gpu_type":"a100","extra":["--comment=reserved"]}}'
```

Only `partition` is required. An omitted `account` uses the CPU account, an omitted `gpu_type` preserves untyped GPU requests, and `extra` is a list of `--name=value` flags. A flag the kernel sets itself is rejected, since sbatch lets the later flag win, and so are unknown keys and malformed JSON. These are deployment settings, not contract fields.

GPU jobs request at least 8 cores and 64 GB of host memory per GPU. On nodes with less per GPU, set `OUTERLOOP_EVAL_CPUS_PER_GPU` and `OUTERLOOP_EVAL_MEM_GB_PER_GPU` to what one GPU's share of a node offers, or Slurm rejects the job as an unsatisfiable node configuration.

## The agent image

On a cluster, evals and launches run inside the Apptainer image at `OUTERLOOP_IMAGE` (default `~/outerloop-images/agent-py312.sif`), in a jail that binds only the checked-out tree. An eval that needs data must fetch it into the tree. The published image lives at [huggingface.co/outerloop-science/agent-image](https://huggingface.co/outerloop-science/agent-image), built from `containers/agent-py312.def`. On Linux with Apptainer installed, `outerloop init` downloads it (about 200 MB), verifies its published checksum and records it. `--image` points at your own, and `--no-image` keeps runs uncontained even when an image is on disk by writing `OUTERLOOP_IMAGE=`.

Only the local loop falls back to running uncontained on its own. On Slurm the tick does not service a target without an image, and its log names the setting below, so a lost image never silently drops containment. Codex and Hermes authors need the image in every mode.

### Running a cluster without a container

A Slurm cluster with no container runtime sets `OUTERLOOP_UNCONTAINED=1` in `~/.config/outerloop/.env`. Sessions and evaluations then run as local mode does without an image, but as Slurm jobs on your account. Sessions run under the harness's own sandbox. Evaluations run bare, in a throwaway tree with an allowlisted environment. The image is ignored even when one is on disk.

The local-mode rules carry over. The panel is off unless you set `OUTERLOOP_PANEL_UNCONTAINED=1`, and `start` refuses a Codex or Hermes author. A session can read any file your account can: the bot's credential, the model key files, the evaluator's tree and the kernel's state. Key secrecy and measurement integrity both weaken. Use it where the site already confines processes, or where that exposure is acceptable to you.

When Apptainer is missing or cannot run containers, init says so, prints the install steps for your system and continues uncontained, which the local loop supports as above. Run `outerloop init --force` after installing it. Check a working install with `apptainer exec docker://alpine:3.20 cat /etc/alpine-release`, which prints a version number.

- Ubuntu: the project's PPA builds amd64 and arm64. The package carries the AppArmor profile that Ubuntu 23.10 and later require.

  ```bash
  sudo add-apt-repository -y ppa:apptainer/ppa
  sudo apt-get update && sudo apt-get install -y apptainer
  ```

- Debian (x86-64): the release package from [Apptainer releases](https://github.com/apptainer/apptainer/releases), not the `-suid` one. Other architectures build from source or use the unprivileged installer.
- Fedora: `sudo dnf install -y apptainer`.
- RHEL, Rocky, Alma: `sudo dnf install -y epel-release`, then `sudo dnf install -y apptainer`.
- No root: download the project's unprivileged installer pinned to a release tag, read it, then run it.
- Slurm clusters: ask the administrators; most already provide it. A cluster without one can run [uncontained](#running-a-cluster-without-a-container).

## Model backends and keys

Authors and judges run on one of three harnesses: Claude Code (the default), Codex and hermes-agent. When the configured author's CLI is missing, `outerloop init` installs it from a pinned release and records its path. An existing executable is kept, and `--no-install-harness` skips the step. `start` checks the CLIs of every configured author, the fleet author and any target overrides, and stops the launch when one is missing. After installing or moving a CLI, run `outerloop init --force` to record its path again.

The author's key goes to `~/.config/outerloop/<backend>_key`, recorded as `OUTERLOOP_<BACKEND>_KEY_FILE`. `OUTERLOOP_CLAUDE_MODEL` is required for every Claude role when nothing more specific covers it: the author without `OUTERLOOP_AUTHOR_MODEL`, panel judges on the claude backend, and the steward. There is no built-in default, and `start` refuses to run without it, naming the line to add.

Judge and steward keys are separate files, one key per role, never the author's. `OUTERLOOP_PANEL_KEY_FILE` holds the panel's Claude key (default `~/.config/outerloop/verifier_key`, absolute when set). `OUTERLOOP_PANEL_CODEX_KEY_FILE` and `OUTERLOOP_PANEL_HERMES_KEY_FILE` have no default and are required by lenses on those backends. `OUTERLOOP_STEWARD_KEY_FILE` turns on the steward lane, which also needs a steward scope in the contract. An Anthropic key must not reach another provider's login.

For a Codex author, set an explicit model. It always runs contained, so it also needs the image. For a Hermes author, set `OUTERLOOP_AUTHOR_BACKEND=hermes` with its model, image and pinned checkout. Self-hosted endpoints and the Hermes wiring are in [endpoints](endpoints.md), and every variable is in the [environment reference](reference/environment.md).

## Billing Claude sessions to Vertex

Anthropic-billed roles can run on Claude-in-Vertex instead of an Anthropic API key, useful when GCP credits are the budget:

```bash
OUTERLOOP_VERTEX_PROJECT=your-gcp-project   # presence switches billing to Vertex
OUTERLOOP_VERTEX_REGION=global              # optional
OUTERLOOP_VERTEX_ADC=~/.config/outerloop/vertex_adc.json
```

Enable the Claude models in the project's Model Garden, mint ADC with `gcloud auth application-default login` and `set-quota-project <project>`, and place the credential at the configured path. `OUTERLOOP_CLAUDE_MODEL` must name a model the project can serve, and `OUTERLOOP_VERTEX_SMALL_MODEL` selects Claude Code's auxiliary fast model. Contained sessions get the ADC file bind-mounted read-only, and their environment carries no Anthropic key at all. Unset the project variable to fall back to API-key billing. OpenAI-backed roles are unaffected.

## Upgrading

A local install upgrades with `outerloop upgrade`, which is `pip install --upgrade outerloop-science` under one verb (`--pre` tracks pre-releases). Then run `outerloop permissions --open` and follow the page it opens, and stop and restart the loop to pick up the new code.

New formats for run records are migrated as the kernel reads them, so an upgrade needs no migration step. An older kernel cannot read the new ones: drain or end active runs before rolling back past a format change, since a rollback needs the records translated by hand.

A resident Slurm deployment runs from a checkout, and `OUTERLOOP_AUTO_UPDATE` in `.env` says whether the deploy step moves it. `off`, the default, leaves the checkout alone: you upgrade when you choose, and the loop keeps running the code you validated. `release` moves it to the newest release tag at the next cadence, pre-releases included, and the repo is public so no credential is needed. `main` follows every merge and is meant for the kernel's own developers. Whatever moves the checkout, you or the policy, the environment is synced to the commit that is checked out, or the deploy rolls back to the last commit whose environment was installed.

After any upgrade, run `outerloop permissions --open` from the checkout's environment if the new code needs permissions the App does not have yet.

## Two instances on one account

Give each instance a separate state root and operator settings file. Keep the production settings at `~/.config/outerloop/.env`, and select the second file with `OUTERLOOP_ENV_FILE` in the process environment, since a file cannot select itself:

```bash
export OUTERLOOP_ENV_FILE="$HOME/.config/outerloop-sandbox/.env"
outerloop init --root /shared/sandbox-state
outerloop start
```

Use the same exported selector for later `init`, `start`, `limits` and `harness` commands. The file must be owned by you and not group- or world-writable. The resident re-reads it each tick and successors inherit its path, while foreground loops read it once at startup. Use a separate checkout via `OUTERLOOP_HOME` if the instances need independent code updates or harness installs.

With the default settings file, jobs keep the names `outerloop-resident` and `outerloop-tick`. A different settings file gets `outerloop-resident-<12 hex>` and `outerloop-tick-<12 hex>`, a stable hash of its resolved path, so use that exact name when inspecting or stopping jobs by hand. Two instances sharing one settings file are not supported, and the selected path must stay unchanged while its chain runs. Each root has its own tick lease, heartbeat, logs and `PAUSE` sentinel. Per-user scheduler caps remain shared across both instances.

## Harness pins and upgrades

`src/outerloop/harnesses.toml` ships in the wheel and owns the harness versions and integrity pins. `outerloop harness status` inspects every harness without installing anything, reporting the kernel pin, the installed version, the path, drift against the effective pin, and any operator override.

`outerloop harness upgrade [claude codex hermes bridge]` defaults to all four; `--used` selects only the backends your deployment configures. The resident deployment runs it after a successful kernel sync, and a failure is logged without stopping the tick. Each changed harness is built under `<state-root>/harnesses/<name>/<version>/` and verified before the operator `.env` switches to its new path; a failed candidate is deleted and the old installation stays available. Each install has a 300-second deadline (`OUTERLOOP_HARNESS_TIMEOUT_SECONDS`), and failures back off from five minutes to one day. Concurrent upgrades are refused, and repeating a completed upgrade does no installation work.

To trial a release, set the version and its matching checksum in `.env` and upgrade that harness. Claude and Codex trials take `OUTERLOOP_CLAUDE_VERSION` plus `OUTERLOOP_CLAUDE_SHA256`, or `OUTERLOOP_CODEX_VERSION` plus `OUTERLOOP_CODEX_SHA256`; Codex also needs `OUTERLOOP_CODEX_CODE_MODE_HOST_SHA256` and npm, whose cache lands under the state root. Hermes trials take both `OUTERLOOP_HERMES_REF` and `OUTERLOOP_HERMES_SHA`. Missing checksums are refused. Remove the override and upgrade again to return to the kernel pin.

A site may provide its own build of a harness CLI, such as a managed `claude` that authenticates by itself. List it in `OUTERLOOP_OPERATOR_HARNESSES` and record its path as usual, for example in `OUTERLOOP_CLAUDE_BIN`. Upgrades then leave it alone, the deployment's automatic `--used` run included. `harness status` reports it as `OPERATOR`, or `OPERATOR MISSING` when the path is not executable. Keeping it current is yours, and so is running a version the kernel was not tested against.

The role's key file is still required and still passed to the session. When the CLI ignores it, fill it with a long random placeholder: the kernel redacts the key's text from transcripts, and a short one would garble them. A contained session binds only the binary into the image, so a wrapper that needs other host paths works only uncontained.

The Hermes installer needs `git` and `uv`. It verifies the pinned source and installs a runtime beside the checkout, so install it on the same OS and architecture as the session image. To upgrade an existing install, rerun `bash scripts/install_hermes.sh "$REVIEW_HERMES_REPO"` or run `outerloop init --force` with Hermes configured. If a killed installer leaves a `<checkout>.installing` directory beside the checkout, for example `~/hermes-agent.installing`, confirm no installer is running, remove that empty lock directory and retry.
