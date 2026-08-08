# CLAUDE.md

Autonomous research agent that co-develops the lab's benchmark-bearing repos.
Scaffold phase — `docs/roadmap.md` says what exists vs. planned;
`docs/design/architecture.md` has the design.

## Commands

```bash
uv sync
uv run pytest                                    # slow/llm/slurm excluded by default
uv run ruff check --fix . && uv run ruff format .
uv run mypy
uv run pre-commit run --all-files
```

## Hard rules

- **The bot never merges and is never a code owner** — do not weaken this in any
  code or config change.
- **autoresearch is never a target of itself**; the contract file, roadmap, and
  `.github/` are forbidden write paths everywhere, regardless of contract YAML.
- Budget caps are load-bearing safety features, not tunables to raise casually.
- Never commit credentials, transcripts, or run artifacts (SECURITY.md).
- Merge commits only; never rebase, squash, or force-push.
- **Review until quiet**: substantive PRs — code, and process rules;
  trivial doc fixes stay lightweight — iterate advisory-review rounds
  (after each fix commit, remove then re-add the `autoresearch:review`
  label once the push settles) until a round finds nothing new; merge only
  on an explicit `ci` conclusion of success AND a quiet round run against
  the head commit. Read every review before merging — green is not read.
- Imports are absolute (`from autoresearch...`); deps go in with their code +
  `uv lock`; CHANGELOG under `[Unreleased]`.
