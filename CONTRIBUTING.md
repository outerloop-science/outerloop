# Contributing

Outerloop is developed in the open by the [Agentic Learning AI Lab](https://agenticlearning.ai)
at NYU, and its own agents are regular contributors to it. Contributions from
people are welcome too: bug reports, fixes, documentation, new compute
backends, and benchmark contracts for your own repos.

## Before you start

- Look through the open issues. For anything larger than a small fix, open an
  issue first so we can agree on the shape before you write code.
- Security problems go to the contact in [SECURITY.md](SECURITY.md), not to a
  public issue.

## Setup

```bash
uv sync
uv run pre-commit install
uv run pytest
```

## Making a change

1. Branch from `main`: `fix/<topic>` or `feat/<topic>`.
2. Keep the change focused. Add or update tests, and a line under
   `[Unreleased]` in `CHANGELOG.md` for anything a user would notice.
3. Run the same gate CI runs before you push:

   ```bash
   uv run ruff check . && uv run ruff format --check .
   uv run mypy
   uv run pytest && uv run pytest -m serial
   ```

4. Open a pull request against `main`. Say what changed, why, and how you
   verified it.

## What happens to your pull request

- CI must pass.
- One of our agents reviews it within a few minutes and posts its findings as
  a review. It never approves or blocks; a maintainer decides. Fix what is
  right and reply to what is not; findings answered with a reason are fine.
  After you push fixes, a maintainer re-applies the `outerloop:review` label
  to run another round.
- A maintainer merges with a merge commit. We do not squash or rebase, so the
  history of how a change came to be stays intact. Keep your branch current
  with `git merge main`, and never force-push a branch someone else has seen.

## Conventions

- Python 3.12, absolute imports (`from outerloop ...`), and code that works
  from the installed wheel; no paths relative to a checkout.
- `ruff` for lint and formatting (line length 100); `mypy` clean.
- Dependencies go in the `pyproject.toml` group that owns them, then
  `uv lock`; CI checks the lock.
- Docs are plain prose. Short sentences, no metaphors, no internal jargon.

## Tests

| Tier | Marker | Runs |
| --- | --- | --- |
| unit | *(none)* | CI, every PR, on all cores |
| serial | `serial` | CI, second step: `pytest -m serial` (inspects the process table) |
| slow | `slow` | nightly or manual |
| llm | `llm` | manual only, paid APIs |
| slurm | `slurm` | manual only, needs a cluster |

While editing, `uv run pytest --testmon` runs only the tests affected by your
change; `uv run pytest -n0` runs everything serially.

## Pull requests from the agents

Pull requests authored by `outerloop-science[bot]` are the system improving
the repositories it works on. They appear on those target repositories, are
gated by each target's own required review, and are skipped by the advisory
reviewer by design. This document is about contributions to the kernel itself.

## License

By contributing you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE), the same as the project.
