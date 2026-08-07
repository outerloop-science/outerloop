# Changelog

All notable changes are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [SemVer](https://semver.org).

## [Unreleased]

### Changed

- CI consolidated to one job (`ci`) — GitHub bills per job rounded up to a
  minute, so five short jobs cost 5x. The advisory review now runs on PR open
  and on the `autoresearch:review` label, not on every push.

- **Breaking**: the review CLI reads `ANTHROPIC_REVIEWER_KEY`, no longer
  `ANTHROPIC_API_KEY` (role-named credentials; the harness gets its own key).
  Reusable-workflow callers are unaffected. Direct invokers must export the
  new name — with the old one the reviewer skips cleanly but silently.
- The reviewer now sees today's date, repo/PR metadata, and bounded
  head-revision contents of changed files, and its prompt demands
  evidence-based findings (fixes a live false positive that flagged a correct
  date as a typo).

### Fixed

- Post-merge advisory findings on the climb glue: content-fingerprint drift
  check (rewrites during eval, not just new files), leader best never
  regresses, climb exceptions record aborted (never a stale implementing),
  zero-change improvements rejected, redacted publish logs; pushed
  branches are never deleted on publish failure — kept and recorded in the
  run note for a future sweeper (deletion could close a real PR).

- Hardening from the first adversarial review pass: local git runs without
  credentials and with hooks disabled (planted hooks can no longer read the
  PAT), scope paths are normalized component-wise, self-target matching accepts
  any repo spelling, contract YAML rejects aliases/duplicate keys/oversized
  input, commits are vetoed against the contract's forbidden paths, and API
  errors/response shapes are typed instead of crashing.

### Added

- The tick now services in-review runs automatically: PR merge/close ends the
  run; new qualifying review comments submit a follow-up job that wakes the
  authoring session (lease-guarded; `followup_job_id` prevents duplicate
  queueing). The chain passes the PAT path to the tick for GitHub reads;
  agent sessions remain scrubbed by the harness allowlist.

- `autoresearch.followup`: the in-review path — PR merge/close ends the run;
  qualifying maintainer comments (author-association gate) resume the
  authoring session in its retained workspace, and the reply lands on the
  thread as the bot, with any code change scope-checked, drift-checked,
  re-measured, and pushed to the PR branch.

- `autoresearch.climb`: one live climb end to end — bot-auth clone, contract
  from the target tree, contained session + eval, full-scope commit veto,
  push, PR with orchestrator-measured numbers, durable run record and report.
  `create_pull` on the GitHub client.

- Phase 5 core: `orchestrator.climb_once` (single-benchmark climb with
  orchestrator-measured baseline/candidate — the agent's claim is never
  trusted) and Apptainer session containment in the harness
  (`container_image`: --containall with workspace + per-run-HOME binds only;
  the API key travels via APPTAINERENV_, never argv).

- Phase 4 loop plumbing: `compute` (Slurm submit/status/cancel behind an
  injectable runner; afterany wake jobs; query-failure ≠ job-gone),
  `runstate` (atomic run records, six endings, expiring wake leases with
  handoff), `tick` (pause sentinel, heartbeat, the five-layer fail-safe
  sweep), and the self-resubmitting chain script.

- `autoresearch.brief` (typed, bounded, replayable session briefs — the
  context-engineering artifact) and `autoresearch.harness` (backend seam;
  Claude Code adapter with scrubbed session env, timeouts, and key-redacted
  transcripts; fake adapter for tests) — phase 3.

- Deployment hardening for the reviewer: operational LLM errors map to
  `CompleterError` (an expected failure — never reds a target repo's CI),
  missing API key skips cleanly, and the reusable workflow accepts a read-only
  deploy key so private self-hosted copies work. README rewritten for external
  adopters.
- `autoresearch.contract_cli`: validate a `.autoresearch.yaml` locally and print
  what the agent would be allowed to do.
- `autoresearch.review` + reusable advisory-review workflow: constrained PR
  reviewer (never on bot PRs, opt-out label, one thread per PR, advisory header),
  Anthropic completer behind an injectable interface, `docs/install.md` for
  self-hosting (phase 2).
- `autoresearch.github`: REST client + git workspace behind a token-provider
  seam (GitHub App-ready), env-injected git auth, dry-run mode (phase 1).
- `autoresearch.contract`: schema + loader for `.autoresearch.yaml` with suite
  aggregates and hard-coded safety invariants (phase 1).
- Repository scaffold: package skeleton, CI gates (lint/types/test/lock/gitleaks),
  pre-commit hooks, governance docs, architecture design, roadmap.
