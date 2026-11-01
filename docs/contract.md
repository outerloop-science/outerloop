# Writing a contract

A contract is one file, `.outerloop.yaml`, at the root of the repository the
agents will work on. It defines what "better" means, how much compute an
agent may spend, and which paths it may write. This page walks through one
worked contract and the decisions behind it. Every field, default and
constraint is listed in the [contract reference](reference/contract.md).

## A worked contract

Two benchmarks in one research repo, one CPU and one GPU:

```yaml
benchmarks:
  - name: reach-success
    command: uv run python -m mypkg.eval --json
    metric: success_rate
    direction: max          # higher is better; min means lower is better
    seed_env: MYPKG_SEED     # the eval reads its run seed from this variable
    min_delta: 0.02          # a win must clear this many absolute units
  - name: train-time
    command: uv run python -m mypkg.eval --timing --json
    metric: train_seconds
    direction: min
    seed_env: MYPKG_SEED
    min_delta_rel: 0.05      # or this fraction of the recorded level
    eval_minutes: 45         # evals longer than 5 minutes run as their own jobs
    gpus: 1                  # on one GPU
    regression:              # what a shared-code change may cost here
      free_rel: 0.01
      max_rel: 0.25
      requires_gain_rel: 0.05
budgets:
  gpu_hours_per_run: 24
  runs_per_week: 20
scope:
  allowed: [src/mypkg/solver/]    # the only paths an agent may write
  shared: [src/mypkg/solver/model/]   # shared code: touching it re-measures siblings
roadmap: docs/roadmap.md     # what the agents read for direction; never written
```

Validate the file before you push it:

```bash
uv run python -m outerloop.contract_cli .outerloop.yaml
```

The command prints what the agent would be allowed to do, or exactly what is
wrong. Run it from any environment with `outerloop-science` installed.

## Choosing a benchmark

The `command` must be deterministic and re-runnable, and must print the
metric as JSON on one line. That is what lets the kernel re-measure a change
itself, and lets your own CI re-verify any improvement an agent claims.

One benchmark usually measures one artifact well. If a change should be
judged across the whole suite at once, add an aggregate and every benchmark
is measured per change, with no cherry-picking:

```yaml
suite:
  metric: mean_success_rate
  direction: max
```

## Seeding and the significance floor

Noise must not count as an improvement, and a floor is what stops it. Two
decisions set that up.

- `seed_env` names the environment variable your eval reads its run seed
  from. The kernel draws one fresh seed per measurement pass and pins both
  sides of a comparison to it, so the comparison shares its luck. Your eval
  must read the seed from that variable, print it to stdout, and never write
  it into the tree.
- `min_delta` is an absolute floor in metric units, right for a bounded
  metric such as a success rate. `min_delta_rel` is a fraction of the
  recorded level, right for an unbounded metric such as a wall time. Set
  either or both. When both are set, the more conservative one applies.

Calibrate the floor from seed variance: run the base tree several times at
different seeds and measure the spread. A floor inside the spread lets noise
through. A floor far above it freezes real progress. Credit requires two
bars at once, a delta that meets the floor (equality counts) and a relative
improvement of at least 0.5%, applied absolutely when the baseline is zero.

By default the gate re-measures the base tree beside every candidate, both
under one fresh seed, at two evals per attempt. Set `baseline: cached` to
measure the base tree once per base commit instead, which halves eval spend.
That comparison is unpaired, so the contract must then declare a positive
floor, and you should calibrate it across seeds.

## Budgets

`runs_per_week` caps how many attempts may start per week. For a GPU
benchmark, `gpu_hours_per_run` is a real meter: every experiment an author
launches and both gate evals of a submission draw on it, and an over-budget
request is refused with the numbers. The author states how long its final
eval may run when it submits, so a slower eval is paid for out of the budget
rather than killed. CPU benchmarks are not metered.

A run that opens a pull request gets a one-time top-up on top of these
budgets, 2 launches, 4 sleeps and 0.5 GPU-hours by default.

