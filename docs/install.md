# Installing autoresearch in your own infrastructure

autoresearch is built to be **self-hosted**: you run your own instance, with
your own credentials, your own compute, and your own store. There is no hosted
service to sign up for, and nothing you run reports back to us. Everything
below assumes your org, not ours.

## What you need

| | |
| --- | --- |
| A bot identity | A GitHub machine user in your org (or an App), with a fine-grained PAT: contents + pull-requests + issues, **no workflow permission**, scoped to the repos you opt in |
| An LLM API key | Yours. Spend-capped is strongly recommended |
| Somewhere to run the loop | Any host that can make outbound HTTPS calls — a Slurm cluster, a VM, a laptop for the pilot. The orchestrator itself is CPU-only |
| A place for experiments to run | Your CI, your cluster, or your workstation — see Runners below |

## The advisory reviewer (start here — no compute needed)

The reviewer comments on PRs. It needs no GPU, no orchestrator, and no bot
account: it runs in the target repo's own Actions, authenticating as that
workflow.

1. Add an org or repo secret with your LLM API key.
2. Add `.github/workflows/review.yml` to the repo you want reviewed:

```yaml
on:
  pull_request_target:
    types: [opened, synchronize, reopened]
jobs:
  advisory:
    uses: <your-org>/autoresearch/.github/workflows/advisory-review.yml@main
    secrets:
      reviewer_api_key: ${{ secrets.YOUR_LLM_KEY }}
```

Set `REVIEW_BOT_LOGIN` if your bot is named something other than the default —
the reviewer never comments on PRs authored by that login. Maintainers silence
it per-PR with the `autoresearch:no-review` label. It comments; it never
approves, and it never fails your build.

## The benchmark climber

The climber needs two things from a target repo: **bot access** and a
`.autoresearch.yaml` at the root declaring what "better" means.

```yaml
benchmarks:
  - name: my-benchmark
    command: uv run python -m mypkg.eval --json
    metric: success_rate
    direction: max          # or: min
suite:                      # optional — for one artifact evaluated across a suite
  metric: mean_success_rate
  direction: max
budgets:
  gpu_hours_per_run: 8
  runs_per_week: 10
scope:
  allowed: [src/, tests/]   # the only writable paths
roadmap: docs/roadmap.md
```

Three invariants the loader enforces no matter what your YAML says: the
contract file, the roadmap, and `.github/` are never writable, paths cannot
escape the repo, and autoresearch never targets itself.

Your benchmark command should be **deterministic and re-runnable** — that is
what makes a claimed improvement checkable by your own CI rather than taken on
the agent's word.

## Runners: where experiments execute

Experiments run on **your** infrastructure. The `compute` interface has one
implementation today (Slurm via `sbatch`/`squeue`); the same interface is what
a CI runner, a cloud backend, or a robot rig plugs into. To hook up your own:

- **Slurm**: point the config at your partition and account. The tick loop can
  live in the queue itself as a self-resubmitting job — no daemon, no inbound
  SSH, which matters if your cluster requires 2FA.
- **Anything else**: implement the submit/poll interface and register it. The
  orchestrator only needs "start this job" and "is it done."

The rule we keep on our side and recommend on yours: **the loop never executes
another organization's code on your hardware**, and your code never leaves
your infrastructure to run on ours.

## Safety defaults worth keeping

These are on by default and you should think hard before turning any off:

- The bot never merges and is never a code owner; your branch protection
  applies to it like any contributor.
- Agent sessions run without credentials in their environment; pushes happen
  after the session ends.
- Only maintainer-authored issues and comments become tasks — everything else
  is data, not instructions.
- Budgets (tokens, dollars, GPU-hours, PRs/week) are enforced in code; a run
  that hits a cap dies.
- A pause sentinel stops the loop from anywhere with repo write access.

## Getting help

Open an issue. If you're reporting something the agent did, include the run
report — every run writes one.
