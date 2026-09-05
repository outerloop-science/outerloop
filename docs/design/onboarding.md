# Onboarding: the simplest path from zero to a climbing fleet

Adoption is a design constraint now, not an afterthought: the public flip
positions Outerloop as something other groups install and run. This note
designs the whole funnel — GitHub identity, credentials, compute — around
one wizard and two compute modes, so the landing page's setup panel is
honest when it says three steps:

1. **`init`** — one wizard mints the GitHub App, writes the config, and
   validates the host (two browser clicks and one paste).
2. **A contract** — add `.outerloop.yaml` + an eval ruler to the target
   repo.
3. **Start** — one `sbatch` (cluster) or one foreground process
   (workstation).

## What the wizard does

`python -m autoresearch.init` (interactive; every answer has a flag for
non-interactive use):

- **Detect compute.** `sbatch` on PATH → Slurm mode (prompt for account and
  partition, verify with a real `sbatch --test-only`); otherwise local mode
  (no placement questions at all).
- **Mint the GitHub identity.** The **App manifest flow**: the wizard prints
  a GitHub URL carrying our manifest (name, description, the least-privilege
  permission set from [github-app-auth.md](github-app-auth.md), webhook off);
  the operator clicks *Create App* in their org, GitHub redirects with a
  one-time code, the operator pastes it back; the wizard exchanges the code —
  **the conversion response carries the App ID and the private key in-band**,
  so the wizard writes `github_app.json` + the PEM straight to
  `~/.config/outerloop/` (0600) on the host that will use them. No browser
  download, no file shuffling between machines. It then prints the install
  URL; after the operator installs the App on their repos, the wizard
  discovers the installation id itself (App JWT → `GET /app/installations`).
  A pre-existing PAT path is accepted as the alternative for orgs that
  prefer it.
- **Collect the author key.** A hidden paste for the configured backend,
  written to `~/.config/outerloop/<backend>_key` (0600) and recorded as
  `OUTERLOOP_<BACKEND>_KEY_FILE`; `--author-key-file` for an existing file.
- **Write `~/.config/outerloop/.env`** — only keys the operator chose;
  the tick chain's allowlist is the contract for what matters.
- **Doctor.** Re-run every check the tick already preflights, plus the
  wizard-level ones, and print a pass/fail table: git reachable with the
  minted token, key files present and 0600, image present (offer
  `apptainer pull` from the published registry), shared-FS/state root
  writable, Slurm placement accepted, contract readable on the target.
  The doctor is re-runnable on its own (`init --doctor`) and is the first
  thing support asks for.
- **Print the start command.** Nothing starts implicitly. The command is
  `autoresearch start` on both paths: it submits the resident tick where
  `sbatch` exists and runs the local loop elsewhere, taking the root and
  placement from flags, the environment, or the `.env` the wizard wrote.

The wizard invents no new validation: every check calls the same functions
the tick preflights with, so the doctor and the tick cannot disagree
(the `_panel_preflight_error` precedent).

## Compute modes

The `Compute` protocol already has both implementations; onboarding adds
**selection** and a **local chain**, nothing more.

- `OUTERLOOP_COMPUTE=slurm` (default) — unchanged: the self-resubmitting
  sbatch chain, every role its own job.
- `OUTERLOOP_COMPUTE=local` — `LocalCompute` at the two construction
  sites (tick, attempt). Jobs run as subprocesses of the tick process,
  **synchronously**: `submit` returns with the job already terminal.

### What local mode degenerates to (by design)

Local mode is the **monolith**: one process, one thing at a time. This is
simultaneously the zero-Slurm on-ramp and the paper's Karpathy-loop
ablation cell (width 1 × serialized) — the same kernel, the compute seam
swapped, nothing else different.

- **The chain is a loop.** No sbatch → `autoresearch start` runs
  `tick --loop`, a tick every cadence in the foreground. State stays in records on
  disk, so killing and restarting the loop resumes exactly like the Slurm
  chain surviving a dead tick.
- **Serialization is the semantics, not a bug.** A tick that launches a
  climb blocks until the session ends; a launch syscall blocks the
  orchestrator until training ends. Width is effectively 1 regardless of
  the contract. The landing-page taste run and the ablation both want
  exactly this.
- **No dependency jobs.** `arm_wake` submits afterany wake jobs — under
  local compute the dependencies are already terminal when `submit`
  returns, and a synchronous wake would recurse into the park. Local mode
  skips arming; the next loop iteration's sweep delivers every wake. Wake
  latency = the cadence, as the pre-#184 fleet ran.
- **Containment is best-effort.** With apptainer present, sessions run
  contained exactly as on the cluster. Without it (macOS), sessions run
  `--uncontained` — acceptable for an operator's own machine and keys, and
  the doctor says so out loud rather than hiding it.

## Stages

1. **Kernel enablement** (this note's PR series, part 1):
   `OUTERLOOP_COMPUTE` selection at the two `SlurmCompute()` sites,
   `tick --loop`, local-mode wake-arming skip, per-mode required-env
   relaxation (no account/partition in local mode), and the one launch
   command `autoresearch start` (`src/autoresearch/cli.py`). Done.
2. **The wizard**: `autoresearch.init` — detection, prompts, `.env` writer,
   doctor (PAT path first).
3. **The manifest flow**: App mint + installation discovery inside the
   wizard.
4. **Docs + landing page**: QUICKSTART.md that is literally the three
   steps, and the landing page's setup panel quoting it.

Each stage lands as its own reviewed PR; the wizard is useful after stage
2 even before the manifest flow exists (PAT path).

## Non-goals

- No hosted service, no telemetry, no accounts: onboarding ends at the
  operator's own hardware and their own GitHub org.
- No second config system: the wizard writes the same `.env` the chain
  already reads, and the allowlist in `tick_chain.sbatch` remains the
  single statement of which keys the chain honors.
- Local mode does not simulate Slurm (no fake queue, no fake dependencies);
  it embraces the synchronous degenerate case.
