# Environment variables

Every environment variable you set to configure an Outerloop deployment:
its meaning, its default and when it is required. Most live in the operator
settings file (`~/.config/outerloop/.env`, written by `outerloop init` and
read at each launch). The launch exports an allowlist from the file, so a
variable outside that allowlist must be exported in the shell around
`outerloop start`; the sections below mark those with "the shell only".
See [the install guide](../install.md) for the whole
deployment procedure.

Variables the kernel sets for its own child sessions are not listed here.
A flag always beats the environment. For a foreground loop the environment
beats the settings file, and an empty value in the settings file is an
off-switch, not an unset key. The resident chain is the exception: it
re-reads its allowlist from the file each tick, and a present entry wins
over the inherited environment even when it is empty.

## Variable summary

| Variable | Meaning | Default |
| --- | --- | --- |
| [`OUTERLOOP_COMPUTE`](#outerloop_compute) | run the local loop instead of the Slurm chain | unset |
| [`OUTERLOOP_ROOT`](#outerloop_root) | state root | `~/.outerloop` |
| [`OUTERLOOP_HOME`](#outerloop_home) | the kernel source checkout to deploy from | start directory |
| [`OUTERLOOP_ACCOUNT`](#outerloop_account) | Slurm account for the tick | unset |
| [`OUTERLOOP_PARTITION`](#outerloop_partition) | Slurm partition for the tick | unset |
| [`OUTERLOOP_QOS`](#outerloop_qos) | Slurm QOS for submitted jobs | unset |
| [`OUTERLOOP_TICK_HOST`](#outerloop_tick_host) | resident job or login host for the Slurm tick | `resident` |
| [`OUTERLOOP_RESIDENT_MINUTES`](#outerloop_resident_minutes) | walltime of the resident tick job | 360 |
| [`OUTERLOOP_CADENCE_MIN`](#outerloop_cadence_min) | minutes between ticks | 30 |
| [`OUTERLOOP_AUTO_UPDATE`](#outerloop_auto_update) | how the deploy step moves the checkout | `off` |
| [`OUTERLOOP_ENV_FILE`](#outerloop_env_file) | path to the operator settings file | `~/.config/outerloop/.env` |
| [`OUTERLOOP_PAT_FILE`](#outerloop_pat_file) | path to the GitHub PAT file | unset |
| [`OUTERLOOP_IMAGE`](#outerloop_image) | Apptainer image for sessions and evals | `~/outerloop-images/agent-py312.sif` |
| [`OUTERLOOP_UNCONTAINED`](#outerloop_uncontained) | run a cluster deployment without a container | off |
| [`OUTERLOOP_APPTAINER_BIN`](#outerloop_apptainer_bin) | apptainer binary | `apptainer` on PATH |
| [`OUTERLOOP_CACHE_ROOT`](#outerloop_cache_root) | where downloaded runtimes are cached | `<root>/cache` |
| [`OUTERLOOP_BRIDGE_RUNTIME`](#outerloop_bridge_runtime) | where the bridge runtime is installed | `<cache>/bridge` |
| [`OUTERLOOP_TARGET`](#outerloop_target) | the repo being climbed | unset |
| [`OUTERLOOP_GITHUB_APP_FILE`](#outerloop_github_app_file) | the GitHub App config file init writes | unset |
| [`OUTERLOOP_BOT_LOGIN`](#outerloop_bot_login) | the login the kernel acts as | unset |
| [`OUTERLOOP_BOT_ALIASES`](#outerloop_bot_aliases) | former logins of the same bot | unset |
| [`OUTERLOOP_AUTHOR_BACKEND`](#outerloop_author_backend) | the author's harness | `claude` |
| [`OUTERLOOP_AUTHOR_MODEL`](#outerloop_author_model) | the author's model | unset |
| [`OUTERLOOP_CLAUDE_MODEL`](#outerloop_claude_model) | model for every Claude role | unset |
| [`OUTERLOOP_AUTHOR_ENDPOINT`](#outerloop_author_endpoint) | endpoint profile for the author | unset |
| [`OUTERLOOP_AUTHOR_OVERRIDES`](#outerloop_author_overrides) | per-target author selection | unset |
| [`OUTERLOOP_PANEL`](#outerloop_panel) | the review panel's lenses | `verify,review` |
| [`OUTERLOOP_PANEL_KEY_FILE`](#outerloop_panel_key_file) | key for a Claude panel lens | `~/.config/outerloop/verifier_key` |
| [`OUTERLOOP_PANEL_CODEX_KEY_FILE`](#outerloop_panel_codex_key_file) | key for a Codex panel lens | unset |
| [`OUTERLOOP_PANEL_HERMES_KEY_FILE`](#outerloop_panel_hermes_key_file) | key for a Hermes panel lens | unset |
| [`OUTERLOOP_PANEL_UNCONTAINED`](#outerloop_panel_uncontained) | run panel judges outside the container | off |
| [`OUTERLOOP_STEWARD_KEY_FILE`](#outerloop_steward_key_file) | the steward's key; turns the lane on | unset |
| [`OUTERLOOP_CLAUDE_KEY_FILE`](#outerloop_claude_key_file) | the author's Claude key file | `~/.config/outerloop/claude_key` |
| [`OUTERLOOP_CODEX_KEY_FILE`](#outerloop_codex_key_file) | the author's Codex key file | `~/.config/outerloop/codex_key` |
| [`OUTERLOOP_HERMES_KEY_FILE`](#outerloop_hermes_key_file) | the author's Hermes key file | `~/.config/outerloop/hermes_key` |
| [`REVIEW_BACKEND`](#review_backend) | backend for the standalone reviewer | `claude` |
| [`REVIEW_MODEL`](#review_model) | model for the standalone reviewer | unset |
| [`REVIEW_ENDPOINT`](#review_endpoint) | endpoint profile for the standalone reviewer | unset |
| [`OUTERLOOP_TOKEN_PRICES`](#outerloop_token_prices) | token prices for session cost accounting | unset |
| [`OUTERLOOP_VERTEX_PROJECT`](#outerloop_vertex_project) | GCP project that routes Claude through Vertex | unset |
| [`OUTERLOOP_VERTEX_REGION`](#outerloop_vertex_region) | Vertex region | `global` |
| [`OUTERLOOP_VERTEX_ADC`](#outerloop_vertex_adc) | application default credentials file | the gcloud default |
| [`OUTERLOOP_VERTEX_SMALL_MODEL`](#outerloop_vertex_small_model) | a cheaper model for auxiliary roles | unset |
| [`OUTERLOOP_CLAUDE_VERSION`](#outerloop_claude_version-and-outerloop_claude_sha256) | Claude CLI version pin override | the packaged pin |
| [`OUTERLOOP_CLAUDE_SHA256`](#outerloop_claude_version-and-outerloop_claude_sha256) | checksum for the Claude override | required with the version |
| [`OUTERLOOP_CODEX_VERSION`](#outerloop_codex_version-outerloop_codex_sha256-and-outerloop_codex_code_mode_host_sha256) | Codex CLI version pin override | the packaged pin |
| [`OUTERLOOP_CODEX_SHA256`](#outerloop_codex_version-outerloop_codex_sha256-and-outerloop_codex_code_mode_host_sha256) | checksum for the Codex override | required with the version |
| [`OUTERLOOP_CODEX_CODE_MODE_HOST_SHA256`](#outerloop_codex_version-outerloop_codex_sha256-and-outerloop_codex_code_mode_host_sha256) | checksum of the Codex code-mode host | required with the version |
| [`OUTERLOOP_HERMES_REF`](#outerloop_hermes_ref-and-outerloop_hermes_sha) | Hermes source ref override | the packaged pin |
| [`OUTERLOOP_HERMES_SHA`](#outerloop_hermes_ref-and-outerloop_hermes_sha) | Hermes commit SHA override | required with the ref |
| [`OUTERLOOP_CLAUDE_BIN`](#outerloop_claude_bin) | absolute path of the Claude CLI | PATH lookup |
| [`OUTERLOOP_CODEX_BIN`](#outerloop_codex_bin) | absolute path of the Codex CLI | PATH lookup |
| [`OUTERLOOP_OPERATOR_HARNESSES`](#outerloop_operator_harnesses) | harness CLIs the site supplies, left alone by upgrades | unset |
| [`REVIEW_HERMES_REPO`](#review_hermes_repo) | Hermes source checkout | `~/hermes-agent` |
| [`REVIEW_HERMES_PROVIDER`](#review_hermes_provider) | native Hermes provider, `openai` or `openrouter` | unset |
| [`OUTERLOOP_CODEX_WEB_SEARCH`](#outerloop_codex_web_search) | Codex's built-in web search | `auto` |
| [`OUTERLOOP_HERMES_RESUME_MAX_CHARS`](#outerloop_hermes_resume_max_chars) | Hermes resume payload cap | 120000 |
| [`OUTERLOOP_GPU_PARTITION`](#outerloop_gpu_partition) | the GPU lane for evals and launches | unset |
| [`OUTERLOOP_GPU_ACCOUNT`](#outerloop_gpu_account) | account for the GPU lane | unset |
| [`OUTERLOOP_GPU_LANES`](#outerloop_gpu_lanes) | per-target GPU lanes | unset |
| [`OUTERLOOP_EVAL_CPUS_PER_GPU`](#outerloop_eval_cpus_per_gpu) | CPUs per GPU for dispatched evals | 8 |
| [`OUTERLOOP_EVAL_MEM_GB_PER_GPU`](#outerloop_eval_mem_gb_per_gpu) | memory per GPU for dispatched evals | 64 |
| [`OUTERLOOP_LOCAL_GPUS`](#outerloop_local_gpus) | GPU count for the local compute pool | auto-detected |
| [`OUTERLOOP_MIN_TICK_MINUTES`](#outerloop_min_tick_minutes) | coalesce window for piled-up ticks | 10 minutes or half the cadence |
| [`OUTERLOOP_MAX_JOB_MINUTES`](#outerloop_max_job_minutes) | walltime cap for climb jobs | 360 |
| [`OUTERLOOP_JOB_PARTITION`](#outerloop_job_partition) | partition the walltime cap is sized for | unset |
| [`OUTERLOOP_DISPATCH_WAKE`](#outerloop_dispatch_wake) | `0` disarms dispatched wakes | armed |
| [`OUTERLOOP_HARNESS_TIMEOUT_SECONDS`](#outerloop_harness_timeout_seconds) | deadline for one harness upgrade | 300 |
| [`OUTERLOOP_NATIVE_LOG_MAX_BYTES`](#outerloop_native_log_max_bytes) | cap on a session's native log kept | 32 MiB |

## Placement and state

### `OUTERLOOP_COMPUTE`

`local` runs the loop in the foreground instead of submitting to Slurm,
even where `sbatch` exists. Any other value runs the Slurm chain where
`sbatch` is on PATH.

- Values: `local` or anything else
- Default: unset

### `OUTERLOOP_ROOT`

The state root: runs, workspaces, logs and the tick lease live under it. On
Slurm it must sit on the shared filesystem, and start refuses without it.

- Default: `~/.outerloop`
- Required: on Slurm, from the flag, the environment or the settings file

### `OUTERLOOP_HOME`

The kernel source checkout the resident chain deploys from. A foreground
loop has no deploy step and can run the installed package, so it falls back
to a `home` directory under the state root.

- Default: the directory `outerloop start` runs from

### `OUTERLOOP_ACCOUNT`

The Slurm account the tick job bills to. Unset bills your default Slurm
association.

- Default: unset

### `OUTERLOOP_PARTITION`

The Slurm partition the tick job runs on. Unset lets Slurm choose its
default. A comma-separated list submits to whichever frees up first.

- Default: unset

### `OUTERLOOP_QOS`

The Slurm QOS the tick and its jobs request.

- Default: unset

### `OUTERLOOP_TICK_HOST`

Where the Slurm tick runs. `resident` is a self-handover job on the
cluster, and `login` runs the loop in the foreground on this host against
Slurm. A login loop yields CPU to other users of the login node.

- Values: `resident` or `login`
- Default: `resident`

### `OUTERLOOP_RESIDENT_MINUTES`

The walltime of the resident tick job. The job hands over to itself near
the end, so the value is the chain's self-healing interval, not its
lifetime.

- Default: 360
- Constraint: a positive whole number of minutes

### `OUTERLOOP_CADENCE_MIN`

How often the chain schedules a tick, in minutes. The chain divides by it
and the foreground loop sleeps on it.

- Default: 30
- Constraint: a positive number

### `OUTERLOOP_AUTO_UPDATE`

How the chain's deploy step moves the kernel checkout. `off` runs it as
is, `release` moves to the newest release tag, and `main` follows every
merge. The login loop never auto-updates, whatever this says.

- Values: `off`, `release` or `main`
- Default: `off`

### `OUTERLOOP_ENV_FILE`

The operator settings file to read instead of the default, for running a
second instance beside the first. Must be an absolute path, owned by you and
not group- or world-writable, or it is refused.

- Default: `~/.config/outerloop/.env`

### `OUTERLOOP_PAT_FILE`

The file holding the GitHub personal access token, when the identity is a
PAT rather than a GitHub App. One identity or the other, never both.

- Default: unset

### `OUTERLOOP_IMAGE`

The Apptainer image sessions and evals run in. An empty value is the
off-switch for local Claude runs: they stay uncontained even when an image
is on disk. On Slurm the tick does not service a target without an image,
and Codex and Hermes authors need one in every mode.

- Default: `~/outerloop-images/agent-py312.sif`

### `OUTERLOOP_UNCONTAINED`

`1` runs a Slurm deployment without a container: sessions under the
harness's own sandbox, evaluations bare, as jobs on your account. The image
is ignored. A session can then read any file the account can, keys
included. See [running a cluster without a container](../install.md#running-a-cluster-without-a-container).

- Values: `1`
- Default: off

### `OUTERLOOP_APPTAINER_BIN`

The apptainer binary the tick uses, for clusters where it is not on PATH.

- Default: `apptainer` on PATH

### `OUTERLOOP_CACHE_ROOT`

Where downloaded harness runtimes and images are cached, when the default
under the state root is not right.

- Default: `<root>/cache`

### `OUTERLOOP_BRIDGE_RUNTIME`

Where the Codex bridge runtime is installed.

- Default: `<cache>/bridge`

## Identity and GitHub

### `OUTERLOOP_TARGET`

The repository being climbed, `owner/repo`. The tick does not service a
target without it.

- Default: unset
- Required: always

### `OUTERLOOP_GITHUB_APP_FILE`

The GitHub App config file `outerloop init` writes, `github_app.<slug>.json`.
It is Outerloop's own JSON, holding `app_id`, `installation_id` and
`private_key`, the path of the separate PEM file beside it. With
`OUTERLOOP_PAT_FILE` unset, this is the identity the kernel posts as.

- Default: unset

### `OUTERLOOP_BOT_LOGIN`

The login the kernel acts as, so it can recognize its own comments. `init`
records it: the App's login is `<slug>[bot]`, and a PAT's is the token's
login. When init cannot read the PAT's login, set the variable by hand in
the settings file, or rerun `outerloop init --force` with network access.

- Default: unset
- Required: for both identities; the tick services no target without it

### `OUTERLOOP_BOT_ALIASES`

Former logins of the same bot, comma-separated. After an identity flip,
recognition of the kernel's own comments stays keyed on login, so the set
of its logins must widen by hand.

- Default: unset

## Authors, panel and models

### `OUTERLOOP_AUTHOR_BACKEND`

The harness the climbing author runs on.

- Values: `claude`, `codex` or `hermes`
- Default: `claude`

### `OUTERLOOP_AUTHOR_MODEL`

The author's model, overriding `OUTERLOOP_CLAUDE_MODEL` for the author
only. A Hermes author needs it, in your provider's model ID.

- Default: unset

### `OUTERLOOP_CLAUDE_MODEL`

The model for every Claude role: the author when its backend is claude,
the panel judges that name no model of their own, and the steward. There
is no code default. Start refuses with the list of roles that need it.

- Default: unset
- Required: whenever a Claude role runs without its own model

### `OUTERLOOP_AUTHOR_ENDPOINT`

The endpoint profile the author's model is served by. See
[the endpoints guide](../endpoints.md).

- Default: unset

### `OUTERLOOP_AUTHOR_OVERRIDES`

A JSON object selecting a different author for individual targets, or for
individual slots of one target. Each entry names a backend, a model and
optional `slots` (`agent-01`, ...), plus optional `session_minutes` and
`session_max_turns` within the orchestrator's bounds.

```bash
OUTERLOOP_AUTHOR_OVERRIDES='{"owner/repo":{"backend":"codex","model":"gpt-5.2","slots":["agent-02"]}}'
```

- Default: unset

### `OUTERLOOP_PANEL`

The panel's lenses, comma-separated. Each entry is `kind`, `kind:backend`
or `kind:backend:model`. A lens that names no model runs the author's model
when it runs the author's backend, and must name one on any other backend.
Endpoint-backed judges use `kind:backend:model[endpoint=profile]`.

- Default: `verify,review`

### `OUTERLOOP_PANEL_KEY_FILE`

The key file for a Claude panel lens, kept separate from the author's key.

- Default: `~/.config/outerloop/verifier_key`

### `OUTERLOOP_PANEL_CODEX_KEY_FILE`

The key file for a Codex panel lens: the judge's own key, never the
author's.

- Default: unset
- Required: for a Codex panel lens

### `OUTERLOOP_PANEL_HERMES_KEY_FILE`

The key file for a Hermes panel lens, resolved like the Codex one.

- Default: unset
- Required: for a Hermes panel lens

### `OUTERLOOP_PANEL_UNCONTAINED`

`1` runs the panel judges outside the container, on the host. Off by
default, because an uncontained judge holds a shell on the launch node.

- Values: `1`
- Default: off

### `OUTERLOOP_STEWARD_KEY_FILE`

The steward's own key file. Setting it turns the steward lane on, and
the steward always runs Claude, so this names a Claude key. The lane also
needs a steward scope in the contract. Unset by default: creating the
file at the default path is not enough, the variable must name it.

- Default: unset

### `OUTERLOOP_CLAUDE_KEY_FILE`

The author's Claude model key file.

- Default: `~/.config/outerloop/claude_key`

### `OUTERLOOP_CODEX_KEY_FILE`

The author's Codex model key file.

- Default: `~/.config/outerloop/codex_key`

### `OUTERLOOP_HERMES_KEY_FILE`

The author's native-provider key file for a Hermes author.

- Default: `~/.config/outerloop/hermes_key`

### `REVIEW_BACKEND`

The backend the standalone reviewer runs on, in its own GitHub workflow.
It does not select the climber's panel lenses; those come from
`OUTERLOOP_PANEL`.

- Values: `claude`, `codex` or `hermes`
- Default: `claude`

### `REVIEW_MODEL`

The model the standalone reviewer runs on. The claude backend has no
built-in default, so a claude reviewer without a model skips with a
could-not-run stub.

- Default: unset

### `REVIEW_ENDPOINT`

The endpoint profile the standalone reviewer's model is served by.

- Default: unset

### `OUTERLOOP_TOKEN_PRICES`

A JSON table of USD prices per million tokens, keyed by model name and
token kind, that prices session evidence. No prices are built in, and
without an entry a session's cost stays unpriced.

```bash
OUTERLOOP_TOKEN_PRICES='{"claude-sonnet-5-5":{"input_tokens":3,"output_tokens":15}}'
```

- Default: unset

## Vertex

### `OUTERLOOP_VERTEX_PROJECT`

The GCP project that routes Claude sessions through Vertex AI, billed to
that project. Setting it is the whole switch, and unsetting it falls back
to the API key.

- Default: unset

### `OUTERLOOP_VERTEX_REGION`

The Vertex region.

- Default: `global`

### `OUTERLOOP_VERTEX_ADC`

The application default credentials file Vertex authenticates with.
Unset uses the gcloud default under your home directory.

- Default: the gcloud default

### `OUTERLOOP_VERTEX_SMALL_MODEL`

The small fast model Claude sessions use for background tasks, which
otherwise defaults to the session's main model.

- Default: unset

## Harness pins and CLIs

### `OUTERLOOP_CLAUDE_VERSION` and `OUTERLOOP_CLAUDE_SHA256`

Override the packaged Claude CLI version pin. The version requires its
SHA-256 checksum.

- Default: the packaged pin

### `OUTERLOOP_CODEX_VERSION`, `OUTERLOOP_CODEX_SHA256` and `OUTERLOOP_CODEX_CODE_MODE_HOST_SHA256`

Override the packaged Codex CLI version pin. The version requires both
checksums.

- Default: the packaged pin

### `OUTERLOOP_HERMES_REF` and `OUTERLOOP_HERMES_SHA`

Override the packaged Hermes source pin, a ref and its full commit SHA.
Setting one requires the other.

- Default: the packaged pin

### `OUTERLOOP_CLAUDE_BIN`

The absolute path of the Claude CLI. Recorded by `init` so every job
spawns the same binary you installed, and an unrunnable path is refused at
start.

- Default: PATH lookup, then `~/.local/bin/claude`

### `OUTERLOOP_CODEX_BIN`

The absolute path of the Codex CLI, recorded like the Claude one.

- Default: PATH lookup, then `~/.local/bin/codex`

### `OUTERLOOP_OPERATOR_HARNESSES`

Harness CLIs the site supplies itself, comma-separated from `claude`,
`codex`, `hermes` and `bridge`. Upgrades leave them alone, and
`harness status` reports them as `OPERATOR`. See [harness pins and
upgrades](../install.md#harness-pins-and-upgrades).

- Default: unset

### `REVIEW_HERMES_REPO`

The Hermes source checkout, pinned and installed. Required when any role
runs Hermes.

- Default: `~/hermes-agent`

### `REVIEW_HERMES_PROVIDER`

The provider a native Hermes author or reviewer authenticates with. Set
`openai` or `openrouter`; anything else fails validation unless an endpoint
profile covers the role. With `openrouter`, name models in OpenRouter's
`provider/model` form, such as `openai/gpt-5.6-terra`. With `openai`, use
the provider's native id, such as `gpt-5.6-terra`, with no prefix.

- Values: `openai` or `openrouter`
- Default: unset; the standalone reviewer alone defaults to `openrouter`

### `OUTERLOOP_CODEX_WEB_SEARCH`

Whether a Codex session keeps its built-in web search. `auto` keeps it on
native provider sessions and turns it off on endpoint sessions, where the
provider-side tool cannot run. Any other value turns it off everywhere.

- Values: `auto` or off
- Default: `auto`

### `OUTERLOOP_HERMES_RESUME_MAX_CHARS`

The cap on a Hermes session's resume payload, in characters, brief plus
latest results. When required context cannot fit below it, the run parks
with a configuration-blocked notice naming the value to raise it to.

- Default: 120000

## GPU and dispatch

### `OUTERLOOP_GPU_PARTITION`

The partition GPU evals and author launches run on. A contract with GPU
benchmarks needs this lane, and without one the tick refuses to launch
attempts on them rather than queue evals that can never run. Local compute
has no lanes, so it needs none, and a target entry in `OUTERLOOP_GPU_LANES`
satisfies the check without this variable.

- Default: unset
- Required: for GPU benchmarks on lane-based compute (Slurm)

### `OUTERLOOP_GPU_ACCOUNT`

The account for the GPU lane, when it differs from the tick's.

- Default: unset

### `OUTERLOOP_GPU_LANES`

A JSON object naming a GPU lane per target, overriding the fleet default.
Each lane has a partition and optional `account`, `gpu_type` and `extra`
flags. Extra flags may not repeat one the kernel sets.

```bash
OUTERLOOP_GPU_LANES='{"owner/repo":{"partition":"gpu-large","account":"my-account","gpu_type":"a100"}}'
```

- Default: unset

### `OUTERLOOP_EVAL_CPUS_PER_GPU`

The CPUs a dispatched GPU eval asks for per GPU, for nodes leaner than
the default sizing.

- Default: 8

### `OUTERLOOP_EVAL_MEM_GB_PER_GPU`

The memory, in GB, a dispatched GPU eval asks for per GPU.

- Default: 64

### `OUTERLOOP_LOCAL_GPUS`

The number of GPUs the local compute pool offers, when automatic
detection gets it wrong or misses them.

- Default: auto-detected
- Constraint: a non-negative integer
- Configuration: the shell only

## Loop pacing

### `OUTERLOOP_MIN_TICK_MINUTES`

The window within which a piled-up tick is skipped as redundant, so
congestion on the partition does not run back-to-back sweeps. Zero
disables coalescing. A value at or above half the cadence is clamped down
so it cannot stall the loop.

- Default: the lesser of 10 minutes and half the cadence
- Configuration: the shell only

### `OUTERLOOP_MAX_JOB_MINUTES`

The walltime cap for climb jobs, kept under the job partition's `MaxTime`
so Slurm does not reject the submission outright. Clamped between the
40-minute floor and the 10-hour ceiling.

- Default: 360
- Constraint: 40 to 600
- Configuration: the shell only

### `OUTERLOOP_JOB_PARTITION`

The partition climb jobs run on, when it differs from the tick's. Name a
partition with a longer `MaxTime` to raise the walltime cap with
`OUTERLOOP_MAX_JOB_MINUTES`.

- Default: unset
- Configuration: the shell only

### `OUTERLOOP_DISPATCH_WAKE`

`0`, `off`, `false` or `no` disarms dispatched wakes, so a run waiting on
jobs is woken by the next tick instead of its own wake job. The same
disarm comes from touching `<root>/DISARM_WAKE`.

- Values: `0` disarms, anything else is armed
- Default: armed
- Configuration: the shell only

## Diagnostics

### `OUTERLOOP_HARNESS_TIMEOUT_SECONDS`

The deadline for one harness upgrade, installation and verification
together. A longer install, such as the Hermes runtime, is cut off at this
deadline. Probes in `outerloop harness status` use their own short fixed
timeouts.

- Default: 300
- Constraint: positive and finite
- Configuration: the shell only

### `OUTERLOOP_NATIVE_LOG_MAX_BYTES`

How much of a session's native harness log is kept as evidence.

- Default: 32 MiB
- Configuration: the shell only

## Endpoint profiles

Self-hosted model endpoints are named by profile variables rather than one
variable each. A profile named `onprem` is five keys:
`OUTERLOOP_ENDPOINT_ONPREM_URL` (or `_URL_FILE`), `OUTERLOOP_ENDPOINT_ONPREM_KEY_FILE`,
`OUTERLOOP_ENDPOINT_ONPREM_MODEL`, and `OUTERLOOP_ENDPOINT_ONPREM_API` for
the API dialect. Roles select a profile with
`model[endpoint=onprem]`, and `OUTERLOOP_AUTHOR_ENDPOINT` selects one for
the author. See [the endpoints guide](../endpoints.md).
