# Install

Outerloop is **self-hosted**. You run it, with your keys, on your compute,
against your repos. Nothing reports back to us and there is no service to sign
up for.

There are two things you can turn on, in this order. Level 1 takes about five
minutes and needs no bot account, no cluster, and no GPU. Do that first.

---

## Level 1 — advisory PR reviews (~5 minutes)

An automated reviewer comments on your pull requests. It never approves, never
blocks a merge, and never fails your build.

**You need:** an API key for the reviewer's model. Anthropic by default; the OpenAI and OpenRouter variants are below. That's it.

**Step 1 — add the key as a repository secret.** Repo → Settings → Secrets and
variables → Actions → New repository secret. Name it `ANTHROPIC_REVIEWER_KEY`.
A spend-capped key is strongly recommended.

**Step 2 — add this file** to the repo you want reviewed, at
`.github/workflows/review.yml`:

```yaml
name: advisory-review
on:
  pull_request_target:
    types: [opened, reopened, labeled]
permissions:
  contents: read
  pull-requests: write
jobs:
  advisory:
    # a labeled event only runs for the outerloop:review label (manual re-review)
    if: github.event.action != 'labeled' || github.event.label.name == 'outerloop:review'
    uses: outerloop-science/outerloop/.github/workflows/advisory-review-agent.yml@main
    with:
      bot_login: my-bot            # PRs by this login are never reviewed
    secrets:
      anthropic_reviewer_key: ${{ secrets.ANTHROPIC_REVIEWER_KEY }}
```

**Step 3 — open a pull request.** A comment appears within a minute or two.

Every review runs through the least-token split: the model session holds
read-only permissions; a separate posting job holds the write token. Each
round names its reviewer in the stamp.

**Optional — a second, independent opinion from an open-model backend.** Add
one more job to the same file (and an `OPENROUTER_API_KEY` repo secret) —
same reusable, different backend. Each opinion posts its own labeled round:

```yaml
  second-opinion:
    if: github.event.action != 'labeled' || github.event.label.name == 'outerloop:review'
    uses: outerloop-science/outerloop/.github/workflows/advisory-review-agent.yml@main
    with:
      bot_login: my-bot
      backend: hermes
      model: openai/gpt-5.6-terra  # any OpenRouter id
      opinion_label: second opinion — terra
    secrets:
      openrouter_api_key: ${{ secrets.OPENROUTER_API_KEY }}
```

To run the same hermes opinion **directly against the OpenAI API** (no
OpenRouter platform fee), add `hermes_provider: openai` under `with:`, pass
`openai_reviewer_key: ${{ secrets.OPENAI_REVIEWER_KEY }}` in `secrets:`
instead of the OpenRouter key, and use the provider-NATIVE model id
(`model: gpt-5.6-terra` — no `openai/` prefix).

For `backend: codex` instead: pass `openai_reviewer_key: ${{ secrets.OPENAI_REVIEWER_KEY }}`
in `secrets:`, and set `model` to a Codex model id (or omit it for the codex
default) — an OpenRouter id will not work there.


A third opinion is one more block with a distinct `opinion_id`. If the backend's
key is missing or expires, each triggering PR gets a visible "could not run"
stub naming that opinion — a dead key is never silent.

That's the whole setup. Notes:

- `bot_login` is **required** — the reviewer refuses to run without it, because
  that's how it knows never to review its own (or your bot's) pull requests.
  If you have no bot yet, any placeholder login works.
- **If you forked this repo**, add `reviewer_repo: your-org/outerloop` under
  `with:` — otherwise your fork's changes never run.
- **Pin the version in production**: `reviewer_ref: v0.1.0` (or a commit SHA).
  The default `main` moves.
- Silence it on one PR with the `outerloop:no-review` label. (A repo that already
  uses the `autoresearch:*` labels keeps working — both prefixes are recognized.)
- Fork PRs are skipped by design: they must not reach your API key.
- Nothing from the pull request is ever executed. The workflow checks out the
  reviewer, not your PR's code.

