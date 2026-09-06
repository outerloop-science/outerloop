<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/outerloop-science/outerloop/main/docs/assets/icon-dark.svg">
  <img alt="Outerloop" src="https://raw.githubusercontent.com/outerloop-science/outerloop/main/docs/assets/icon-light.svg" width="132">
</picture>

# Outerloop

[![ci](https://github.com/outerloop-science/outerloop/actions/workflows/ci.yml/badge.svg)](https://github.com/outerloop-science/outerloop/actions/workflows/ci.yml)

**Autoresearch agents that improve your benchmark.**

Outerloop runs AI agents on your own research code. An agent proposes a change,
runs the experiment on your cluster, and opens a pull request only when your
benchmark actually improves. Every attempt is written up, including the ones
that failed.

You run it yourself: your keys, your compute, your repos. Nothing reports back
to us. It is built and used every day by the
[Agentic Learning AI Lab](https://agenticlearning.ai) at NYU, where it
co-develops our research codebases.

## How it works

1. **Propose.** An agent picks a hypothesis and writes the code change.
2. **Experiment.** It runs the training on your cluster and reads the results.
3. **Measure.** Outerloop scores the change against the base tree at the same
   seed. Noise does not count as an improvement.
4. **Review.** Reviewers read the change and the claim. If both hold up, a
   pull request opens.
5. **Record.** Every attempt gets a short report: hypothesis, change, outcome,
   next step. Negative results included.

Agents cannot touch the benchmark, the budgets, or your CI. Your branch
protection and required checks apply to them as to any contributor. By default
a pull request waits for a human; a repo can also let clean ones merge
themselves.

## Get started

Three commands and one file. You need a repo with a benchmark command, an API
key for the model that will write the code, and a Slurm cluster or one machine
with a GPU.

```bash
pip install outerloop-science
outerloop init     # where the loop runs, which repo, which model and its key, your GitHub identity
```

The wizard asks for a GitHub identity for the agents to open pull requests
as. Pick `app` and it walks you through creating a GitHub App, under your
account or under an organization you name, and installing it on the repo:
two browser pages and a code pasted back. It then checks that the App can
write the repo and tells you if it cannot. Pick `pat` if you already have a
token. It writes the config and the key files; nothing to edit by hand. Then
add one file, `.outerloop.yaml`, to the repo you want improved:

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
  allowed: [src/]        # the only paths an agent may change
roadmap: docs/roadmap.md # what the agents read for direction; never written
```

```bash
outerloop start    # on a Slurm login node this submits the loop; without Slurm it runs in the foreground
```

Step by step, other model backends included:
[docs/install.md](https://github.com/outerloop-science/outerloop/blob/main/docs/install.md).
Everything the contract can say:
[docs/contract.md](https://github.com/outerloop-science/outerloop/blob/main/docs/contract.md).

## Only want pull request reviews?

The reviewer works on its own. One workflow file and an API key, about five
minutes, no bot account and no cluster. It comments on pull requests with
concrete findings and never approves, blocks, or fails your build. See
[docs/reviewer.md](https://github.com/outerloop-science/outerloop/blob/main/docs/reviewer.md).

## Where it runs

The first-class home is a Slurm cluster. There is no daemon: the loop is a
chain of short jobs that resubmit themselves, so nothing listens and no inbound
SSH is needed. Experiments and evaluations run inside your container image with
no credentials, and GPU-hours are metered against the contract's budget. A
single machine with a GPU works too, for cheap benchmarks. Details:
[docs/compute.md](https://github.com/outerloop-science/outerloop/blob/main/docs/compute.md).

## Safety by design

- **Opt-in and contract-bound.** A repo takes part by granting the bot access
  and committing a contract. The contract, your roadmap, and `.github/` are
  never writable by an agent.
- **Nothing on trust.** Outerloop measures every claim itself, on committed
  trees, and re-verifies before a pull request exists.
- **Untrusted input.** Pull request text, diffs, issues, web pages, and job
  output are data, never instructions. Agents run without credentials.
- **Budgets in code.** Launches, GPU-hours, and runs per week are enforced by
  the kernel, not left to the agent.
- **No model lock-in.** Claude Code, Codex, and hermes-agent are wired today;
  backends are swappable.

Full design: [docs/design/architecture.md](https://github.com/outerloop-science/outerloop/blob/main/docs/design/architecture.md) ·
Roadmap: [docs/roadmap.md](https://github.com/outerloop-science/outerloop/blob/main/docs/roadmap.md)

## Developing

```bash
uv sync
uv run pre-commit install
uv run pytest
```

| Path | Purpose |
| --- | --- |
| `src/outerloop/` | The kernel: contract, tick (the Slurm chain), attempt/orchestrator (the climb), measure/dispatch (evals as jobs), syscall (the author's tool), panel/verifier/review, github, harness backends |
| `tests/` | Tiers: unit (default), `slow`, `llm`, `slurm` markers |
| `scripts/` | Committed operational scripts (the tick chain, provisioning) |
| `docs/` | Install guide, architecture and design notes, roadmap |

## License

Apache License 2.0 — see [LICENSE](https://github.com/outerloop-science/outerloop/blob/main/LICENSE) and [NOTICE](https://github.com/outerloop-science/outerloop/blob/main/NOTICE).
