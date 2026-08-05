# autoresearch

[![ci](https://github.com/agentic-learning-ai-lab/autoresearch/actions/workflows/ci.yml/badge.svg)](https://github.com/agentic-learning-ai-lab/autoresearch/actions/workflows/ci.yml)
Autonomous research agent that co-develops the lab's benchmark-bearing repos:
picks work, implements, runs GPU experiments, opens PRs when metrics improve,
reviews PRs, reports weekly. Humans keep the merge button.
[Agentic Learning AI Lab](https://agenticlearning.ai), New York University.

> **Private — internal infrastructure.** Handles bot credentials and unpublished
> lab code. Confidential until released.

## How a repo opts in

1. Grant the lab bot account write access.
2. Add `.autoresearch.yaml` at the repo root (benchmarks, budgets, scope — see
   [docs/design/architecture.md](docs/design/architecture.md)).

Target repos never import this repo; the agent's PRs go through their normal
review gates.

## Quickstart

```bash
uv sync
uv run pre-commit install
uv run pytest
```

## Layout

| Path | Purpose |
| --- | --- |
| `src/autoresearch/` | The library: contract, harness, orchestrator, compute, github, budget, report (arriving per [roadmap](docs/roadmap.md)) |
| `tests/` | Tiers: unit (default), `slow`, `llm`, `slurm` markers |
| `scripts/` | Committed operational scripts (protection, tick job) |
| `docs/` | Architecture and roadmap |

## Status

Scaffold phase. Design: [docs/design/architecture.md](docs/design/architecture.md).