**If no comment appears**, open the workflow run and read the log — the
reviewer logs why it stopped (missing key, skipped PR, model refusal) and
always exits successfully so your PR stays green.

---

### Optional — a weekly maintenance digest

The same reviewer, pointed at your whole repository once a week: dead code,
duplicated logic, oversized modules, stale pins, slow tests, repeated work on
the hot path, documentation drift. It writes one issue, titled "Maintainer
digest — <date>" (the date of the digest it shows), and replaces its body each
scan. It changes no code and opens no work orders.
Add `.github/workflows/maintenance.yml` with the same secret as the reviewer:

```yaml
name: maintenance
on:
  schedule:
    - cron: '17 6 * * 1'   # weekly; or run it from the Actions tab
  workflow_dispatch:
permissions:
  contents: read
  issues: write
concurrency: maintenance
jobs:
  digest:
    uses: outerloop-science/outerloop/.github/workflows/maintenance-agent.yml@main
    secrets:
      anthropic_reviewer_key: ${{ secrets.ANTHROPIC_REVIEWER_KEY }}
```

The `backend`, `model` and `hermes_provider` inputs select the model as they
do for the reviewer; `lenses` picks which sections run; `bot_login` names the
account the digest is posted as when it is not the workflow's own token. Every
run scans the default branch's head, whatever ref a manual run was started
from. Items marked
**Decision** need your call; the rest are mechanical and can be given to an
agent as work orders (the steward, Level 2).

## Level 2 — the benchmark climber

The agent proposes improvements to your code and opens PRs when a benchmark
improves. This needs a bot identity and somewhere to run experiments.

### 2a. Write a contract

`.outerloop.yaml` at your repo root declares what "better" means and where
the agent may write:

```yaml
benchmarks:
  - name: my-benchmark
    command: uv run python -m mypkg.eval --json
    metric: success_rate
    direction: max          # max = higher is better, min = lower is better
budgets:
  gpu_hours_per_run: 8
  runs_per_week: 10
scope:
  allowed: [src/, tests/]   # the ONLY paths the agent may write
roadmap: docs/roadmap.md
```

**Check it before you push:**

```bash
uv run python -m outerloop.contract_cli .outerloop.yaml
```

It prints what the agent would be allowed to do, or exactly what is wrong.

The optional knobs — paired seeding and the significance floor
(`seed_env`, `min_delta`/`min_delta_rel`), dispatched and GPU evals
(`eval_minutes`, `gpus`), `baseline: paired|cached`, the depth budgets
(`depth_k`, `sleep_k`), width and pacing (`max_active_attempts`,
`attempt_cooldown_minutes`), a stewardship scope, and `merge: manual|auto` —
are listed in [docs/contract.md](contract.md); the schema's own docstrings
(`src/outerloop/contract.py`) are the reference. A GPU benchmark needs a
dispatched eval (`eval_minutes` above the in-job threshold) and a cached
baseline needs a positive floor — the validator says so — and for GPU
benchmarks `gpu_hours_per_run` is a real meter: launches and evals draw on
it (CPU benchmarks meter nothing).

`budgets.review_topup` grants extra room once, when the run first opens a PR:
`launches` adds experiment launches to `depth_k` (default 2, range 0–16),
`sleeps` adds sleeps to `sleep_k` (default 4, range 1–32), and `gpu_hours`
adds GPU-hours to `gpu_hours_per_run` (default 0.5, nonnegative).
The meter keeps all prior spend; every wake states the top-up and remaining budget.

Authors can submit in review: a credited verdict fast-forwards the PR head
only when the sealed commit contains its current head and auto-merge is
confirmed disarmed. Gate and panel verdicts return as inbox messages. Edits
in review are measured and pushed only when submitted. Submit needs neither
a prior launch nor a report (`--report` is optional). With resumable compute,
a stop without a submit ends unmeasured; on an open PR it returns to review.
Backends without resume continue to measure at finish.


Two things to get right:

