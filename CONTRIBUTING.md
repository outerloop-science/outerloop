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
- **Review until quiet** (PI rule 2026-08-07): development PRs (human- or
  assistant-authored) iterate adversarial review rounds. Mechanics: the
  advisory workflow runs on open; to re-run after a fix commit, remove
  then re-add the `autoresearch:review` label once the push has settled
  (it fires on the labeled event; removal alone runs nothing). The
  authorizing round must be the MOST RECENT one and run against the HEAD
  commit — a quiet verdict on an older diff authorizes nothing. Findings
  rejected on rationale get a reply on the PR thread, never silence.
  Termination is judged, not literal — an eager reviewer can always find
  one more wording nit, and prose never reaches literal quiescence:
  - code PRs: iterate until a round yields no new medium-or-higher or
    behavior-affecting findings; wording nits in an otherwise quiet round
    are fixed or answered without another round;
  - docs/process PRs: ONE round, its nits batched into a single fix or
    reply;
  - hard cap of 4 rounds: still finding mediums by then means the change
    itself is the problem — stop and escalate to the PI, don't cycle.
  The habit exists because rounds have repeatedly found real defects in
  earlier rounds' own fixes. Bot improvement PRs sit outside this gate:
  the advisory reviewer skips them by design, and their gate is the
  TARGET repo's required human review — the publish step arms auto-merge
  only when the target's branch protection requires a review (the arming
  guard refuses otherwise, in code). The "no required approvals"
  solo-phase note above is about THIS repo's protection; bot PRs land on
  target repos, never here.

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
