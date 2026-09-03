# Changelog

All notable changes are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [SemVer](https://semver.org).

## [Unreleased]

### Changed

- A blocking follow-up re-read wakes the author: the panel's findings are
  recorded on the run and the tick submits a follow-up for them like any
  other wake; the revision is re-measured and re-read, bounded by two rounds
  before the findings are left to a human.
- `AUTORESEARCH_BOT_LOGIN` names the login the kernel posts as; every role
  defaults to it (the bot account's name when unset), so a GitHub App cutover
  flips identity and credential together.
- A follow-up that pushes a re-measured code change now has the SAME
  verification panel re-read the new head, posted on the thread: a clean
  read under `merge: auto` re-blesses the pushed sha so the tick may arm the
  merge once GitHub reports the PR clean; blocking findings, a degraded read,
  a manual dial, or a panel that could not run leave the merge to a human,
  each named. Author follow-up jobs carry the climb's `--panel` and one
  read's walltime (`docs/design/orchestrator-verify.md`).
- A signature-clean base sync re-arms auto-merge when the merge dial is
  `auto` in BOTH the measured and merged worlds (auto at publish means the
  panel ran; auto now means the owner still wants it) — the clean sync
  restores exactly the freshness that withheld arming at publish. Any other
  combination still leaves the merge to a human.
- `AUTORESEARCH_COMPUTE=local` runs the whole loop without a cluster: jobs
  become synchronous subprocesses (`LocalCompute` at every seam), Slurm
  placement env is not required, park-time wake arming yields to the sweep,
  and `tick --loop` is the foreground chain. The zero-Slurm on-ramp and the
  serialized-monolith ablation are the same switch (`docs/design/onboarding.md`).
- The base-sync skip compares benchmark MEASUREMENT SIGNATURES — all
  benchmark fields except the pure workflow dials (lines, depth_k, sleep_k,
  display_digits) — instead of whole-model equality: the dials steer the
  loop, never what a measured number means. Live motivation: the
  `lines: true` flip sits inside the benchmark stanza, so the equality
  check refused the exact sync it was built for.
- A base sync whose merge changes only base-owned content pushes without a
  re-measure — the solver and eval surface are bit-for-bit what was measured,
  so the numbers stand (the topology rule, extended one rung). And the sync
  cursor now spends only on REMOTE progress: a merge that exists solely in
  the workspace (a withheld re-measure) leaves the head re-wakeable.
- A PR left BEHIND by a base move now wakes its author the way a conflicted
  one does: merge the base in (cleanly — no resolving), reconsider the
  conclusion, and the result is re-measured before pushing. Publish already
  declined to arm auto-merge on a stale claim; this closes the loop so the
  claim can become fresh again instead of sitting until a human notices.
- The kernel can authenticate as a GitHub App: set `AUTORESEARCH_GITHUB_APP_FILE`
  to a JSON config (`app_id`, `installation_id`, `private_key` path) and every
  role mints short-lived installation tokens instead of reading the bot PAT
  (`docs/design/github-app-auth.md`). Unset, the PAT path is unchanged.
  Redaction now also covers every installation token minted this process,
  including refreshes after a secrets tuple was captured. The signer needs the
  new `app-auth` extra (`cryptography`).
- Attempts start with the target's research memory: the newest reports from the `research-log` branch are inlined in the brief, the full archive is materialized in the channel, and `syscall reports` lists it (one line each) or prints full reports, several per call. Works across clusters: any kernel climbing the target reads the same branch.
- Every role can search and read the web (Claude: WebSearch/WebFetch; Codex: `--search`; hermes: the `web` and `search` toolsets) — literature and documentation are part of research.
- A park now submits its own wake job, which waits on the park's jobs and
  starts the moment they finish (previously: the next sweep, a grace
  window, and another sweep — about 30 minutes). The sweep remains the
  fallback, and it replaces a wake job Slurm can never start. A lease held
  by a live Slurm job is no longer reaped by age.

### Fixed

- `AUTORESEARCH_BOT_ALIASES` names the kernel's former logins, and every
  "is this ours" check (own issues, claims, alarms, own PRs, own comments)
  recognizes them, so an identity flip does not turn the kernel's own
  research-log issue into a research order or hide its earlier claims.
- Tick successors no longer carry a Slurm `--deadline`: under congestion the
  scheduler cancels a deadline job on its own start estimate, which killed
  every successor within minutes of deploying it. A successor moved off its
  requested partition is handled by the running tick's requeue.
- Jobs the site moves off their submitted partition no longer stall the kernel:
  the tick chain cancels and requeues a moved successor (it starved on a
  lower-tier partition and blocked its twin through `singleton`), and the
  sweep treats an armed wake sitting on a foreign partition as lost and
  redelivers it.
- Before each deploy, the tick job removes git lock files older than ten
  minutes when no git process is working in the checkout.
- Dispatched wakes on research lines no longer read the line's memory files
  as out-of-scope deletions (the run's base is the line tip that carries them;
  the sealed candidate excludes them); the panel's claim diff omits them too.
- GPU-hours for an author's launches are charged at the declared walltime when dispatched and reconciled when the wake gathers them: unused walltime is refunded (a sweep that dies in its first minutes no longer costs its full declared hours).
- An author that concludes after an errored gate eval ends the attempt on that error; only a resubmit runs the eval again.
- A tree the gate already turned down in this attempt is never measured again: when the woken author concludes (or resubmits untouched), that verdict stands and the attempt ends on it — no second eval pair, no GPU-hours. An errored eval is still retried.

### Added

- Board v2: training curves behind every attempt (parsed from the gate eval's output, overlaid for the newest attempts, log-scale toggle), hypotheses summarized to a sentence with a link to each full report, and the live strip shows each agent's own working direction.
- The climb board: `CLIMB.md` (numbers + attempts table), `climb/<benchmark>.json` (the data), and `climb.html` (a chart of the JSON) on the target's `research-log` branch, kernel-published as runs end. Idempotent: rows merge by run id, files are written only on change.
- SWEEPS: `launch --array K` fans one command out to K jobs, each with
  `SWEEP_INDEX=0..K-1` in its environment, delivered back as K results
  (artifacts under `results/<name>/<i>/`) in one wake. It counts as one
  launch against `depth_k` and K times the
  walltime against GPU-hours. No Slurm array involved, so every backend and
  the hedged lanes work unchanged.
- `baseline: cached` (per benchmark; default `paired`): the base tree is
  measured once per (benchmark, base sha) into a target-wide cache and reused
  by every attempt on that base, so a gate runs only the candidate — half the
  eval spend for a benchmark whose baseline is effectively deterministic. The
  comparison is unpaired, so the contract must declare a calibrated floor
  (the loader insists), and a credited result says which cached measurement
  (and seed) its delta was taken against.
- THE AUTHOR DECLARES ITS EVAL WALLTIME, AND PAYS FOR IT: `submit --minutes
  <N>` sizes the submit's paired gate evals (default: the contract's
  `eval_minutes`), and `gpu_hours_per_run` is now METERED at the syscall for
  GPU benchmarks — every launch (minutes x GPUs) and every submit (2 evals x
  walltime x GPUs) draws on it, an over-budget request is refused with the
  numbers, and `status`/wake text show GPU-hours remaining. The metric stays
  steps; compute is priced by the researcher who chose to spend it, so a
  slower-per-step candidate is no longer killed by a limit sized to the
  baseline. The dispatcher's ceiling becomes a 24h backstop.
- GPU BENCHMARKS: a per-benchmark contract field `gpus` (default 0) sizes
  every dispatched job of that benchmark — gate measures and author
  launches — with `--gpus-per-node=N` on the job (the per-job `--gpus` form
  is rejected by Torch's submit plugin as a "CPU job" on a GPU partition —
  the first fleet speedrun attempt aborted on it) and `--nv` on the jail. GPU jobs go
  to a dedicated lane (`AUTORESEARCH_GPU_PARTITION`, optional
  `AUTORESEARCH_GPU_ACCOUNT`); a `gpus > 0` benchmark on a deployment with no
  lane is refused at launch rather than queued into evals that can never
  run; GPU benchmarks must dispatch (the contract rejects an in-job
  `eval_minutes`), and stewardships on them are refused for now (their
  validation runs in-job). The dispatched-eval ceiling rises from 240 to
  300 minutes for the ~3.5h single-GPU evals of the speedrun target.
- GPU evals are sized per GPU (8 cores, 64 GB each) — a training eval's
  data loading and torch.compile workers do not fit the CPU eval's 4 cores /
  8 GB; explicit larger requests are never shrunk.

- THE WIDTH DIAL: `budgets.max_active_attempts` runs N self-initiated
  attempts abreast on one target (default 1 — today's serial behavior).
  Each slot gets its own agent identity (`agent-01..agent-0N`) threaded
  via `--agent-id`, keeping branches, ledger rows, and reports distinct;
  pending markers become per-slot (the legacy un-suffixed marker from a
  pre-width deploy is still read and attributed); live markers and
  non-stranded active records both occupy slots; resumed runs inherit
  identity from their record. With `attempt_cooldown_minutes: 0` this is
  the RSI-era "cluster hot" configuration — `runs_per_week` and
  `gpu_hours_per_run` remain the spend guards.

### Added

- `attempt_cooldown_minutes` joins the contract budgets: the per-benchmark
  self-initiated cooldown becomes a target-owner dial. Unset keeps the 6h
  default (right for a standard research repo); an RSI/speedrun target sets
  0 for back-to-back re-dispatch, with `runs_per_week` as the spend guard.
  Still serial per target until the width dial lands.

### Added

- The merge-policy dial: a contract-level `merge: manual | auto` knob
  (default `manual`, unchanged behavior). In `auto`, a gate+panel-clean PR
  merges itself — the publish arms GitHub auto-merge, or merges directly
  when nothing is pending to arm against; the manual-mode review-required
  guard deliberately does not apply (the target owner opted in, and the
  gate — contract floor, suite phase, panel taste — binds before publish).
  The base-moved decline holds in BOTH modes: a stale claim never
  self-merges. Repo prerequisites for `auto` are documented on the field.

### Fixed

- The gate now enforces the contract's declared significance floor
  (`min_delta`/`min_delta_rel`): the decide previously applied only the
  global relative default (0.5%), so a benchmark's calibrated noise floor
  was consulted by the followup comparison path but never by the publish
  gate — yolo#16 shipped at +0.0379 against a declared 0.04 floor. A delta
  inside the floor now ends `no-improvement` with the reason on the record;
  benchmarks that declare no floor keep today's behavior. Same-seed pairing
  removes pool noise, not training stochasticity — which is exactly what a
  declared floor is calibrated to. Specific no-improvement reasons now
  survive to the record instead of being overwritten by generic framing.
  Auto-mode prerequisite #1 (docs/design/headline.md) done.

### Added

- Every terminal report now lands in a browsable ledger on the target repo,
  via a STATE-driven tick service (terra #170: wiring publisher calls at
  terminal sites missed four terminal paths — the service instead publishes
  any ended run whose report lacks a published marker, catching every
  terminal past and future by construction, steward and sweep-aborted runs
  included, and retrying after crashes). The full redacted report becomes
  `reports/<date>-<run_id>.md` on a `research-log` branch; a two-line
  pointer routes to the run's claimed issue never (it already got the full
  finish post), else an open order issue naming the benchmark, else a
  rolling "Research log" issue whose number is cached in the state dir (no
  pagination scans, no duplicate creation; single-tick execution removes
  the concurrency races). The pointer posts only after a successful archive
  — no dead links. Pre-feature history is adopted silently, browsable
  without backfill spam.

### Added

- The verify lens's rubric gains the code owner's aggregation standard
  (set closing yolo-jepa#16): a delta that clears the significance floor
  only as a MIXTURE of individually sub-floor tweaks is a finding — a
  publishable improvement needs an identifiable mechanism that clears the
  floor on its own. New `aggregation` category in the verify taxonomy.

### Fixed

- A jobless checkpoint sleep no longer inherits the 12h QUEUE slack: the
  park deadline added `PARK_QUEUE_SLACK_MIN` (12h, sized to keep the sweep
  from cancelling healthy-but-queued Slurm jobs) to every park — including
  an author checkpoint sleep with nothing in any queue, turning "wake at
  the first deadline pass" into a 12h coma (observed live on the yolo
  heldout_probe run, 2026-08-27). A launch-less author-sleep park now gets
  a near-term deadline (`CHECKPOINT_SLEEP_SLACK_MIN`, 45 min) and the
  existing blind-park deadline branch delivers the wake; parks with queued
  jobs keep the full queue slack, and the sweep gains no new predicate
  (terra #166: a wake-on-sight predicate over-matched blind re-parks and
  long sleeps).

### Removed

- The review-path hermes CACHE machinery, wholesale (provision job, shared
  hermes-provision workflow, restore/save steps, callers' actions:write
  ceilings). The evidence was all one way: no cache entry ever committed
  (pull_request_target runs appear cache-write-blocked regardless of
  token permissions — the "unable to reserve" message masks it), and the
  machinery caused two review outages in one night (cross-repo `./`
  resolution; a hard-coded @main nested reference running mutable
  privileged code under pinned callers — terra #164). What it optimized
  is a 5-7s runner-side anonymous clone that backoff-retry already made
  reliable across the 5-lens fan-out (two live wide rounds). Sessions
  keep the sha-verified clone with retries — the proven path. If clone
  cost ever matters again, provision the cache from a scheduled (non-PR)
  workflow per repo, which is the only event class that can write it.

### Fixed

- Cross-repo review callers (jepa-agent, egolearn) died at
  `startup_failure` the moment the nested provision job landed: a
  `uses: ./...` local reference inside a workflow that was itself called
  cross-repo does not resolve for the caller. The nested
  `hermes-provision.yml` reference is now fully qualified (`@main`).
  autoresearch's own rounds resolved the local path fine, which hid the
  breakage from this repo's reviews.

### Fixed

- `AUTORESEARCH_TARGET` joins the tick chain's `.env` allowlist, making the
  launch target a LIVE knob like its siblings. It was only ever inherited
  from the chain-start environment, so a `.env` edit could not retarget the
  fleet — the tick kept servicing the default (pilot) while review-sweeping
  other targets' records, which is why closing yolo's PR never produced a
  dispatch there.

### Removed

- Audit kill-list: `Compute.submit_after` (no production caller — wake jobs
  build their `afterany` dependency directly), the never-populated
  `RunRecord.wake_job_id` field (old records still load; unknown keys are
  ignored), and orphaned test scaffolding (`QueueEvaluator` in test_climb).

- The `autoresearch.climb` compat shim (added in #156 for jobs queued before
  the climb->attempt rename): its window — jobs submitted pre-rename still
  pending in Slurm — closed within a day of the deploy (climbs cap at 6h and
  the tick drains every 15 min). `-m autoresearch.attempt` is the only entry.
  Same class: the tick's legacy `climb-terminal-seen` kill-stamp fallback
  (its window was one in-flight KillWait grace across the deploy).

### Fixed

- Session-planted git filter drivers are neutralized on every orchestrator
  `Workspace.git` invocation: host global/system config never loads, and any
  `filter.*` driver in the workspace's session-writable `.git/config` is
  overridden to a passthrough — a checkout can no longer execute an
  agent-configured smudge/clean command with the orchestrator's permissions
  (the containment the dispatched job script already had, now on the shared
  git surface too; mutation-tested).

- The hermes Actions cache now actually commits: cache SAVES require
  `actions: write`, which the model-running session jobs deliberately never
  hold — the service refused their reservations (masked as "unable to
  reserve... another job"), so no session save ever committed, in any repo.
  Provisioning is ONE shared reusable workflow (`hermes-provision.yml`),
  modelless and the sole holder of `actions: write`: restore, clone the pin
  (sha-verified), commit the cache. autoresearch's review.yml calls it ONCE
  before the lens fan-out (a cold cache costs one clone total — terra #162,
  all five lenses agreed); the agent workflow nests it so single-call repos
  provision with zero wiring (no-op on a warm cache). Sessions are
  restore-only with the anonymous clone+retry as fallback — being UNABLE to
  write the shared cache replaces save-ordering as the poisoning defense.
  Callers and the reusable's own top-level block grant the `actions: write`
  ceiling (terra's advisory: the reusable's block would have capped its own
  provision job); session jobs still downscope to read-only.

### Fixed

- Wide-round lens sessions no longer die on GitHub's clone rate limit. The
  wide first round fans out several hermes (terra) lens sessions at once, and
  GitHub 429s the concurrent ANONYMOUS clones of the same public repo
  (observed live: 3/5 lens sessions died at exit 128 before the model ran).
  Fix: (1) the pinned clone is CACHED — and because the review workflows run
  on `pull_request_target`, every run on every PR shares the base-branch
  cache scope, so one round populates it for all future PRs (cold = first
  run on a fresh tag only); (2) a backoff+jitter retry rides out the
  anonymous rate limit on a cold fan-out. The clone is deliberately
  ANONYMOUS: the job's `GITHUB_TOKEN` is a repo-scoped installation token,
  and git-over-HTTP rejects a foreign-repo credential outright (observed
  live on the first post-merge run: six identical "could not read Username"
  failures), so for a foreign public repo anonymous succeeds where the
  token cannot. The cache is
  written by an explicit `actions/cache/save` placed BEFORE the session step
  and verified against the pinned commit sha on restore (wipe + re-clone on
  mismatch) — a post-job save would have let a prompt-injected session
  poison the shared tree every later run executes with the reviewer key. A
  genuine clone failure still degrades to the advisory missing-repo stub.
  Also fixes the sha pin itself (here and in `scripts/install_hermes.sh`):
  hermes v-tags are ANNOTATED, so `ls-remote` had yielded the tag OBJECT's
  sha — every verify comparing `rev-parse HEAD` (a commit) against it would
  have always failed, stubbing out all hermes lenses and making the Torch
  installer refuse. Pins are now the dereferenced `^{}` commit sha, and the
  installer's three paths (fresh / idempotent / tamper re-pin) were executed
  live against the real repo (a 222 MB clone — the cache earns its keep).

### Changed

- The author brief is de-prescriptified (Mengye, "don't dictate the
  contract"; research-loop.md, author-directed): the Task's `expected_effect`
  and `done_criteria` shift from directives to ORIENTATION. The metric line is
  now a fact ("mean_tour_length (lower is better), currently 10.84" — or "no
  score recorded yet"), not a target; the finish is stated as the AUTHOR's
  call ("You decide when your result is worth publishing — a negative result
  reported clearly is a success"), with the gate/suite framed as how a claim
  is verified rather than a bar to clear. The gate itself is unchanged — it,
  never the brief, is the real bar; naming a target in the brief only invited
  optimizing that number. Rendered labels: "Metric:" / "Finishing:".

- Completed the climb->attempt vocabulary in the limits/contract surface:
  `climb_job_minutes`->`attempt_job_minutes` (contract budget field),
  `CLIMB_JOB_MINUTES_FLOOR`/`CLIMB_OVERHEAD_MINUTES`/`MAX_CLIMB_JOB_MINUTES`
  ->`ATTEMPT_*`, and the `EffectiveLimits` field + `_BOUNDS` key. The old
  contract field name is accepted as a TRANSITIONAL pydantic alias so the two
  live contracts (pilot, yolo-jepa) migrate at leisure; the alias drops once
  they have.

### Changed

- Vocabulary: the author-role activity is an **attempt** (a type of run), the
  substrate stays **run**. `climb.py`→`attempt.py`, `climb_once`→
  `attempt_once`, `live_climb`→`live_attempt`, `resume_climb`→`resume_attempt`,
  `ClimbResult`→`AttemptResult`, `LiveClimbOutcome`→`AttemptOutcome`; the
  general-substrate names become `RunParked`/`RunConfig`, while `RunRecord`,
  `run_id`, `resume_run`, and park/wake are unchanged. The `-m
  autoresearch.attempt` CLI verb replaces `-m autoresearch.climb`. Deploy-safe:
  the renamed module and the tick's argv land together (the tick pulls at
  tick-start), a `load_record` shim maps a pre-rename record's `climb_job_id`
  to `run_job_id`, and the kill-stamp reader honors a legacy
  `climb-terminal-seen` — so an in-flight run started before the rename still
  wakes and ends correctly.

### Added

- Hermes container mode: `HermesHarness` runs under apptainer like the other
  backends when an image is set — workspace and per-run home bound, the
  pinned hermes-agent clone read-only, the project venv and uv cache in the
  per-run home, key and uv vars via `APPTAINERENV_*` (never argv). With
  containment uniform, the panel gate admits `hermes` lenses: the shelled-
  judge rules (image required; the judge's OWN key, neither the author's nor
  the claude panel key) move into one shared helper covering codex and
  hermes, mirrored in the tick preflight; `AUTORESEARCH_PANEL_HERMES_KEY_FILE`
  + `REVIEW_HERMES_*` ride the .env allowlist, and the chain provisions the
  pinned clone (`scripts/install_hermes.sh`) when the panel names hermes.

### Added

- Wide first round, narrow convergence (docs/design/reviewer-infra.md): on
  PR open, the advisory review fans out three distinct-lens terra opinions
  (credentials & containment; the deployment chain end-to-end; test honesty)
  as emit-only sessions, and a SUMMARIZER role merges them into the one
  posted round — dedup, blocking first, lens attribution, and every rejected
  finding listed with its reason (never dropped silently). Label-triggered
  convergence rounds stay a single full-rubric session. Mechanically:
  `REVIEW_LENSES` + a `lens` brief seam, `summarizer_spec`,
  `review_summarize_cli` (passthrough when only one real opinion — a model
  session only when there is merging to do), a reusable
  `advisory-review-summarize.yml`, and `lens`/`post` inputs on the reusable
  reviewer workflow (defaults keep existing callers unchanged).

### Added

- Claude-on-Vertex (ADC) billing, config-driven: set
  `AUTORESEARCH_VERTEX_PROJECT` (+ optional `_REGION`, `_ADC` file) and every
  claude-backend session — author, panel judge, reviewer — authenticates to
  Vertex AI via Application Default Credentials instead of an Anthropic API
  key (the session env then carries no key at all; contained sessions get the
  ADC file bind-mounted read-only). One env owner (`harness.vertex_from_env`);
  unset the project to fall back to API-key billing instantly. Codex/hermes
  are unaffected — OpenAI models are not on GCP.

### Changed

- One publish — the sealed candidate sha, everywhere. `live_climb`'s inline
  publish (workspace commit + drift fingerprints + the moved-base
  merge/re-measure machinery) is retired: every improved run now branches the
  SEALED `candidate_sha` and layers only the ledger commit on top, exactly as
  the wake publish always has. With every gate eval running on a fresh
  checkout of its sealed sha (the compute unification), eval-time workspace
  drift is structurally impossible, so the fingerprint forensics guarded
  nothing. A base branch that moves during a climb is no longer merged and
  re-measured by the orchestrator — the PR opens against the moved base as-is
  and review handles staleness, the wake path's long-standing stance
  (docs/design/research-loop.md: a stale PR is a re-wake, not an auto-merge).

- One compute interface, one measurer (`SlurmCompute` vs `LocalCompute` —
  Mengye's framing): `LocalCompute` runs the identical eval-job scripts as
  synchronous subprocesses in the current allocation, so `DispatchedMeasurer`
  is now the ONLY measurer — on the cluster its jobs park the climb, locally
  every job is done when checked and nothing parks. `LocalMeasurer` (the
  inline special case that measured candidates in the LIVE workspace and
  needed a separate baseline worktree) is deleted; every gate eval now runs
  on a fresh checkout of its sealed sha, so eval-time workspace drift is
  structurally impossible. The job script gains a bare mode (no image):
  the command runs in the throwaway tree under an env-scrubbed `env -i`,
  mirroring the uncontained evaluator it replaces. A future cloud/GPU-rental
  backend is one more implementation of the same six verbs.

- Author syscalls are CONTRACT-DRIVEN and on by default: the launch/sleep/
  submit tool arms whenever the deployment can deliver it (dispatch coords +
  a resumable backend) and the benchmark has not opted out. `depth_k: 0` is
  the per-benchmark opt-out; defaults are now generous (`depth_k` 10, bounds
  [0, 16]; `sleep_k` 20, bounds [1, 32]) — aggregate spend stays bounded by
  the contract's `runs_per_week` plus the per-session walltime/turn caps.

### Removed (enablement scaffolding)

- The transitional dark-launch scaffolding around author syscalls, now that
  the substrate is live-validated: the `AUTORESEARCH_AUTHOR_SYSCALLS` env
  flag, the `climb --author-syscalls` one-off switch, and the dead
  `AUTHOR_SLEEP_WAKE_READY` constant. Enablement lives in the contract, where
  a per-benchmark knob belongs.

### Fixed

- Multi-job parks are no longer blind: the tick's backup sweep now polls every
  job id in the stage's `afterany` string when a park records no single
  `experiment_job_id` (a candidate with sibling evals, or several author
  launches) and wakes when ALL are done. Previously such a park's only wake
  was the deadline floor — observed live as ~12 h of dead time after evals
  that finished in minutes. The park-time afterany wake job (zero-latency
  primary) remains planned; the sweep stays the backup.

### Added

- The `submit` verb (research-loop-buildout.md Phase B; role-cli.md Phase 1):
  the author declares its candidate ready with `syscall submit` + `sleep` —
  the tree is SEALED, the gate's paired evals (and any sibling launches) run
  as jobs, and the review panel reads the credited claim. A clean pass
  publishes the sealed candidate as a PR directly; a failed gate or blocking
  findings wake the SAME session with the feedback and the author decides —
  revise and resubmit, run more experiments, or finish with an honest
  negative. A submit costs only the sleep it rides on (`sleep_k` bounds the
  rounds; no new dial).

### Removed

- The orchestrator-driven panel-revision loop, retired in favor of
  author-driven revision via `submit` (buildout Phase B invariant: the swap
  lands in one PR). `panel_revisions` / `--panel-revisions`, the
  `_panel_revise_policy` composition seam, and the candidate wake's
  `_do_revise` re-entry are deleted. A plain finish (no submit) still runs
  the same gate and panel, but blocking findings now always open a DRAFT PR
  for a human — they no longer re-enter the author by kernel policy.

- `climb --author-syscalls` — a one-off switch to arm the author launch/sleep
  syscalls for a SINGLE climb (equivalent to `AUTORESEARCH_AUTHOR_SYSCALLS=1`,
  ORed with it, but scoped to the run so a live validation need not arm the
  whole tick). The benchmark must also declare `depth_k>1`. This is the
  enablement seam for validating the (still-dark) author sleep/wake substrate
  end-to-end on a real cluster before it goes live.

- Interchangeable backends — one harness construction for every role
  (docs/design/role-cli.md, "the harness unification"). `build_harness`
  (role_runner.py) replaces `build_editor_harness` and
  `build_reviewer_harness`: claude maps `spec.tools` to native flags (judges
  run `--bare` — an untrusted checkout must never load as instructions); codex
  runs `--sandbox danger-full-access` uniformly (its own sandbox needs
  bubblewrap — the boundary is the deployment's container or ephemeral runner,
  plus the tokenless split); hermes derives its toolsets from the spec and is
  a first-class backend (the manual-bench-only caveat is gone). Judges are
  executing roles now: they hold `Bash` and record their verdict by RUNNING
  the syscall tool (`finding` / `conclude`) — the reviewer/verifier briefs
  instruct exactly that, and roles differ by prompt, verbs, and output
  handling, never by a bespoke tool posture. A judge IS a role with an
  `output_schema`, so the redundant `RoleSpec.verdict_tool` flag is removed
  (`run_role` gates the verdict path on the schema), and the
  parse-a-final-message-and-repair path is deleted — `read_verdict` on the
  committed syscall channel is the one way a verdict reaches the kernel.

  Because judges now hold a shell, the verifier adopts the reviewer's existing
  tokenless split: its session job runs read-only and emits the verdict to an
  artifact (`VERIFY_EMIT_FILE`), and a separate write-token job
  (`verify_post_cli`) posts it — so a prompt-injected judge has no write
  credential to lift via /proc. The reviewer already worked this way.

- Hermes headless resume (`supports_resume = True`) — a headless CLI "resumes"
  by restarting with its prior context restored (all `claude --resume` /
  `codex exec resume` do), and hermes reads its brief from a file, so resume
  rehydrates the prior conversation INTO the next brief. `HermesHarness` keeps
  its own linear transcript (the message it sent + the assistant reply it
  parsed) in the per-run home, mints a session id on a fresh run, and prepends
  the rendered transcript on resume — so a resumed hermes session physically
  cannot start context-blind (the reason the old refusal existed). A resume whose
  transcript is missing is a hard error (`resume-unavailable`), never a blind
  fresh start; the transcript is read/written with the existing symlink-refusing
  helpers (the per-run home is session-writable). Hermes now revises/wakes like
  the other backends — a step toward fully interchangeable harnesses.

- One syscall surface — a verdict is a syscall TYPE, not a second tool
  (docs/design/role-cli.md). Every role talks to the kernel through ONE tool
  (`.autoresearch/syscall`); the kernel dispatches by type. The author's `sleep`
  (`type: "sleep"`) parks and wakes; the judge's `conclude` (`type: "verdict"`)
  is its `exit()`, carrying findings. `syscall_cli.py` gains the judge verbs
  (`finding` / `conclude`) alongside the author's (`launch` / `note` / `sleep`);
  `syscall.py` gains `read_verdict` (authoritative: size-capped, every field
  checked, typed) beside `read_request`, and `install_tool` now force-owns the
  `.autoresearch/` channel (a judge's checkout is untrusted — a pre-planted
  symlink or a forged `syscall.json` must not survive). `RoleSpec.verdict_tool`
  declares a judge emits via the tool instead of a final JSON message (requires
  an `output_schema`); `run_role` gates on that flag alone — like every other
  capability, the deployment builds the harness to match the role (a judge that
  emits via the tool runs a shell in the jail, same as an author) — installs the
  tool before the session and reads the committed verdict AFTER, no
  parse-and-repair loop. A spec without the flag uses the parse path, unchanged.
  No spec sets `verdict_tool` yet, so this changes no live behavior. Replaces the
  standalone `verdict.py` / `verdict_cli.py` (`.verdict/verdict`), which are
  removed. Follow-up unifies the judge and author harness setup so judges run a
  shell in the jail (differing only by system prompt), flips `verdict_tool` on,
  and deletes the judge parse-and-repair path.

- Author syscalls, part 2 — the WAKE (research-loop-buildout.md Phase A is
  complete; `AUTHOR_SLEEP_WAKE_READY` is flipped, so setting
  `AUTORESEARCH_AUTHOR_SYSCALLS` now enables the whole loop end to end):
  `resume_run` services `author-sleep` parks — each launch's job output (exit
  code, stdout/stderr tails) and its declared artifacts are delivered into the
  sandbox (`.autoresearch/results/<name>/`), and the SAME session is resumed
  through the climb's resume-entry with the results data-fenced, the author's
  own note echoed back, and the remaining budgets. The woken session may launch
  again (a fresh author-sleep park, counts advanced) or finish — its gate
  measures through the dispatched backend and parks the run as a CANDIDATE,
  which the existing wake path (panel, sealed-sha publish) decides. A wake that
  cannot resume (no author harness / no session id / a no-resume backend) ends
  the run with a named `session-error`, never a stuck WAITING loop. The brief
  now advertises the launch/sleep tool (with this run's budgets) exactly when
  it is wired, and the wake CLI builds the author harness for author-sleep
  parks even when no panel is configured.

- Author syscalls, part 1 — the SLEEP side (docs/design/research-loop-buildout.md,
  Phase A; fully dark: enabling needs BOTH `AUTORESEARCH_AUTHOR_SYSCALLS` and the
  part-2 wake, which flips `AUTHOR_SLEEP_WAKE_READY` — arming the flag alone
  logs a warning and changes nothing): an enabled author
  can end its session having written `.autoresearch/syscall.json` — launches to
  run outside the sandbox (each a jailed Slurm job on a sealed snapshot of its
  tree, stdout/stderr + declared artifact files captured for the wake) plus a
  note to itself — and the climb parks as `author-sleep` instead of measuring.
  Budgets are three independent generous counts: launches (`depth_k`), sleeps
  (new per-benchmark `sleep_k`, default 4, bounded `[1, 16]`), submits later
  (Phase B). An over-budget ask wakes the same session once with a refusal;
  a malformed request errs loudly; the `.autoresearch/` channel is excluded
  from diffs/scope/drift via `.git/info/exclude`. The author's INTERFACE is a
  tool (`python .autoresearch/syscall launch ... -- <cmd>`; `... sleep`),
  installed into the sandbox — the JSON is an internal ABI it commits, with
  in-session validation so bad args fail immediately, not as a burned sleep.
  The wake side (deliver results, resume the session through the climb's
  resume-entry) is part 2.

- Per-benchmark depth budget `depth_k` on the contract's benchmark
  (docs/design/research-loop.md, "one syscall, author-directed"): how many
  external experiment jobs the author may launch within one attempt — a
  generous meter on the author's own experimentation, not a loop the kernel
  drives. Defaults to `1` (today's one-shot), bounded `[1, 8]`. This is the
  declared knob; the launch/sleep syscalls that consume it land next
  (research-loop-buildout.md, Phase A).

- Alternative AUTHOR backend: `climb --author-backend codex` runs the editing
  role on the OpenAI Codex CLI instead of Claude, to try a cheaper author (e.g.
  `--model gpt-5.6-terra`). It runs **contained** — `codex login` and `codex
  exec` both inside `apptainer exec --containall --cleanenv` (shared bound
  `--home` for auth.json). codex runs `--sandbox danger-full-access` and relies
  on apptainer as the boundary (codex's own sandbox needs bubblewrap, absent in
  the image) — the same single-boundary posture as the Claude author. The
  host codex binary is bind-mounted read-only into the container (`--codex-bin`,
  like `--claude-bin`) so the image stays codex-free and codex updates by
  swapping one host binary. `--image` and a non-claude `--model` are required
  (guarded early); `--codex-config KEY=VALUE` passes host-specific codex config.
  Headless session resume is validated on codex-cli 0.130.0 (`supports_resume=True`):
  a contained `codex exec resume <thread_id>` recalled prior-turn context, so a
  blocking panel finding WAKES codex to revise like Claude. `codex exec resume`
  takes neither `--sandbox` nor `--cd`: it inherits the recorded session's sandbox
  (a resumed read-only reviewer stays read-only — verified) and cwd; the author
  adds `--dangerously-bypass-approvals-and-sandbox` to also skip approvals. The
  read-only codex/hermes reviewer path is unchanged. (Needs codex on the host.)

- Deploy plumbing for the config-driven author: `scripts/install_codex.sh`
  idempotently installs the harness-verified codex binary (0.130.0) on the host
  (fast path is a local `codex --version`; only downloads on a version
  mismatch/missing binary), and `tick_chain.sbatch` now sources
  `~/.config/autoresearch/.env` each tick and runs the install best-effort when
  `AUTORESEARCH_AUTHOR_BACKEND=codex`. So enabling codex on the live loop is a
  `.env` edit (no chain restart) plus provisioning the codex key file — nothing
  in the chain breaks if either is absent.

- Config-driven author selection: the author backend is now a CONFIG choice the
  role runners resolve themselves, not something the tick threads per lane. The
  `climb` and `followup` CLIs default `--author-backend`/`--model`/`--codex-bin`
  from `AUTORESEARCH_AUTHOR_BACKEND`/`_MODEL` and `AUTORESEARCH_CODEX_BIN` (same
  env-default pattern already used for `--image`/`--account`/`--partition`), and
  the author key resolves PER BACKEND — `AUTORESEARCH_HARNESS_KEY_FILE` for
  claude, `AUTORESEARCH_CODEX_KEY_FILE` for codex — with the two keys COEXISTING
  on disk (`resolve_author_key_file`). So flipping the fleet is a one-line env
  change, not a key swap, and no in-flight run breaks. The tick no longer threads
  the author backend or key into the climb/intake/wake/follow-up jobs — a new
  author backend needs ZERO tick change (only the steward keeps its own explicit
  key, a distinct role). A run's full author identity — `(backend, model,
  key-file path)` — is persisted on its record, so a wake or follow-up reproduces
  that run's OWN author, model, and key, never the current fleet default (a legacy
  record with no backend is treated as claude, not the fleet's; an explicit
  `--key-file` survives resume). A mid-flight fleet flip therefore never resumes a
  run on the wrong backend, pairs a codex backend with a claude model, or reaches
  for the wrong key. The tick preflights the fleet author config (a codex backend
  needs a non-claude model + image) before self-initiated submits or intake
  claims, so a misconfig never strands a claimed issue. The codex author is validated on the EFFECTIVE
  author per path (fresh args vs the parked run's persisted pair), so the check
  never judges a fleet backend against a run's model.

- Tick coalescing: under partition congestion, queued ticks bunch up and become
  eligible together (the singleton dependency serializes them), so they would
  run back-to-back and redundantly re-sweep. A tick now no-ops if another tick
  *completed its work* within `AUTORESEARCH_MIN_TICK_MINUTES`. The window is
  cadence-aware: both the default and any explicit value are capped at half of
  `AUTORESEARCH_CADENCE_MIN` (the default additionally capped at 10 min, the
  ceiling at 60 min), so it can never reach the cadence and swallow normal ticks
  — a short cadence scales it down; 0 disables; non-finite/corrupt inputs fall
  back safely. The guard keys on a work-completion marker written at a tick's END
  (at real completion time), not the start-of-tick heartbeat, so a tick that
  crashes mid-work never suppresses the next (recovery) tick; the heartbeat is
  still written so the watchdog stays fed; a corrupt marker file degrades to "no
  marker" rather than crashing the tick. Only late-bunched pile-ups fall inside
  the window; on-cadence ticks are untouched. (Partition routing for the heavy
  climb/eval jobs already exists — `AUTORESEARCH_JOB_PARTITION`.)

- The dispatched-wake on-switch can now be armed with a `<root>/DISPATCH_WAKE`
  sentinel file, mirroring the `PAUSE` sentinel — an operator arms/disarms with
  a `touch`/`rm`, no tick-chain restart or env-var surgery on a live chain. The
  `AUTORESEARCH_DISPATCH_WAKE` env var still works too (either arms it); a
  half-configured chain env still fails safe to the dry `LoggingDispatcher`.

- The improved-wake publish is now idempotent across a crash mid-open. A wake
  that opened the PR but died before recording it left the run WAITING; the
  re-wake would re-push non-fast-forward and open a duplicate (or record ABORTED
  over a live PR). Now `resume_run` first asks `find_open_pull_for_head` (new
  `GitHubClient` method, `GET /pulls?head=…&state=open`) and, if a PR is already
  open for the run's head, reconciles the record to `in-review` on that PR —
  no re-push, no duplicate. This is the last dispatcher activation-blocker; the
  path can now be turned on (`AUTORESEARCH_DISPATCH_WAKE`).

- Panel-on-wake, slice 2 — the first depth-axis build (docs/design/research-loop.md,
  "wake the agent with evidence"): a BLOCKING panel finding on a dispatched
  improvement now WAKES THE AGENT to revise instead of drafting. `resume_run`
  re-runs the author session with the findings (`run_role` over the checked-out
  candidate), re-snapshots the revised tree, and re-measures through the
  dispatched measurer — which re-parks, so the NEXT wake runs the panel again.
  Each revision is one park/wake cycle; `panel_reads` persists in the stage and
  bounds the loop at `panel_revisions`, after which a still-blocking finding
  DRAFTs for a human. The revision keeps the new candidate snapshot and drops
  the superseded one; a revision that goes out of scope (or otherwise fails the
  gate) ends the run. The `--resume` CLI builds the editor harness (author key)
  and `JobWakeDispatcher` forwards `--key-file`/`--max-turns` + sizes the wake
  walltime for the revision session (`_wake_panel_minutes`). A revision session
  that cannot run falls back to a DRAFT — the improvement is real, just
  unrevised.

- Panel-on-wake, slice 1 — a dispatched improvement now runs the SAME pre-PR
  verification panel as an inline climb (docs/design/orchestrator-verify.md), so
  it is not published unverified. `resume_run` runs the read-only lenses over
  `base_sha` vs the sealed `candidate_sha` (checked out first, so the panel
  judges exactly what was measured — the dispatched evals ran on node-local
  scratch, so the tree is unchanged); a blocking or degraded verdict opens a
  DRAFT PR carrying the findings and never arms auto-merge, a clean verdict (or
  no panel) arms only where branch protection requires a review — same policy as
  the inline path. The `--resume` CLI and `JobWakeDispatcher` forward
  `--panel`/`--panel-key-file` (via the shared `_panel_lenses_from_args` and
  `_climb_panel_argv`), and `build_panel_runner`/lens-building are now shared by
  both paths. Slice 2 — WAKING the agent to REVISE on a blocking finding (the
  first depth-axis build, docs/design/research-loop.md) — is next; for now a
  human triages the draft.

### Changed

- We credit **beating your own baseline**, not only beating the recorded best
  (docs/design/research-loop.md, "two kinds of win"). The first-pass climb no
  longer hard-fails a candidate that improves over `base_sha` by the gate's
  threshold but does not beat the ledger's recorded best — that clean composable
  win now opens a PR (a human, later a planner, judges). The `NoiseFloored`
  ending and the recorded-best/stale-clone abort are removed; the gate
  (`measure_and_decide`) still requires candidate to beat `base_sha` on paired
  seeds, and the ledger's `best` still only advances on a genuine improvement
  (SOTA stays tracked, just not required). The dispatched wake matches: when the
  ledger does not move, it pushes the sealed candidate with no leaderboard commit
  on top (this also fixes an empty-commit abort the reviewer caught).

### Fixed

- A genuine API refusal the Claude CLI reports as a `subtype: "success"` error
  is no longer misclassified as a `session-error`. Surfaced on Torch: a session
  hit the author key's workspace usage cap and came back `is_error` with a
  content-free `subtype: "success"` and the real cause only in
  `result="API Error: 400 ... usage limits"`. The parse stamped `"success"` into
  `error_detail`, so `outage()` scanned `"success"`, matched nothing, and billed
  a genuine outage as a `session-error` — the lanes would NOT pause and the
  failure would count against the run's retry caps. The Claude harness parse now
  lifts that machine-shaped (`"API Error ..."`) `result` into `error_detail`, so
  the real cause reaches the operator log AND the outage latch — both its
  classifier and its throttle-duration check (which reads `rate_limit`/
  `overloaded` off the detail). Fixed at the parse, not in `outage()`, so the
  classifier keeps consulting `final_text` only when `error_detail` is empty —
  agent prose (Codex/Hermes `final_text`) still cannot trip the latch.

- Wake-path hardening from the self-review + advisory review of the dispatched
  wake (all pre-activation, the path is still dark):
  - the improved wake now FORCE-checks-out the sealed candidate sha (at wake the
    workspace is the session's dirty tree) and commits ONLY the ledger files on
    top of it — never `git add -A`, which would sweep in untracked cruft the
    session/eval left, unmeasured and unscoped;
  - a no-progress re-park (a blind re-park with an empty afterany, or the same
    jobs still pending) KEEPS `wake_attempts` instead of resetting it, so the
    stuck cap still bites a run that wakes without advancing; a productive
    re-park (a new job set) still resets;
  - the wake never arms auto-merge — it runs no verification panel yet
    (panel-on-wake is a later slice), so every dispatched improvement waits for
    a human;
  - the IN_REVIEW record is saved BEFORE the snapshot is dropped, so a failed
    save leaves the run recoverable rather than an ABORTED record over a live
    PR; a publish failure drops the snapshot (ENDED runs are never swept, so
    keeping it only leaked the ref);
  - terminal records clear the WAITING-only fields (`stage`, `deadline`,
    `wake_attempts`, …) so a woken run does not carry a shrunk follow-up retry
    budget into `in-review`;
  - `measured_paths` is re-derived NUL-delimited (`-z`), so a path with a space
    cannot slip past the scope check.

### Changed

- A dispatched climb now has ONE park, not two. The pre-session baseline
  measurement is dropped: `climb_once` no longer measures `base_sha` before the
  session (and never raises a `phase="baseline"` park). The baseline is measured
  by the GATE (`measure_and_decide`, `base_sha` vs `candidate_sha`) after the
  session, and the brief's reference number comes from the ledger's last-known
  best (`brief_baseline`, `None` on a benchmark's first run) — so the agent
  still sees "improve from ~X" without a dispatched pre-pass. This removes the
  baseline-wake path entirely (a dispatched climb only ever candidate-parks),
  and matches what `dispatcher.md`'s resume seam already specified (the seam is
  after the session). Tradeoff: a broken eval command now costs a session before
  it surfaces in the gate, instead of failing fast pre-session. `resume_run`
  guards that every park it wakes is a candidate park.

### Added

- `resume_climb` (`orchestrator` module) — dispatcher phase 1, stage B part 2c,
  slice 1: the WAKE side of a dispatched candidate park, as a re-enterable
  function. It re-runs the post-session decision (`measure_and_decide`) over the
  committed shas the record persisted, WITHOUT re-running the session — the
  session's edits are already in `candidate_sha` and its write-up is now saved
  on the park (a new `report` field in the WAITING `stage`) so the wake can
  build the PR body and panel claim from it. The measurer reads the cached eval
  results and it returns the decision, or a measure is not done yet (the suite
  pairs after an improving candidate — "another round of experiments") and it
  re-parks by raising `ClimbParked`, same shape as the first pass. This is the
  seam the depth axis (docs/design/research-loop.md) grows from: slice 1 wakes
  the grader with the panel off; waking the AGENT with the result lands next.
  The wake-entry CLI + `WakeDispatcher` (delivery) are the following slices.
- `resume_run` (`climb` module) — dispatcher phase 1, stage B part 2c, slice
  1b: the wake-entry that rebuilds a parked climb's context from its record and
  drives `resume_climb`. It re-derives `measured_paths` from the COMMITTED
  `base..candidate` diff (the sealed candidate, never a live tree that may have
  drifted since the park), and handles the two exits that reuse existing
  machinery — a RE-PARK (a measure the wake just dispatched isn't done — the
  suite pairs an improving candidate fans out) re-persists the WAITING stage on
  the new afterany keeping the same snapshot; a NEGATIVE terminal drops the
  snapshot and ends the record; an IMPROVED terminal branches the sealed
  `candidate_sha` (never the live tree — the scope-checked diff carries only
  in-scope changes), folds the leaderboard update on top, pushes, opens the PR,
  and ends the run in-review. The mechanical moved-base merge the first pass
  does is deliberately NOT here (docs/design/research-loop.md, "the finish is
  agent-driven too"): a stale PR is a re-wake, not an orchestrator auto-merge.
  `WakeDispatcher` (the delivery that fires this on the eval jobs finishing) is
  the slice after.
- The wake delivery — dispatcher phase 1, stage B part 2c, slices 2a+2b: a
  `--resume RUN_ID` mode on the climb CLI (no session/api-key/panel — it rebuilds
  the dispatched measurer and calls `resume_run`), and `JobWakeDispatcher`, the
  production `WakeDispatcher` that submits a short CPU wake job running that CLI,
  depending on the run's eval jobs (`afterany`) so it fires when they finish (or
  immediately if already done). `tick.main` now selects the wake delivery behind
  an EXPLICIT on-switch (`_wake_dispatcher_from_env`, slice 2c): with
  `AUTORESEARCH_DISPATCH_WAKE` set AND the chain env complete, the waiting-run
  sweep runs LIVE with `JobWakeDispatcher`; otherwise it stays dry with the
  `LoggingDispatcher` — dispatched climbing lands DARK, and a half-configured
  environment fails safe to dry. With the switch on, the sweep delivers wakes: a
  single-job park wakes on its eval's terminal, a multi-job park on the deadline
  floor. The faster afterany wake submitted AT PARK (a wake the instant the eval
  jobs finish, not on the next sweep) is the remaining responsiveness follow-up.

- Dispatched measurement is now SELECTED per benchmark — dispatcher phase 1,
  stage B part 2c(ii), the switch that first makes a park fire. `live_climb`
  reads the benchmark's `eval_minutes` once and, when it exceeds the in-job
  runway (`should_dispatch`) and cluster coordinates are present, measures
  through a `DispatchedMeasurer` (each eval its own Slurm job) instead of the
  inline `LocalMeasurer`; otherwise nothing changes. The coordinates are a new
  `DispatchSettings` group (compute + image + account + partition) the climb
  CLI reads once from `--account`/`--partition`/`--image` (defaulting to the
  `AUTORESEARCH_*` chain env the tick already sets on the climb job); absent
  any of them, measurement stays inline regardless of the hint. On the
  dispatched path the climb skips the local baseline worktree (the eval jobs
  check out each sha themselves) and `snapshot` registers no live map. An
  expensive benchmark now parks its baseline before the session (PARK 1) and
  its candidate after (PARK 2). If the WAITING record fails to persist, the
  already-submitted eval jobs are cancelled rather than orphaned in the queue
  (nothing would ever wake them). The wake path that resumes from these parks
  is the next part.
- The climb PARK mechanics — dispatcher phase 1, stage B part 2c(i). When a
  measurer parks (`MeasurementPending`), `climb_once` raises `ClimbParked` at
  whichever point hibernated: `baseline` (before the session even runs — the
  wake will rerun it) or `candidate` (after it — the wake decides). `live_climb`
  catches it and writes a `WAITING` record carrying the re-entry `stage` (a new
  `RunRecord` field: committed shas, drawn seeds, the candidate snapshot ref,
  and the afterany set), keeps the candidate snapshot alive so the wake can read
  it (dropping every other), and ends — never an error. Selecting the dispatched
  backend for expensive evals (so parks actually fire) and the wake path that
  resumes from the record are the next parts.

### Changed

- `climb_once` now measures through the `Measurer` seam — dispatcher phase 1,
  stage B part 2b(ii). Its synchronous baseline/candidate/suite evals are
  replaced by one `measure_and_decide` call over committed shas; it takes a
  `measurer` + `base_sha` + a `snapshot()` callback (all git stays in the
  caller, `climb.py`, which builds a `LocalMeasurer`, snapshots each candidate,
  and drops the snapshot refs when the climb ends) instead of an `evaluator` +
  `baseline_workspace`. Same decisions and same panel loop; the eval sequence
  is nearly identical, with two deliberate improvements from the clean/live
  split: the baseline (a clean tree) is cached after the brief so the gate does
  not re-measure it, and on a panel revision the sibling BASELINES are no longer
  re-measured either (they too are the stable clean tree) — only the candidate
  and sibling-candidate sides re-run. The `suite_seed` is also drawn once up
  front (persisted-shape, for a wake to reproduce) instead of inside the loop.
  This is the seam that lets a later PR swap in the dispatched backend for
  expensive evals. Also: `LocalMeasurer` now names the failing measure in its
  `EvalError` (matching the dispatched backend), and the removed "no pristine
  baseline workspace" branch is gone (baseline is always a committed sha now,
  so the suite gate can always measure siblings).

### Added

- `LocalMeasurer` (`measure` module) — dispatcher phase 1, stage B part 2b(i):
  the inline `Measurer` backend, for cheap benchmarks and local runs where a
  dispatched job would cost more than the eval. It measures the worktrees the
  caller already has (`base_sha` -> pre-session tree, `candidate_sha` -> the
  session workspace), so it adds no checkout and no new trust surface — the
  same evaluator on the same worktrees the synchronous climb always used — and
  never parks. Caching turns on the worktree kind: `clean` checkouts (content
  == sha, e.g. the pristine baseline tree) are cached by identity, so the
  baseline measured for the brief is not re-measured in the gate; the `live`
  workspace (the candidate) is measured FRESH every call, because its content
  is not pinned by the sha (untracked files) and a revision changing only
  untracked content must not read a stale cached result. The second backend
  behind the `Measurer` seam `measure_and_decide` already targets; the inline
  wiring into `climb_once` landed in part 2b(ii), and `should_dispatch` picking
  the dispatched backend for expensive evals is next.

- `measure_and_decide` (`orchestrator` module) — dispatcher phase 1, stage B
  part 2a: the post-session decision extracted as a PURE function of committed
  shas and a `Measurer` seam (scope, then baseline/candidate paired measure,
  the improvement threshold, and the suite gate). No session and no git, so a
  wake reconstructs the same plan and reads cached results — the re-enterable
  core the resume path needs. Two `Measurer` backends will sit behind the one
  seam (inline for cheap benchmarks, the dispatched measurer for expensive
  ones), chosen per `eval_minutes`; the suite seed is now an input (drawn once,
  persisted) rather than drawn inline, which a resume could not reproduce.
  `climb_once`/`live_climb` are now wired onto it (inline backend); the
  dispatched backend + park/wake are the next part.
- The dispatched measurer (`measure` module) — dispatcher phase 1, stage B
  part 1: turns a climb's set of committed-tree measures into submit-all,
  PARK (`MeasurementPending` carrying the afterany wake dependency for the
  whole set), and on wake read-all-from-disk. Built on the eval primitive;
  the resumable unit is the measure-and-decide phase (a pure function of
  committed trees), so it re-runs idempotently after a process death — a
  completed measure returns instantly, a pending one re-parks. Selecting it for
  expensive evals (`should_dispatch`) and the park/wake path are the next part.
- The dispatched-eval primitive (`dispatch` module) — dispatcher phase 1,
  stage A per docs/design/dispatcher.md: temp-index tree snapshots (working
  index untouched, tree hash = the drift fingerprint), the orchestrator-
  authored eval job script (same jail as the in-job evaluator; the contract
  command never crosses shell quoting; stdout captured outside the
  containment into the run dir), result reading with the in-job error
  semantics, and the contract's `eval_minutes` hint (own ceiling, in-job
  below the threshold). Wiring into the climb transaction and the wake
  path is the next stage.

### Removed

- The one-shot **completer** reviewer and verifier are sunset, now that all lab
  repos run the agent-session path. Deleted: `advisory-review.yml`,
  `verify.yml`, `review_cli.py`, `verifier_cli.py`, `llm.AnthropicCompleter`,
  the `Completer` protocol, the completer `review()`/`verify()`, and the
  `anthropic` dependency (with the `review` extra). The shared review vocabulary
  and rendering the agent path builds on (`build_prompt`, `result_from_data`,
  `format_review`, `gather_thread`, …) stay in `review`/`verifier`.
- Swept in the same PR, dead once the completer went: the unused
  `PullRequest.context_files` field and its prompt rendering (nothing populated
  it), and the explicit re-request override for bot PRs (only the completer
  workflow set `REVIEW_EXPLICIT_REQUEST`). The advisory reviewer now never
  reviews bot-authored PRs.
- `ADVISORY_MARKER` dropped from follow-up wake context
  (`MACHINE_ROUND_MARKERS`): with the reviewer reviewing only human PRs and
  wakes scanning bot PRs, advisory rounds never fed a wake. Verifier rounds
  still do.

### Added

- The pre-PR verification panel (docs/design/orchestrator-verify.md) runs
  inside the climb job: after a candidate measures improved (and suite-gates
  clean), a panel of read-only lenses — integrity `verify` and code `review`,
  each on any backend — reads the claim over local pr-head/base worktrees.
  Blocking findings wake the same author session (data-fenced); the revision
  is fully re-measured and re-gated; one revision round after the initial
  read, then blocking-still-open opens a DRAFT PR with the findings on top
  and never arms auto-merge. Every PR carries the verification transcript;
  a lens with no verdict is recorded — silence is never endorsement. Enable
  with `--panel verify,review` (+ `--panel-key-file`, the verifier's own
  key). Round numbers now count per reviewer, so standing opinions no longer
  inflate each other's counters.
- autoresearch's own PRs now get a terra second opinion (openai/gpt-5.6-terra
  via hermes/OpenRouter) next to the Claude round — the first live user of the
  least-token split. Takes effect for PRs opened after this merges
  (`pull_request_target` runs the base branch's workflow).
- The advisory reusable (`advisory-review-agent.yml`) is now one workflow for
  every backend, always through the least-token split: a read-only session
  job runs the backend (claude default; hermes/OpenRouter; codex) and emits
  raw findings as an artifact; a separate posting job — no session —
  re-validates the envelope (right PR, bot-skip re-checked, sanitizing
  render) and posts with an opinion label. A second opinion is one more caller
  job with a different backend/model. Session cost and turns are logged at
  emit/post time. On private repos the session job carries a read-scoped
  token only; it becomes tokenless at the public flip.
- The hermes backend can run directly against the OpenAI API: the new
  `hermes_provider` workflow input (`openrouter` default | `openai`) seeds
  hermes's canonical `openai-api` provider and switches the key source to
  `openai_reviewer_key` — same token rate, no OpenRouter platform fee, and
  provider-native model ids (`gpt-5.6-terra`, no `openai/` prefix). Key
  injection stays one-key-per-session-tree across the provider split, and a
  typo'd provider posts a PR-visible skip stub like a typo'd backend.
- Every judge round now names its reviewer in the round stamp —
  ``reviewer `backend/model`​`` (e.g. `claude/claude-opus-5`, `hermes/<terra
  id>`) — on advisory rounds, second opinions (via the envelope), and
  verifier rounds alike, so multi-reviewer threads read unambiguously.
- Suite no-regression gate: contracts may declare `scope.shared` (shared code
  paths — encoder, world model, training loop). A solver diff touching one is
  only credited after every sibling benchmark is re-measured on both sides
  (paired seed); a sibling regressing beyond its own floor ends the run as
  `suite-regression`, an honest negative — including when the regression is
  found on the merged tree after the base moved (never an abort). Env-specific
  diffs still measure only their benchmark; the PR body carries the suite
  table. A shared path outside the allowed scope is dead config and fails at
  load. Benchmark names are now slug-shaped (they reach branch names and
  ledger keys). Follow-up pushes are not yet suite-gated (named gap).
- The reviewer can now run on three backends, selected by `REVIEW_BACKEND`
  (`claude` | `codex` | `hermes`) with per-backend keys
  (`ANTHROPIC_REVIEWER_KEY` | `OPENAI_REVIEWER_KEY` | `OPENROUTER_API_KEY`) and
  a backend-aware `model`. How read-only is enforced differs by backend, and
  that is a trust difference: claude uses a native read-only tool set
  (trusted); codex an OS sandbox (on GitHub-hosted, the Landlock sandbox via
  `REVIEW_CODEX_LEGACY_LANDLOCK`, since the default bwrap cannot init there);
  hermes an environmental boundary only (no shell, inert writes on an ephemeral
  runner) — experimental. On the auto path every backend now runs through the
  least-token split (see the consolidated reusable above), so no session sits
  next to a write token; the manual bench (`review-agent.yml`) remains the
  single-job informed-caveat path for experiments.

### Changed

- The verify lens's severity rubric now states that a claim contradicted by
  the PR's own evidence is an integrity defect and grades blocking, even
  when the measured delta is real — from the first live panel run, where
  convergent findings of exactly that shape were graded advisory and the PR
  shipped ready instead of draft.
- The tick now flips the pre-PR panel ON for every climb job it submits
  (`--panel verify,review` at both submission sites). `AUTORESEARCH_PANEL=""`
  is the operator off-switch; `AUTORESEARCH_PANEL_KEY_FILE` overrides the
  verifier-key path. The tick preflights the whole panel config BEFORE
  claiming an intake issue or submitting a climb — lens grammar and backend
  (via the shared `parse_lenses`), the key file with the climb's own
  acceptance rules (absolute path, exists, mode 600, non-empty), and role
  separation (the panel key must not be the author key) — so a misconfigured
  panel stays loud (tick error every pass) but never strands a claimed issue
  behind a climb that dies at startup. The panel's walltime is orchestrator
  overhead: the tick ADDS an allowance for it to the contract-clamped job
  budget (a contract can never raise orchestrator spend, so it cannot be
  asked to fund our own gate), clamped at the job partition's MaxTime —
  sbatch rejects longer requests outright. Work jobs can ride a longer
  partition than the tick chain: `AUTORESEARCH_JOB_PARTITION` (e.g. cpu48)
  with `AUTORESEARCH_MAX_JOB_MINUTES` raising the cap in lockstep (code
  ceiling 10h until the stranded window is spec-aware — named gap). A
  residual overrun still fails safe via the self-deadline.
- All five roles now run on the role-runner: `author_spec`/`followup_spec`/
  `steward_spec` (roles.py) join the judges' specs as the manifests,
  `climb_once`/`respond_once`/`live_steward` dispatch through `run_role`, and
  the CLIs build their harness from the spec (`build_editor_harness`, one
  builder for all editing roles), so the session budget and tool set have
  one source. The follow-up runs under the resuming role's key and scope
  (author or steward, from the record and contract); the steward's spec
  carries its own territory (`contract.steward.allowed`). Behavior
  unchanged. The session-dispatch half of the driver collapse is complete;
  briefs/skills as app config is the remaining consolidation.
- The verifier can now run as an agent session over TWO read-only checkouts:
  the PR head (the change under review) and the base branch (the trusted
  contract and ruler — all ruler reads are directed there, never at the PR).
  Same constitution as the one-call verifier it will replace: bot PRs only,
  advisory, silence is never endorsement. The completer verifier stays the
  live default until the caller workflows swap.
- Agent judge sessions are hardened against instruction smuggling: the Claude
  CLI runs with `--bare` (no hooks, no CLAUDE.md auto-discovery, key-only
  auth), and the CLIs rename instruction-bearing files (`CLAUDE.md`,
  `AGENTS.md`, `.claude/`, `.mcp.json`) inside the untrusted checkout before
  any session starts, so PR content can be read as data but never loaded as
  instructions.
- The advisory reviewer now runs as an agent session that reads the PR head
  (read-only — no execute), instead of a single model call over the diff. It
  investigates the surrounding code, callers, and tests, so it finds defects a
  diff-only pass cannot. It runs on the same triggers (PR open/reopen and the
  `autoresearch:review` label), keeps the same fork gate (same-repo PRs only),
  and never executes PR code. The reusable workflow is
  `advisory-review-agent.yml`; the completer reusable remains until the other
  lab repos migrate, then it is removed. Backends are pluggable (claude on
  GitHub-hosted runners; codex on a namespace-capable host).
- Review findings carry a `kind` — change, suggestion, question, or note —
  saying what the reader is asked to do, separate from whether the finding
  gates the merge. Placement follows it: actionable findings (a blocking
  defect, a change, or a suggestion) anchor inline on their line; questions
  and notes stay in the compact body list so local FYIs do not flood the
  diff. Suggestions are marked as optional.
- Reviews are shorter and lead with a verdict. Every advisory and
  verification round now opens with one line — blocking vs advisory
  counts — then shows blocking findings in full and the rest as a
  compact list. Findings carry a `blocking` flag; only blocking ones
  should gate a merge or trigger another round. The review bar is
  "materially sound", not "no finding survives".
- Every model role (reviewer, verifier, author, steward) writes in plain
  simple technical English, from one shared style instruction.

### Added

- Benchmarks can declare a relative cross-seed noise floor
  (`min_delta_rel`), a fraction of the recorded level, alongside or
  instead of the absolute `min_delta`. This is the right floor for an
  unbounded metric such as wall-clock timing, whose level drifts with
  hardware so a fixed absolute number goes stale. Both set means the
  more conservative applies.

- Persistent contract failure alarms on GitHub: after three consecutive
  ticks unable to load the target's `.autoresearch.yaml`, the tick opens
  one issue on the target repo with the loader's error (marker-deduped,
  survives state loss); the next successful load comments and closes it.
  A rejected contract silently idled every launch lane for 36 hours once
  — the only signal was a log line on the cluster.

### Changed

- Every flight has a snapshot: submitted jobs (climbs, stewardships,
  intake, follow-ups) run from a detached worktree of the checkout as
  it was at submit time instead of the shared checkout the deploy step
  resets every tick — no more code swaps under queued jobs, and crashed
  flights leave their exact tree for forensics. Expired flights are
  reaped after 24 h; snapshot failure falls back to the shared checkout.

- The advisory reviewer posts human PRs as a GitHub review: findings
  anchored to diff lines become resolvable inline threads (auto-marked
  outdated on push), the marker/header/notes and any un-anchorable
  findings stay in the review body, and the event is hard-coded COMMENT
  — the client cannot approve or request changes. Explicit rounds on
  bot PRs remain issue comments so they can ride into follow-up wakes;
  round numbering spans both styles. REVIEW_INLINE=false restores the
  single-comment style.

### Fixed

- Follow-up sessions now receive the raised turn budget: the tick passes
  `--max-turns` (clamped by the contract's `session_max_turns`) to
  follow-up jobs, and the CLI's fallback follows the harness ceiling
  instead of a stale 40. A live steward follow-up burned its whole
  session against the old default and produced nothing.

### Changed

- Ledger numbers on resampled pools are re-derivable and noise-honest
  (verifier finding on the first reach re-base): a benchmark declaring
  `seed_env` gets one orchestrator-drawn seed per measurement pass,
  injected into BOTH sides of every comparison (paired) and recorded as
  `run_seed` in results/leader.json — by the climb, the steward re-base,
  and both follow-up re-measure paths. A new `min_delta` contract knob
  (absolute units) sets the cross-seed noise floor: candidates that beat
  the recorded best by less end as honest negative results ("within the
  noise floor"), not PRs and not aborts.

- API outages are contained instead of billed to runs: a session the
  model API refuses (credit balance, usage/spend limit, auth,
  throttling) ends the run `stuck` with the refusal named, releases a
  steward claim without counting it toward the attempt cap, refunds the
  follow-up wake attempt, and stamps a latch that pauses all
  that role's session-spawning lanes (per-role latches: a dead steward
  key never idles the solver lanes) for a 45-minute cooldown — 5 minutes
  for transient throttling — so one canary per hour re-probes instead of
  every lane burning retries each tick. Persistent refusals escalate:
  after 5 outage releases a work order waits for a human. The
  advisory reviewer and verifier post a skip stub on the PR thread
  (own marker; never counts as a round, never rides as wake context)
  when the model API refuses their round, so a missing review is
  visible on the thread rather than only in the Actions tab.

- Session budgets raised (60/60/90/60 → 120-turn sessions, 90-minute
  session walltime, 120-minute climb jobs, 90-minute follow-up jobs):
  the first steward work order to BUILD an environment burned its full
  60-turn budget mid-work — budgets sized for solver tweaks starve
  construction work. Ceilings still equal defaults, so contracts shape
  spend strictly downward.

- A session that runs out of turns or walltime now ends as
  budget-exhausted — one of the six honest deaths — instead of an
  error, and every surface a human reads carries the real cause: the
  record's ending note, the run report, the work-order comment ("ran
  out of its session budget (error_max_turns: Reached maximum number
  of turns (120))"), where before all three said `ValueError: steward
  session error: tool_use`.

- Review and verification comments read like a colleague, not a legal
  notice (maintainer feedback): one calm italic header line per role, and
  findings rendered as prose paragraphs with the reference at the end
  instead of bullet fragments. The mechanical guard against forged
  endorsements (approval-language redaction) is unchanged.

- CI consolidated to one job (`ci`) — GitHub bills per job rounded up to a
  minute, so five short jobs cost 5x. The advisory review now runs on PR open
  and on the `autoresearch:review` label, not on every push.

- **Breaking**: the review CLI reads `ANTHROPIC_REVIEWER_KEY`, no longer
  `ANTHROPIC_API_KEY` (role-named credentials; the harness gets its own key).
  Reusable-workflow callers are unaffected. Direct invokers must export the
  new name — with the old one the reviewer skips cleanly but silently.
- The reviewer now sees today's date, repo/PR metadata, and bounded
  head-revision contents of changed files, and its prompt demands
  evidence-based findings (fixes a live false positive that flagged a correct
  date as a typo).

### Fixed

- Evals and validation runs build a PRIVATE environment per eval
  (`UV_PROJECT_ENVIRONMENT` on node-local scratch, beside the uv
  cache, dying with the eval) instead of
  consuming the workspace venv the session built: no shared mutable state
  across processes (the first live steward validation lost a race to NFS
  close-to-open consistency on a venv another process had just written),
  and the orchestrator never executes session-authored entrypoints.

- Post-merge advisory findings on the climb glue: content-fingerprint drift
  check (rewrites during eval, not just new files), leader best never
  regresses, climb exceptions record aborted (never a stale implementing),
  zero-change improvements rejected, redacted publish logs; pushed
  branches are never deleted on publish failure — kept and recorded in the
  run note for a future sweeper (deletion could close a real PR).

- Hardening from the first adversarial review pass: local git runs without
  credentials and with hooks disabled (planted hooks can no longer read the
  PAT), scope paths are normalized component-wise, self-target matching accepts
  any repo spelling, contract YAML rejects aliases/duplicate keys/oversized
  input, commits are vetoed against the contract's forbidden paths, and API
  errors/response shapes are typed instead of crashing.

### Added

- Steward follow-ups: comments on a steward PR resume the steward session
  with ITS key and ITS scope check, and pushed changes run the steward
  ruler (suite + sibling smoke-checks + re-measure) with an
  orchestrator-re-based ledger row — never the solver's improvement math.
  And in every follow-up wake, comments WITHOUT standing (verifier and
  advisory rounds) now ride along data-fenced as context — they still
  never trigger or steer, but the woken agent finally sees what "address
  the findings" refers to without a human relaying the text.

- Run endings talk back to the work order: when a run that started from an
  issue ends by PR merge, the issue gets a closing nudge (the claim stays
  held, so an open order queues nothing — closing is the human's call);
  a steward PR closed unmerged releases its claim right there with honest
  wording instead of the reconciliation pass calling a deliberate "no" a
  crash.

- The benchmark steward (`steward` module + tick lane): maintainer-filed
  work orders (issues labeled `autoresearch:steward`, standing-gated) run
  a session whose territory is the solver's INVERSE — the contract's new
  `steward.allowed` paths (env generators, eval harness, tests), with the
  solver's scope and the always-forbidden set blocked in code. Its ruler
  is validation, not improvement: the orchestrator runs the target's test
  suite contained, re-measures the named benchmark with the CURRENT
  solver, and writes the reset record rows from its own measurement.
  Steward PRs are bot-authored, so the verifier reads them adversarially;
  identity is `steward-01` with its own key
  (`AUTORESEARCH_STEWARD_KEY_FILE`; the lane stays off until
  provisioned).

- Display precision follows convention, not float repr (maintainer
  decision): human surfaces — PR tables, BENCHMARKS.md, the rewritten
  candidate row — render at the benchmark's `display_digits` (contract
  knob, 2–12 significant digits, default 6); full precision lives only in
  `results/leader.json`, and every comparison runs on full floats, so
  display can never hide or fake an improvement.
- Follow-up pushes rewrite the PR body's measured candidate row in place
  (the one mechanical, orchestrator-owned number that must never go
  stale), scoped to the preamble so report text with a lookalike row is
  untouched; the narrative is never rewritten — the Edit block points at
  the replies.

- The roles now read each other (both gaps found live on the first
  verifier exchange): the verifier's context includes the PR discussion —
  bounded, with its own prior rounds marked as its own findings to
  re-check — and a follow-up that pushes code appends an **Edit** addendum
  to the PR body, so the report frozen at publish is never mistaken for
  the current state.

- The verifier (`verifier`, `verifier_cli`, reusable workflow
  `verify.yml`): adversarial integrity reads of BOT-authored PRs — the
  advisory reviewer's mirror. Gaming lens (harness exploitation,
  ruler-fishing, leakage, overfitting, unsupported claims, measurement
  gaps) with the contract and the frozen ruler's source fetched from the
  BASE branch into context; findings tagged by category; a header stating
  a clean read does not certify the result; its own spend-capped key
  (`ANTHROPIC_VERIFIER_KEY`). The advisory workflow's interim
  bot-PR-on-label override is now opt-in (`review_bot_prs_on_label`,
  default false) for verifier-less self-hosters — with a verifier
  deployed, the label routes bot PRs to it.

- `docs/design/roles.md`: the one-page cast — every role (live, next, and
  designed) with its authority and its "may never" column, mermaid flow
  diagrams for the improvement pipeline and review routing, the
  collusion-separation table, and where state lives. architecture.md's
  stale three-line Roles section now points there.

- Design docs for the meta track: `docs/design/meta.md` (pilot-vs-meta
  taxonomy, two-tier GitHub simulator, battery + scorecard with paired
  repetitions, sim-only self-improvement, benchmark-steward role) and
  `docs/design/public-surface.md` (threat model for public target repos;
  the `pull_request_target` audit checklist that gates any public flip).

- Per-repo budget shaping: a contract's `budgets:` may now set
  `session_max_turns`, `session_minutes`, `climb_job_minutes`, and
  `followup_job_minutes`. Contracts are untrusted, so every value is
  clamped into orchestrator-side [floor, ceiling] bounds (`limits` module)
  with CEILING == DEFAULT — contracts are merged by target-repo
  maintainers, so the knobs shape strictly downward; raising a budget is
  orchestrator-side config — and the session is shrunk to fit inside its
  job. The tick fetches the contract
  once per cycle and threads the limits through all three launch lanes.
- Self-deadline: climbs arm a SIGALRM at walltime-minus-margin (default
  120s, floor 60s, `--deadline-margin-s`) raising into the ordinary
  containment — the only pre-kill warning on clusters that deliver no
  signals to job processes (measured on Torch 2026-08-08). The tick passes
  each climb its own walltime via `--job-minutes`.

- The advisory reviewer posts one comment PER ROUND (numbered, stamped
  with the reviewed head SHA) instead of editing a single thread: under
  review-until-quiet, humans must see each round — comment edits fire no
  notifications and bury prior rounds in edit history. The upsert guarded
  against synchronize-era spam; runs are now open- or label-triggered
  only.

- Killed climbs now reach a recorded ending: the record stores the climb's
  own Slurm job id and a new sweep pass ends `implementing` records whose
  job is terminal — Slurm truth plus grace, outage never reads as dead.
  Previously only crashes (Python exceptions) were contained; kills
  stranded the record forever. A SIGTERM handler also raises into the
  ordinary containment, but measurement (Torch, 2026-08-08) showed Slurm
  delivers no signal to job processes there — on such clusters the sweep
  and the self-deadline (below) are the real teardown paths; the handler
  covers direct kills and other sites.

- Adding the `autoresearch:review` label runs a review on bot-authored
  PRs: the labeling EVENT (re-request by removing and re-adding, same as
  on human PRs) is an explicit ask. Superseded within this release by the
  verifier (below): the label now routes bot PRs to the verifier, and the
  advisory-reviewer override became opt-in (`review_bot_prs_on_label`,
  default false) for verifier-less self-hosters. The opt-out label still
  wins over contradictory signals.

- Review-until-quiet merge gate documented (CONTRIBUTING, CLAUDE.md):
  development PRs iterate advisory-review rounds with a judged termination
  criterion — code PRs stop at no new medium+/behavior-affecting findings,
  docs/process PRs get one round with nits batched, and a 4-round hard cap
  escalates to the PI. Contributor docs also updated for the single
  consolidated `ci` check (stale five-check wording).

- Publish-time freshness: when the base branch moves during a climb, the
  run branch merges the fresh base (merge commit, never rebase), the claim
  is re-measured on the merged tree, and the leader check runs against the
  fresh ledger — before anything is pushed; both sides are measured in
  throwaway worktrees of commits (equivalent pristine environments; the
  shas pin content). A conflicting merge or an absorbed improvement ends
  the run honestly instead of opening an unmergeable or unverified PR, and
  a final base re-check before push narrows (not eliminates) the window.

- Improvement PRs arm GitHub auto-merge at publish (best-effort): the bot
  still never merges — arming hands the merge to the human approval that
  branch protection requires, so approving is the last human action needed.

- Disk preflight (`disk` module): quota exhaustion is invisible on some
  clusters until a write fails, so the tick and the climb now write-probe
  their storage (plus a statvfs early-warning threshold, `--min-free-gb`)
  before launching new work. A failed preflight turns the launch lanes off
  for the tick, surfaces in the heartbeat and the tick summary, and never
  kills the chain; the climb refuses to start and tells the issue. The
  chain script defaults `UV_CACHE_DIR` and `APPTAINER_CACHEDIR` under the
  state root so host-side caches never land in a constrained $HOME.

- Crash containment in live climbs: the run record is saved before any
  network or clone work, the claim block runs inside the contained region,
  and every ending step (record, report, issue post) degrades independently
  — a full disk cannot block the GitHub failure report, and a network
  failure cannot block the record. The tick summary line now reports the
  intake and self-initiated lanes.

- Self-initiated lane: when the requested lane claims nothing and no run is
  active, the tick launches a climb on the least-recently-attempted contract
  benchmark, within the weekly budget and a per-benchmark cooldown. The
  planning agent later replaces this picker with motivated, vetoable plans.

- The requested lane: maintainer issues on the target repo become runs. The
  tick claims at most one qualifying issue per cycle (standing-gated,
  single-benchmark-named, claim-marker deduped); the issue text enters the
  brief data-fenced, the PR says "Addresses #N", and the run report lands
  back on the issue thread.

- The tick now services in-review runs automatically: PR merge/close ends the
  run; new qualifying review comments submit a follow-up job that wakes the
  authoring session (lease-guarded; `followup_job_id` prevents duplicate
  queueing). The chain passes the PAT path to the tick for GitHub reads;
  agent sessions remain scrubbed by the harness allowlist.

- `autoresearch.followup`: the in-review path — PR merge/close ends the run;
  qualifying maintainer comments (author-association gate) resume the
  authoring session in its retained workspace, and the reply lands on the
  thread as the bot, with any code change scope-checked, drift-checked,
  re-measured, and pushed to the PR branch.

- `autoresearch.climb`: one live climb end to end — bot-auth clone, contract
  from the target tree, contained session + eval, full-scope commit veto,
  push, PR with orchestrator-measured numbers, durable run record and report.
  `create_pull` on the GitHub client.

- Phase 5 core: `orchestrator.climb_once` (single-benchmark climb with
  orchestrator-measured baseline/candidate — the agent's claim is never
  trusted) and Apptainer session containment in the harness
  (`container_image`: --containall with workspace + per-run-HOME binds only;
  the API key travels via APPTAINERENV_, never argv).

- Phase 4 loop plumbing: `compute` (Slurm submit/status/cancel behind an
  injectable runner; afterany wake jobs; query-failure ≠ job-gone),
  `runstate` (atomic run records, six endings, expiring wake leases with
  handoff), `tick` (pause sentinel, heartbeat, the five-layer fail-safe
  sweep), and the self-resubmitting chain script.

- `autoresearch.brief` (typed, bounded, replayable session briefs — the
  context-engineering artifact) and `autoresearch.harness` (backend seam;
  Claude Code adapter with scrubbed session env, timeouts, and key-redacted
  transcripts; fake adapter for tests) — phase 3.

- Deployment hardening for the reviewer: operational LLM errors map to
  `CompleterError` (an expected failure — never reds a target repo's CI),
  missing API key skips cleanly, and the reusable workflow accepts a read-only
  deploy key so private self-hosted copies work. README rewritten for external
  adopters.
- `autoresearch.contract_cli`: validate a `.autoresearch.yaml` locally and print
  what the agent would be allowed to do.
- `autoresearch.review` + reusable advisory-review workflow: constrained PR
  reviewer (never on bot PRs, opt-out label, one thread per PR, advisory header),
  Anthropic completer behind an injectable interface, `docs/install.md` for
  self-hosting (phase 2).
- `autoresearch.github`: REST client + git workspace behind a token-provider
  seam (GitHub App-ready), env-injected git auth, dry-run mode (phase 1).
- `autoresearch.contract`: schema + loader for `.autoresearch.yaml` with suite
  aggregates and hard-coded safety invariants (phase 1).
- Repository scaffold: package skeleton, CI gates (lint/types/test/lock/gitleaks),
  pre-commit hooks, governance docs, architecture design, roadmap.