1. Your `command` must be **deterministic and re-runnable**, and print the
   metric. That is what lets your own CI re-verify any improvement the agent
   claims, instead of taking its word.
2. `scope.allowed` should be as narrow as the work requires. Three paths are
   never writable no matter what you put there: the contract itself, your
   roadmap, and `.github/`.

For one artifact evaluated across many benchmarks (rather than independent
solvers), add a suite aggregate so a change is judged on the whole suite:

```yaml
suite:
  metric: mean_success_rate
  direction: max
```

### 2b. Create a bot identity

The agents open pull requests, comment, and push branches as a GitHub
identity that is not yours. `outerloop init` sets it up (step 2c); two kinds
are supported. Either way the credential is the kernel's: the tick and the
attempt hold it and perform every GitHub write on the agents' behalf, and a
contained session cannot reach it — the session's environment is an
allowlist that carries no key paths, and the container binds only the
workspace. Uncontained local mode shares your machine with the session; the
local-mode note in 2c says what that means.

**A GitHub App — the default.** Pick `app` at the prompt (or run
`outerloop init --github-app` on its own). init creates an App under your
account or under an organization you name, through GitHub's one-click
manifest flow:

1. init prints one URL. Open it in any browser — a headless cluster works
   too, there is no localhost and no tunnel — and click **Create GitHub App**.
2. Paste the code the page shows back into init. It writes the App's private
   key and `github_app.<slug>.json` under `~/.config/outerloop/` (0600).
3. init points you at the page that installs the App on the target repo, then
   checks that the installation can write it.

