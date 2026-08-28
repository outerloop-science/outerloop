<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/icon-dark.svg">
  <img alt="autoresearch" src="docs/assets/icon-light.svg" width="132">
</picture>

# autoresearch

[![ci](https://github.com/agentic-learning-ai-lab/autoresearch/actions/workflows/ci.yml/badge.svg)](https://github.com/agentic-learning-ai-lab/autoresearch/actions/workflows/ci.yml)

An autonomous research agent you point at your own repos. It reviews pull
requests, implements ideas, runs experiments on your compute, and opens a PR
when a benchmark actually improves — with a readable research report, not just
numbers. How much it may do on its own is a per-repo dial: PRs wait for a human
by default, or merge themselves when the gate and the review panel are clean.

Built and dogfooded by the [Agentic Learning AI Lab](https://agenticlearning.ai)
at NYU, where it co-develops our research codebases. Designed from the start to
be **self-hosted by anyone**: your credentials, your compute, your repos.
Nothing reports back to us.

> Currently private while we harden it in production on our own lab. All
> history is written to be public.

## What it does

- **Advisory PR reviews** — comments on pull requests with concrete findings.
  It never approves, never blocks, and never fails your CI. Five minutes to
  set up.
- **Benchmark climbing** — given a contract (`.autoresearch.yaml`) declaring
  what "better" means and where the agent may write, authors propose changes
  and the orchestrator measures them itself: the base tree and the candidate
  are evaluated on your cluster under one fresh seed, a calibrated
  significance floor decides whether the movement is real, a verify/review
  panel reads the claim, and only then does a PR open. Several authors can
  climb one benchmark abreast (the width dial); each author can run its own
  experiments outside the sandbox and sleep until they finish (the depth
  dial). Claims stay re-verifiable by your own CI because benchmark commands
  are deterministic.
- **Research reports** — every attempt ends with a report (hypothesis,
  change, outcome, takeaway, next step), including the negatives. Reports
  are archived on a `research-log` branch of the target and pointed to from
  a rolling issue; credited improvements update the target's ledger
  (`BENCHMARKS.md`, `results/leader.json`).

## The contract

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
| `steward.allowed` | Paths a separate stewardship lane may maintain (the ruler, the harness) — never the solver |
| `merge: manual \| auto` | Whether a gate-and-panel-clean PR waits for a human or merges itself |

For GPU benchmarks `gpu_hours_per_run` is metered: an author's experiment
launches and its candidate's evals draw on it, and the author declares how
long its final eval may run (`submit --minutes`) — compute is paid for by
whoever chose to spend it, and never gated as the metric. CPU benchmarks
meter nothing.

```bash
uv run python -m autoresearch.contract_cli .autoresearch.yaml   # validate before you push
```

## Getting started

See **[docs/install.md](docs/install.md)**. Level 1 (advisory reviews) needs
only an LLM API key and one workflow file. Level 2 (the climber) adds a bot
account, a contract, and a Slurm cluster with Apptainer.

## Design principles

- **Opt-in and contract-bound.** A repo participates by granting the bot access
  and committing a contract. The contract, your roadmap, and `.github/` are
  never writable by the agent, whatever the contract says.
- **Your gates apply.** The agent is an ordinary contributor: branch
  protection, required checks, and code owners bind it like anyone else. In
  `merge: auto` mode your required CI with strict up-to-date checks is the
  last word, and GitHub itself refuses a stale-base merge.
- **Untrusted by default.** PR text, diffs, issue text, and job output are
  data, never instructions. Authors run without credentials in their
  environment; evals run in a jail that sees only the checked-out tree.
  Budgets — launches, sleeps, GPU-hours, weekly runs — are enforced in code.
- **Nothing is taken on trust.** The orchestrator measures every claim
  itself, on committed trees, and re-verifies credited candidates before a
  PR exists.
- **Nothing leaves your infrastructure.** Experiments run where you say
  (Slurm is the first backend; the compute interface is small and pluggable).
- **No resident process.** The whole system is a self-perpetuating chain of
  short Slurm ticks with all state in records on the shared filesystem —
  every role is its own job, and a crash anywhere heals into an honest
  ending on the next tick.
- **No model lock-in.** Every model call goes through the harness layer, so
  backends are swappable — Claude Code, Codex, and hermes-agent are wired today.

Full design: [docs/design/architecture.md](docs/design/architecture.md) ·
Roadmap: [docs/roadmap.md](docs/roadmap.md)

## Developing

```bash
uv sync
uv run pre-commit install
uv run pytest
```

| Path | Purpose |
| --- | --- |
| `src/autoresearch/` | The kernel: contract, tick (the Slurm chain), attempt/orchestrator (the climb), measure/dispatch (evals as jobs), syscall (the author's tool), panel/verifier/review, github, harness backends |
| `tests/` | Tiers: unit (default), `slow`, `llm`, `slurm` markers |
| `scripts/` | Committed operational scripts (the tick chain, provisioning) |
| `docs/` | Install guide, architecture and design notes, roadmap |

## License

MIT.
