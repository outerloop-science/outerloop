# Security and repo hygiene

This repo operates a bot with write access to lab repos and spends real money on
LLM APIs and GPU hours. Its history may go public with a release.

## Never commit

- The bot's PAT, LLM API keys, or any credential. They live as env vars or 0600
  credential files on the orchestrator host (Torch home dir), with one sanctioned
  exception: the reviewer role's separate, spend-capped API key in GitHub Actions
  secrets (fork PRs run without secrets). Agent sessions never see the PAT or
  billing keys (scrubbed environment); transcripts are secret-scanned before
  storage.
- Agent transcripts or run artifacts — they contain target-repo code
  (confidential until those repos publish). `runs/`, `transcripts/`, `outputs/`
  are gitignored; they stay on lab storage.
- Personal cluster paths or netids; binaries >500 KB.

## Operational rules

- The bot is never a code owner anywhere and never merges code; its PRs pass the
  same gates as everyone's. Sole exception: the prose-only notebook repo, where
  bot PRs auto-merge on a green secret-scan check.
- Budget caps (tokens, dollars, GPU-hours, PRs/week) are enforced in code; a run
  that hits a cap dies.
- Kill switches, fastest first: set the pause sentinel on the state branch (any
  write access, no cluster login — the chain self-terminates next tick); suspend
  the bot account; `scancel` the queued chain and GPU jobs (needs 2FA login).

## If a credential leaks

1. Rotate immediately; the bot PAT and API keys are the crown jewels here.
2. Tell the PI. Check the run ledger for anything the leaked credential touched.

## Reporting a vulnerability

Email the PI: mengye@nyu.edu.