The bot login is `<slug>[bot]`; init records it as `OUTERLOOP_BOT_LOGIN`.
Tokens are minted from the key an hour at a time and scoped to the installed
repos; the App takes no seat and needs no collaborator grant. The manifest
declares Contents, Issues and Pull requests read-write, Metadata read and
Members read (so a private org member's issue reads as MEMBER, not CONTRIBUTOR),
and Actions and Checks read. Existing installations must accept the two new
permissions, `actions: read` and `checks: read`, before check results flow.
`outerloop upgrade` prints the exact pages when permissions are missing: first
edit the App's permissions if needed, then accept the change on its installation.
The page shapes are:

- Edit: `https://github.com/organizations/<owner>/settings/apps/<slug>/permissions`
  for an organization-owned App, or `https://github.com/settings/apps/<slug>/permissions`
  for a user-owned App.
- Accept: `https://github.com/organizations/<account>/settings/installations/<installation id>`
  for an organization installation, or `https://github.com/settings/installations/<installation id>`
  for a user installation.

Until then, the sweep logs a warning and delivers nothing
for checks. If the install step was cut short, run
`outerloop init --force --github-app` to finish and re-check it.

**If you are a member, not an owner, of the organization.** Creating an App
*owned by the org* needs org-owner rights, so init falls back to creating one
under your personal account — and a personal App does not install on an org
repo by default. You do not need an org-owned App or a shared key:

1. In the App's settings, make it **public** (a private App installs only on
   its owner's account, which is why the org repo is not offered).
2. Request its installation on your repo; an **org owner approves** the request.
3. Run `outerloop init --force --github-app` to record the installation.

Your key never leaves your machine. To let the lab own the App centrally later,
transfer it to the org from the App's Advanced settings — the same key keeps
working, so nothing has to be recreated. init prints these steps itself when it
detects the personal-App fallback.

**A fine-grained PAT — the fallback.** For an org that already runs a machine
user, or one where you cannot create Apps: pick `pat` (or pass `--pat-file`).
Mint the token on the machine user with these settings:

- Resource owner: **your organization** (not the bot's personal account —
  this is the step people miss)
- Repository access: only the repos you opt in
- Permissions: contents, pull requests, issues — **read and write**;
  **no workflow permission**
- Expiration: 90 days, with a rotation reminder

Then add the bot as a direct collaborator with **Write** on every target repo
(org members can be added directly; don't add it to a team, which grants more
than it needs and inherits future grants). Without that grant the tick cannot
even read the contract and idles silently on that target. A pasted token is
stored at `~/.config/outerloop/bot_pat` (0600); `--pat-file` records the path
you gave. Once the token checks out against the target, init records its
login as `OUTERLOOP_BOT_LOGIN`; if the check could not run (no network), it
says so — rerun `outerloop init --force` online, or set the login in the
`.env` yourself, since the tick does not service a target without it.

### 2c. Run the loop

When the configured author's CLI is missing, `outerloop init` installs it from
a pinned release and records its path. An existing executable is kept.
Use `--no-install-harness` to skip this step. To install by hand from the
outerloop checkout, run `bash scripts/install_claude.sh [target_path]` or
`bash scripts/install_codex.sh [target_path]`, then rerun `outerloop init --force`.
The default target is `$OUTERLOOP_<BACKEND>_BIN`, else `~/.local/bin/<backend>`.
Claude 2.1.272 is pinned for Linux x64 (glibc/musl) and ARM64; other platforms
are refused. Installation needs `curl`, `sha256sum`, and a writable target
directory. Hermes remains a review backend, provisioned with
`bash scripts/install_hermes.sh [target_dir]`.

**Host prerequisites for model backends.** From the outerloop checkout:

- Claude, as author or reviewer: the pinned Claude Code CLI; install with
  `bash scripts/install_claude.sh`.
- Codex, as author or reviewer: the pinned Codex CLI; install with
  `bash scripts/install_codex.sh`.
- Hermes, as reviewer only (not an author backend): the pinned hermes-agent
  source checkout, run through `uv`; install with `bash scripts/install_hermes.sh`.

`init` records the absolute Claude
or Codex path found on PATH (or in `~/.local/bin`) as `OUTERLOOP_<BACKEND>_BIN`.
`start` checks only the configured author's CLI: the recorded path takes
precedence, otherwise it searches PATH, then `~/.local/bin`. A missing or non-executable CLI stops
launch before any job runs. After installing or moving it, run
`outerloop init --force` to record its path again. `--dry-run` prints the launch
command without checking the CLI. For Hermes, set `REVIEW_HERMES_REPO` to the installed checkout (the installer
defaults to `~/hermes-agent`); contained review sessions bind its source read-only.

The quickest path is the guided setup:

```bash
outerloop init      # asks for compute, target repo, placement, auth, the Claude model, and the author's key
outerloop start
```

`init` asks for the compute backend, the target repo, placement, the
identity from 2b, the Claude model (`OUTERLOOP_CLAUDE_MODEL`, required for
every Claude role), and the author's model key, and writes
`~/.config/outerloop/.env` (all `0600`) — everything the prose below otherwise
sets by hand. The rest of this section documents what it writes, for when
you'd rather set it directly.

The orchestrator is CPU-only and makes outbound connections only. Anywhere
that can reach GitHub and your LLM provider works.

- **Slurm cluster**: the tick can live in the queue as a self-resubmitting
  job — no daemon and no inbound SSH, which matters when your cluster requires
  2FA.
- **A VM or workstation**: `outerloop start` runs the local loop in the
  foreground. It needs `uv` on PATH or in its installer's directory, and stops
  with a message when it is in neither.
  Ctrl-C stops it; its saved records let it continue the work when
  you start it again. On a headless machine start it under `nohup` or in a
  tmux window, or as a user service.

**Updates.** A resident Slurm deployment runs from a checkout, and
`OUTERLOOP_AUTO_UPDATE` in `.env` says whether the deploy step moves it. `off`,
the default, leaves the checkout alone: you upgrade when you choose, and the
loop keeps running the code you validated. `release` moves it to the newest
release tag at the next cadence (pre-releases included; the repo is public, so
no credential is needed). `main` follows every merge and is meant for the
kernel's own developers. Whatever moves the checkout, you or the policy, the
environment is synced to the commit that is checked out, or the deploy rolls
back to the last commit whose environment was installed. The local loop runs
the installed package, which has no such policy: run `outerloop upgrade` to move
it to the newest release (add `--pre` to track pre-releases), then start it
again. That is `pip install --upgrade outerloop-science` under one verb.

#### Login-node loop

Use this when no Slurm partition can start a resident tick within your cadence
and the site permits a long-lived process on the login node. In
`~/.config/outerloop/.env`, set:

```bash
OUTERLOOP_TICK_HOST=login
OUTERLOOP_QOS=priority
OUTERLOOP_APPTAINER_BIN=/cm/local/apps/apptainer/current/bin/apptainer
```

Run `outerloop start` from your checkout with the usual shared `OUTERLOOP_ROOT`.
`--tick-host login` overrides the environment, which overrides `.env`.
`resident` is the default on Slurm; local compute still runs locally.
The login loop requires `sbatch` on PATH, runs in the foreground at nice +10,
and submits compute to Slurm. QOS applies to all submitted jobs; the Apptainer
path is used on compute nodes. The tick only checks the image file exists and
does not run Apptainer on the login node. Empty QOS uses Slurm's default;
an empty binary setting uses `apptainer`.

Only one loop may own a state root. Every foreground loop, local or login,
holds the root's `TICK` lease (holder `host:pid`, heartbeat and the holder's
cadence written each tick); a file lock refuses a second loop while the owner
runs. `outerloop start` refuses any loop while the lease is held, and a login
loop while a resident chain is queued or running; a resident submission that
finds the lease taken meanwhile withdraws its job, and a login loop that
finds a resident queued after taking the lease stops. After a crash the next
start on the same host takes the lease at once, and a start from another
host once the heartbeat is three of the holder's cadences old (file locks may
not reach across nodes); a loop whose record was overwritten from another
node stops at its next heartbeat. Ctrl-C or SIGTERM releases the lease. Use
tmux or a user service if the process should survive your terminal.

