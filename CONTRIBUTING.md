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
- Substantive PRs — code, and process rules like this one; trivial doc
  fixes stay lightweight — iterate adversarial review ROUNDS until a round
  finds nothing new ("review until quiet", PI rule 2026-08-07). The
  advisory workflow runs on open; after any fix commit, REMOVE then RE-ADD
  the `autoresearch:review` label (the workflow fires on the labeled
  event; removal alone runs nothing) once the push has settled. The
  authorizing quiet round must be the most recent round AND run against
  the head commit — a quiet verdict on an older diff authorizes nothing.
  Findings rejected on rationale get a reply on the PR thread, never
  silence. The habit exists because rounds have repeatedly found real
  defects in earlier rounds' own fixes. Scope: this gate governs
  DEVELOPMENT PRs (human- or assistant-authored). Bot improvement PRs sit
  outside it — the advisory reviewer skips them by design, and their gate
  is the TARGET repo's required human review: the publish step arms
  auto-merge only when the target's branch protection requires a review
  (the arming guard refuses otherwise, in code), so that review executes
  the merge. The "no required approvals" solo-phase note above is about
  THIS repo's protection; bot PRs land on target repos, never here.

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
regenerate `uv.lock` (`uv lock`) — the lock step of `ci` enforces it.

## Confidentiality

See SECURITY.md. Transcripts and run artifacts contain target-repo code and are
never committed.
