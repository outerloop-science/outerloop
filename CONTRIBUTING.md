# Contributing

## Setup

```bash
uv sync
uv run pre-commit install
```

## Branching and merging

- `main` is protected: all changes land via PR with green CI.
- Branch off `main`: `feat/<topic>`, `fix/<topic>`. Agent-authored branches use
  `feat/auto/<slug>`.
- **Merge commits only** — squash and rebase merges are disabled at the repo level.
- Keep branches fresh with `git merge main`, never rebase; never force-push a
  shared branch.

## Pull requests

- The `ci` check must pass (lint, types, tests, lock, and secret scan run
  inside it as one job).
- Solo phase: no required approvals yet; PI review by convention. Raise to 1
  code-owner approval when a second owner joins
  (`scripts/setup_branch_protection.sh <repo> 1`).
- Update `CHANGELOG.md` under `[Unreleased]` for user-visible changes.
- Code PRs iterate adversarial review ROUNDS until a round finds nothing
  new ("review until quiet", PI rule 2026-08-07). The advisory workflow runs
  on open; after any fix commit that introduces a new mechanism (not just
  line-comment edits), toggle the `autoresearch:review` label off/on to
  re-run it on the current diff. Merge only after a quiet round; findings
  rejected on rationale get a reply on the PR thread, never silence. The
  record so far: every round on a substantive PR has found at least one
  real defect, including in the previous round's fixes.

## Style

- `ruff` for lint + formatting (line 100); type hints encouraged; `mypy` must pass.
- Imports are absolute (`from autoresearch...`); code works from an installed wheel.

## Tests

| Tier | Marker | Runs |
| --- | --- | --- |
| unit | *(none)* | CI, every PR |
| slow | `slow` | nightly / manual |
| llm | `llm` | manual only — paid APIs |
| slurm | `slurm` | manual only — needs a cluster |

## Dependencies

Deps go in with the code that needs them (see the comment in `pyproject.toml`);
regenerate `uv.lock` (`uv lock`) — the `lock` check enforces it.

## Confidentiality

See SECURITY.md. Transcripts and run artifacts contain target-repo code and are
never committed.