The login loop does **not auto-update**, even with `OUTERLOOP_AUTO_UPDATE=main`.
To restart or upgrade: stop the process, run `git pull`, run `uv sync`, then
run `outerloop start` again. Settings from `.env` are exported only at launch.

### Upgrading

1. Run `outerloop upgrade` (add `--pre` for pre-releases).
2. Run `outerloop permissions --open` and follow the page it opens. For an App
   missing configured permissions, save the edit, then run the command a second
   time to open the installation page and accept them. If the App is already
   configured, it opens the accept page directly. Run `outerloop permissions`
   to verify: it exits 0 when complete (or using a PAT), 1 for missing permissions
   or a lookup failure. Without `--open`, it prints both settings URLs in order
   when permissions are missing; their shapes are listed in the App section above.
3. Stop the running loop, then restart it with `outerloop start`.

`outerloop upgrade` is for a local install. On Slurm the resident tick runs
the checkout under `OUTERLOOP_HOME` and moves it as `OUTERLOOP_AUTO_UPDATE`
says (or pull it by hand); then run `outerloop permissions --open` from that
checkout's environment. The sweep's log names the same pages until the
permissions are accepted.

A Slurm deployment coming from 0.1 whose resident or chain still runs under a
pre-rename job name cancels it first (`scancel --name autoresearch-resident`,
or `scancel --name autoresearch-tick` for the per-cadence chain) and then runs
`outerloop start`. `start` and the chain refuse a second loop on one root only under the
current name, `outerloop-resident`.

Experiments run wherever your `compute` backend says. Slurm is the first
backend; the interface is small (submit a job, poll for completion), so a CI
runner, a cloud backend, or a hardware rig plugs in the same way.

