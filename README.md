# autoresearch

[![ci](https://github.com/agentic-learning-ai-lab/autoresearch/actions/workflows/ci.yml/badge.svg)](https://github.com/agentic-learning-ai-lab/autoresearch/actions/workflows/ci.yml)

An autonomous research agent you point at your own repos. It reviews pull
requests, implements ideas, runs experiments on your compute, and opens a PR
when a benchmark actually improves — with a readable research report, not just
numbers. **Humans keep the merge button.**

Built and dogfooded by the [Agentic Learning AI Lab](https://agenticlearning.ai)
at NYU, where it co-develops our research codebases. Designed from the start to
be **self-hosted by anyone**: your credentials, your compute, your repos.
Nothing reports back to us.

> Currently private while we harden it in production on our own lab. All
> history is written to be public.

## What it does

- **Advisory PR reviews** — comments on pull requests with concrete findings.
  It never approves, never blocks, and never fails your CI. This works today
  and takes about five minutes to set up.
- **Benchmark climbing** *(arriving — see the [roadmap](docs/roadmap.md))* —
  given a contract (`.autoresearch.yaml`) declaring what "better" means, the
  agent will propose changes, evaluate them on your infrastructure, and open a
  PR only when the metric moves. Claims stay re-verifiable by your own CI
  because benchmark commands are deterministic. The contract format and
  validator exist today.
- **Research reports** *(arriving)* — every run will end with a readable
  report (hypothesis, outcome, takeaways, next steps), feeding task selection
  for future runs.

## Getting started

See **[docs/install.md](docs/install.md)**. Level 1 (advisory reviews) needs
only an LLM API key and one workflow file. Level 2 (the climber) adds a bot
account and a contract.

```bash
# validate your contract before you push it
uv run python -m autoresearch.contract_cli .autoresearch.yaml
```

## Design principles

- **Opt-in and contract-bound.** A repo participates by granting the bot access
  and committing a contract. The contract, your roadmap, and `.github/` are
  never writable by the agent, whatever the contract says.
- **Your gates apply.** The agent is an ordinary contributor: branch
  protection, required checks, and code owners bind it like anyone else.
- **Untrusted by default.** PR text and diffs are data, never instructions.
  Agent sessions run without credentials in their environment. Budgets are
  enforced in code.
- **Nothing leaves your infrastructure.** Experiments run where you say
  (Slurm is the first backend; the compute interface is small and pluggable).
- **No model lock-in.** LLM calls sit behind small interfaces (`Completer` for
  reviews, the harness layer for coding sessions) so backends can diversify;
  Anthropic is the pilot backend.

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
| `src/autoresearch/` | The library: contract, review, github, llm; harness, orchestrator, compute arriving per the roadmap |
| `tests/` | Tiers: unit (default), `slow`, `llm`, `slurm` markers |
| `scripts/` | Committed operational scripts |
| `docs/` | Install guide, architecture, roadmap |

## License

MIT.
