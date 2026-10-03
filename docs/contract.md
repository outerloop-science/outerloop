# The contract

One file in the target repo. The minimum:

```yaml
benchmarks:
  - name: my-benchmark
    command: uv run python -m mypkg.eval --json   # prints {"success_rate": 0.42}
    metric: success_rate
    direction: max
budgets:
  gpu_hours_per_run: 8
  runs_per_week: 10
scope:
  allowed: [src/]                                # the ONLY paths the agent may write
roadmap: docs/roadmap.md
```

The knobs that shape a climb, all optional:

| Knob | What it decides |
| --- | --- |
| `seed_env`, `min_delta` / `min_delta_rel` | Paired seeding for resampled evals, and the significance floor a delta must clear — calibrate it from seed variance, the gate enforces it |
| `eval_minutes`, `gpus` | Evals that need their own job (and GPUs) are dispatched to the cluster rather than run in the author's job |
| `baseline: paired \| cached` | Re-measure the base tree beside every candidate, or measure it once per base and run only candidates |
| `depth_k`, `sleep_k` | How many experiments an author may launch and how many times it may sleep for results |
| `max_active_attempts`, `attempt_cooldown_minutes` | Width: authors abreast on one target; pacing between attempts (0 for a hot loop) |
| `max_concurrent_gpus` | The pace ceiling for an author's sweeps, in GPUs: a sweep runs at most this many GPUs' worth of tasks at once (`--array=0-N%K`, K = ceiling / `gpus`). Lenient by design; unset = the author's own pace |
| `steward.allowed` | Paths a separate stewardship lane may maintain (the ruler, the harness) — never the solver |
| `merge: manual \| auto` | Whether a gate-and-panel-clean PR waits for a human or merges itself |
| `verification: gate \| self_report` | Per-benchmark verification: kernel measurements by default, or explicitly labelled author claims |
| `channels.siblings`, `messages`, `shared_reports`, `branches` | Contract-wide switches for kernel-mediated sharing; all default to true |

For GPU benchmarks `gpu_hours_per_run` is a real budget. An author's
experiment launches and its gate evals (baseline and candidate when paired)
draw on it, and the author sets how long its final eval may run
(`submit --minutes`). Compute is charged to the author that spends it; it
is never the metric. CPU benchmarks are not metered.

```bash
uv run python -m outerloop.contract_cli .outerloop.yaml   # validate before you push
```

Each benchmark can set `verification: gate | self_report`. The default,
`gate`, retains kernel measurement, suite checks and the configured panel.
Verification belongs to the benchmark because it changes the meaning of that
metric's claims, just as its baseline protocol does.

With `self_report`, submit requires `--claimed-value <number>` and
`--claimed-baseline <number>`. Both must be finite numbers. The comparison
baseline is explicitly an author claim too, rather than an implicit selection
from another run's ledger. The kernel checks scope, seals the candidate, and
applies its usual relative improvement threshold (0.5% by default) and the benchmark's
`min_delta` and `min_delta_rel` to the claimed pair. Both floors must be met;
an unchanged tree or a failing claim receives no credit. A valid passing
claim follows the usual PR publication path. Neither value is re-measured,
no suite evaluations run, and no panel runs. Submit spends one sleep and no
evaluation GPU-hours; separately requested experiments retain their normal
budgets. As with an inline gate, launches staged alongside a submit do not run
and must be staged separately. Trust submissions also work without a compute
backend; launches and checkpoint sleeps still require one.

Reports, PRs, submission ledger rows and board status identify these results
as self-reported (`provenance: self_reported`). Run records carry
nondefault `verification` and `channels` fields, and any published
`claimed_value` and `claimed_baseline`. The submission ledger records the claim
when the PR opens; the main leader still advances only after an observed merge.
A self-reported solver claim cannot replace an existing measured leader series
without a ruler reset. The author gets no permission to edit the contract,
roadmap, `.github/`, steward files or any other path outside solver scope.

A run keeps the verification mode it started with; a contract change applies
to runs that start after it. Channel switches take effect at a running attempt's
next wake.
Self-reported PRs never arm automatic merging, including under `merge: auto`:
they have no clean panel blessing. With `merge: manual`, nothing merges
without the existing human process.

Sharing is controlled at contract level, with all four channels enabled by
default:

```yaml
channels:
  siblings: true
  messages: true
  shared_reports: true
  branches: true
```

`siblings: false` removes the sibling syscall example and direction-selection
nudge from the brief, delivers an empty sibling snapshot at startup and wake,
and limits the queue view to this run's jobs. Tool-update wake text no longer
advertises other agents' jobs. `messages: false` removes messaging from the
brief and wake protocol, and refuses inter-agent delivery in both the CLI and kernel, with a clear
reason. Operator thread posts and self reminders remain available; incoming
messages are refused when the recipient disables messaging too.

`shared_reports: false` removes recent reports, distilled lessons, archive
advertising and the research-log pointer from the brief. No shared report
archive is fetched for the author. `branches: false` removes the sibling-branch
suggestion and excludes other agents' `agents/agent-NN` refs from kernel fetches
at startup, wake, mid-session `sync`, preflight and publication. The author's own
line continues to work. Startup reads policy in a temporary kernel checkout. Defaults reuse that
checkout without an additional fetch; restricted authors receive a fresh
base-only clone before filtered refs are fetched. Steward startup uses the same
policy and filtering.
When either sibling or shared-report visibility is disabled, the kernel reads
any enabled research-log view in a temporary repository, delivering only that
view instead of making the entire research-log branch available locally.

These settings control kernel-mediated sharing, not repository authorization.
Other PR branches and shared base history remain available. These switches
cannot stop an author with its own credentials from fetching remote refs,
reading PRs or using external communication. Already fetched Git objects,
previous session context, merged code and content copied through another enabled
channel cannot be erased by changing a switch. Use fresh runs and independent
private repositories when isolation matters. Disabling shared reports withholds
the whole report archive, including earlier reports from the same slot; the
slot's own line memory remains available.

Judges can already be pinned independently of authors in operator settings.
Set every lens explicitly, for example
`OUTERLOOP_PANEL=verify:claude:fixed-judge,review:claude:fixed-judge`, or pass the
same value to `uv run python -m outerloop.attempt --panel`. Each entry is
`kind:backend:model`; specifying both backend and model prevents inheritance
from either the fleet author or a rebound author. Supported judge backends are
`claude`, `codex` and `hermes`. Endpoint-backed judges use
`kind:backend:model[endpoint=judge-profile]` with their own judge credential.
Omitted models retain today's author-model inheritance. This existing setting
needs no additional contract pin; self-report verification bypasses the panel.