The deployment is configured by environment (`OUTERLOOP_*`). Placement and paths are set
when the chain is started: `OUTERLOOP_ACCOUNT`/`OUTERLOOP_PARTITION`
place the CPU jobs (ticks, author sessions; both are optional, unset lets
Slurm bill the default association and pick the default partition),
`OUTERLOOP_HOME`/
`OUTERLOOP_ROOT` locate the checkout and the state, `OUTERLOOP_IMAGE`
the container, and `OUTERLOOP_PAT_FILE` the token when the identity is a PAT.
The rest is re-read from `~/.config/outerloop/.env` each
tick, so changes take effect at the next cadence: `OUTERLOOP_TARGET`
names the repo being climbed; `OUTERLOOP_GITHUB_APP_FILE` is the App from 2b
(one identity or the other, never both); `OUTERLOOP_BOT_LOGIN` is the login the kernel
posts as (`init` records it on both auth paths; there is no default, and the
tick does not service a target without it); `OUTERLOOP_GPU_PARTITION` (optionally
`OUTERLOOP_GPU_ACCOUNT`) is the lane for GPU evals and launches — a
comma-separated partition list lets Slurm start each job wherever it fits
first; `OUTERLOOP_PANEL` names the verify/review lenses (with
`OUTERLOOP_PANEL_*_KEY_FILE` for their keys); the author backend is
`OUTERLOOP_AUTHOR_BACKEND`/`OUTERLOOP_AUTHOR_MODEL`.
`OUTERLOOP_CLAUDE_MODEL` names the model for every Claude role (author,
panel judges, steward) and is required whenever the deployment runs one:
there is no built-in default, and `start` refuses without it, naming the
line to add; `OUTERLOOP_AUTHOR_MODEL` overrides it for the author. The
author key file is
`OUTERLOOP_<BACKEND>_KEY_FILE` (`OUTERLOOP_CLAUDE_KEY_FILE`,
`OUTERLOOP_CODEX_KEY_FILE`; `init` writes the key to
`~/.config/outerloop/<backend>_key`, 0600). A Codex author always runs contained,
so it also needs the image
(`OUTERLOOP_IMAGE`) and a Codex model in `OUTERLOOP_AUTHOR_MODEL`. On a
cluster, evals run inside the Apptainer image at `OUTERLOOP_IMAGE` (default
`~/outerloop-images/agent-py312.sif`) in a jail that binds only the
checked-out tree — an eval that needs data must fetch it into the tree, and
GPU jobs are requested per node (`--gpus-per-node`). The tick has three
scheduling knobs. `OUTERLOOP_CADENCE_MIN`, read from the `.env`, is how often the
chain ticks (minutes; default 30). Two finer ones are read from the tick's own
environment (set at launch, not the per-tick `.env`): `OUTERLOOP_MIN_TICK_MINUTES`
coalesces ticks that land too close together (0 disables; unset defaults to the
lesser of 10 minutes and half the cadence), and `OUTERLOOP_MAX_JOB_MINUTES` caps
the walltime the tick requests for the climb and author-sleep wake jobs it sizes
(clamped under a code ceiling).

**Three operator switches.** `touch <root>/PAUSE` stops tick work except the
heartbeat; jobs already running continue. The resident chain drains on it (it
cancels its successor and exits), so after `rm <root>/PAUSE` run `outerloop
start` there; the per-tick chain and the local loop resume on their own at the
next tick.
`touch <root>/DISARM_WAKE` stops wake delivery by the sweep and by queued wake
jobs until `rm <root>/DISARM_WAKE`, while fresh launches, ending records, and
the chain keep running (the starting environment's `OUTERLOOP_DISPATCH_WAKE=0`
also disarms wakes; it is not read from `.env`).
`touch <root>/HOLD_LAUNCHES` stops fresh intake, self-initiated, and steward runs
until `rm <root>/HOLD_LAUNCHES`, with no chain restart, while the sweep, wake and
message delivery, GitHub polling, self-merge sweep, board, and ending records
continue; existing runs keep spending, including their panels, author sessions,
and the authors' own `launch` submissions.