## Scope

`scope.allowed` lists the only paths an agent may write. Keep it as narrow as
the work requires, and keep the benchmarks' own eval code outside it, since
an eval that sits inside the solver's paths is a ruler the agent can edit.
The validator refuses the repository root, glob patterns
and any path outside the repo. Four paths are never writable whatever the
contract says: the contract itself, your roadmap, `.github/`, and a guidance
directory if you declare one.

`scope.shared` lists code every benchmark exercises, such as a shared model
or training loop. A change that touches a shared path is measured on every
benchmark, and a sibling regression past its floor refuses the credit.

## Regressions on sibling benchmarks

By default a sibling benchmark may not regress beyond the larger of its own
absolute and relative floors. A `regression` block softens that for one
benchmark when shared code changes. In the worked contract above:

- A win on `train-time` still needs its 5% floor.
- As a sibling, a regression up to 1% (`free_rel`) is free.
- A regression between 1% and 25% (`max_rel`) needs at least 5% measured
  gain on the climbed benchmark (`requires_gain_rel`).
- A regression of 25% or more is refused regardless of gain.

Relative values are fractions of the sibling's baseline. Absolute twins
(`free`, `max`, `requires_gain`) exist for metrics where units read better
than percentages. Use one family per block, never both.

## Depth and pacing

An attempt is metered on actions, not on a loop the kernel drives.
`depth_k` caps how many experiment jobs an author may launch (default 10),
and `sleep_k` caps how many times it may sleep waiting for results (default
20). Batching many launches under one sleep costs one sleep.

Pacing between attempts defaults to a 6-hour cooldown. Set
`attempt_cooldown_minutes: 0` to run attempts back-to-back. Width defaults
to one author at a time. Raise `max_active_attempts` to run several abreast,
each with its own agent identity. `max_concurrent_gpus` caps how many GPUs
one author's sweep may hold at once. `runs_per_week` remains the spend guard
in every shape.

## Publication and verification

The remaining decisions are what happens to a clean pull request, who
vouches for the measurement, and what taste the agents carry.

- `merge` controls the end of a clean pull request. The default, `manual`,
  waits for a human. `merge: auto` lets the kernel merge a clean pull request
  at the blessed head, even while its author is still running experiments;
  only an unread human comment holds it. Merging ends the run and cancels its
  jobs. Set branch protection with your repo's own required
  checks under strict up-to-date enforcement and no required review.
- `verification` controls who measures. The default, `gate`, is the kernel's
  own measurement, suite checks and panel. `self_report` accepts an
  explicitly labelled author claim instead: submit requires two claimed
  numbers, the kernel applies the usual floors to the claimed pair, nothing
  is re-measured and no panel runs. Reports, ledger rows and the board label
  these results as self-reported, and they never arm automatic merging.

A third optional switch, `guidance`, selects the research-taste pack the
author brief and the panel carry: the kernel's default pack, none, or a
repo-relative directory with `author.md` and `panel.md`. Guidance is
advisory text and agents can never write it.

## Sharing and isolation

All kernel-mediated sharing is on by default: sibling snapshots, inter-agent
messages, shared reports and sibling branches. Turn any of them off under
`channels` when a repo should work more privately.

```yaml
channels:
  siblings: true
  messages: true
  shared_reports: true
  branches: true
```

These switches control what the kernel delivers, not repository access. An
author with its own credentials can still fetch remote refs and read pull
requests, and objects fetched before a switch was turned off cannot be
erased. Use fresh runs and independent private repositories when isolation
matters.

## Changing a running contract

A run keeps the verification mode it started with, and other changes apply to
runs that start after the contract changes. Two effects reach existing runs:
publishing refuses a run whose base contract changed its measurement rules,
and a run with an open pull request re-pins to the current base at its next
wake. Drain the affected runs before changing measurement rules. Channel
switches take effect at
a running attempt's next wake. Agents can never edit the contract, the
roadmap or anything under `.github/`.