**Local mode without an image.** On a machine with no Apptainer image,
`OUTERLOOP_COMPUTE=local` still runs. Sessions run under the harness's own
sandbox, evaluations run bare in a throwaway tree under an allowlisted
environment, both on your machine with your keys, and the loop says so once
at start. Local compute has no lanes, so GPU benchmarks need no
`OUTERLOOP_GPU_PARTITION`: evaluations and the author's launches run on the
machine's own GPUs. The author's session itself starts with no GPU visible on
any backend (`CUDA_VISIBLE_DEVICES` is empty), so an experiment that needs one
goes through `launch`, where it is recorded in the run's ledger and metered
against `gpu_hours_per_run`. Uncontained, that is a default the session could
reset; contained, the session has no GPU device at all, which is one more
reason to run with the image. The verification panel is off in this mode unless you set
`OUTERLOOP_PANEL_UNCONTAINED=1`, because an uncontained judge holds a shell
next to its own key file; a pull request opened without a panel says so. A
Codex author needs the image in every mode. Contained local mode needs
Apptainer and the image: the published one lives at
[huggingface.co/outerloop-science/agent-image](https://huggingface.co/outerloop-science/agent-image),
built from `containers/agent-py312.def`. On Linux with Apptainer installed,
`outerloop init` downloads it to `~/outerloop-images/` (about 200 MB, with a
progress bar), verifies its published checksum and records it; `--image` points at
your own, `--no-image` keeps runs uncontained even when an image is already on disk
(it writes `OUTERLOOP_IMAGE=`, the off-switch). When Apptainer is missing or cannot
run containers, init says so, prints the install steps for your system, and
continues uncontained; run `outerloop init --force` after installing it.

**One machine with several GPUs.** Local mode shares the machine's GPUs across
jobs first come first served, and launch arrays run their tasks in parallel as
GPUs become available. `OUTERLOOP_LOCAL_GPUS` overrides automatic detection via
`nvidia-smi`; set it to `0` to disable GPU allocation. CPU-only arrays run in
parallel up to the machine's CPU count. Submissions still return only when every
task has finished. For priorities, walltime accounting, or several machines,
use Slurm. A workstation can be its only node:

Ubuntu, once, as root:

```bash
sudo apt-get install -y slurm-wlm munge
sudo slurmd -C            # prints this machine's NodeName line: name, CPUs, memory
```

Use the node name `slurmd -C` printed (below, `mybox`) in every file. In
`/etc/slurm/slurm.conf`, the `NodeName` line is the printed one plus `Gres=gpu:N`:

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

and `/etc/slurm/cgroup.conf`, so a job allocated one GPU sees only that GPU
(without `ConstrainDevices` every job sees all of them):

```
ConstrainCores=yes
ConstrainRAMSpace=yes
ConstrainDevices=yes
```

`sudo systemctl enable --now munge slurmctld slurmd`, check with `sinfo` and
`srun --gres=gpu:1 nvidia-smi -L`, then run `outerloop init --compute slurm` with
`--root` on a local directory and `--partition gpu`. GPU benchmarks and author
launches take their lane from `OUTERLOOP_GPU_PARTITION`, which init does not ask
for: add `OUTERLOOP_GPU_PARTITION=gpu` to `~/.config/outerloop/.env`. Apptainer on
the same machine is installed as described below.

**Installing Apptainer (Linux, one time, needs root or an admin).** Apptainer runs
sessions and evaluations in containers. Check for it
with `apptainer exec docker://alpine:3.20 cat /etc/alpine-release`; a version number
means it works.

- *Ubuntu.* Install from the project's PPA, which builds for amd64 and arm64. The
  package carries the AppArmor profile Ubuntu 23.10 and later require; the
  unprivileged installer does not, and fails at run time with
  `Could not write info to setgroups`.

  ```bash
  sudo add-apt-repository -y ppa:apptainer/ppa
  sudo apt-get update && sudo apt-get install -y apptainer
  ```

- *Debian (x86-64).* Install the release package from
  [github.com/apptainer/apptainer/releases](https://github.com/apptainer/apptainer/releases)
  (`apptainer_<version>_amd64.deb`, not the `-suid` one):

  ```bash
  curl -fsSLO https://github.com/apptainer/apptainer/releases/download/v1.5.3/apptainer_1.5.3_amd64.deb
  sudo apt-get install -y ./apptainer_1.5.3_amd64.deb
  ```

  The release has no package for other architectures; on ARM Debian, build from
  source or use the unprivileged installer below.

- *Fedora.* `sudo dnf install -y apptainer`.
- *RHEL, Rocky, Alma.* `sudo dnf install -y epel-release`, then `sudo dnf install -y apptainer`.
- *No root.* The project's unprivileged installer works on most other systems. It is
  pinned to the release tag; download it, read it, then run it:

  ```bash
  curl -fsSLO https://raw.githubusercontent.com/apptainer/apptainer/v1.5.3/tools/install-unprivileged.sh
  less install-unprivileged.sh && bash install-unprivileged.sh ~/apptainer
  export PATH=$HOME/apptainer/bin:$PATH
  ```
- *Slurm clusters.* Ask the administrators; most already provide it.
- *macOS.* No Apptainer; runs stay uncontained (a macOS containment is on the roadmap).

---

## Billing Claude sessions to GCP credits (Vertex AI)

Anthropic-billed roles (authors, panel judges, reviewers on the claude
backend) can run on Claude-in-Vertex instead of an Anthropic API key —
useful when GCP credits are the budget. Config-driven, one env owner:

```bash
OUTERLOOP_VERTEX_PROJECT=your-gcp-project   # presence flips vertex ON
OUTERLOOP_VERTEX_REGION=global              # optional (default: global)
OUTERLOOP_VERTEX_ADC=~/.config/outerloop/vertex_adc.json  # ADC file
```

`OUTERLOOP_CLAUDE_MODEL` in `~/.config/outerloop/.env` (required for every
Claude role) must name a model ID your Vertex project has access to; it is
what Claude authors, panel judges, reviewers, and stewards run, and an
explicit role model or `OUTERLOOP_AUTHOR_MODEL` still takes precedence.
`OUTERLOOP_VERTEX_SMALL_MODEL` selects Claude Code's auxiliary fast model and defaults to the session model.

Enable the Claude models in the project's Model Garden, mint ADC
(`gcloud auth application-default login` +
`set-quota-project <project>`), and place the credential at the configured
path. Contained sessions get the ADC file bind-mounted read-only; the
session env then carries no Anthropic key at all. Unset the project var to
fall back to API-key billing. OpenAI-backed roles (codex/hermes) are
unaffected — those models are not on GCP.

## Safety defaults

On by default. Think hard before changing any of them:

- By default the bot never merges and is never a code owner — your branch
  protection applies to it like any contributor. `merge: auto` is an explicit
  per-repo opt-in: the kernel sweep merges a clean PR only at the head its
  gate and panel approved, through your required checks (strict up-to-date).
  It never arms GitHub auto-merge in this mode and withdraws existing arms.
  A changed head comes back to the author as a message.
- Agent sessions run with no credentials in their environment; pushes happen
  after the session ends.
- Only maintainer-authored issues and comments become tasks. Everything else,
  including PR descriptions and diffs, is data — never instructions.
- Budgets (tokens, dollars, GPU-hours, PRs per week) are enforced in code. A
  run that hits a cap dies.
- A pause file in the state branch stops the loop from anywhere with write
  access — no cluster login needed.

## Getting help

Open an issue. If you're reporting something the agent did, include its run
report — every run writes one, success or failure.

### Lifecycle deployment

Runs appear as `running`, `parked`, or `ended` on the board and in logs.
An open PR is a link on a parked run. Comments and base moves wake that run
through the ordinary wake job; there is no separate follow-up job. Runs
sleeping on jobs receive comments after those jobs finish.

Update the fleet by commit. Before deploying this stage, check every fleet's
`legacy follow-up records: N` tick line and require zero. Old records migrate
on read; an older kernel cannot read the new state names. The incompatibility
is confined to the state field, so rollbacks need state translation.
