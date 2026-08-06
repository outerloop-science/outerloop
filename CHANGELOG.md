# Changelog

All notable changes are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [SemVer](https://semver.org).

## [Unreleased]

### Fixed

- Hardening from the first adversarial review pass: local git runs without
  credentials and with hooks disabled (planted hooks can no longer read the
  PAT), scope paths are normalized component-wise, self-target matching accepts
  any repo spelling, contract YAML rejects aliases/duplicate keys/oversized
  input, commits are vetoed against the contract's forbidden paths, and API
  errors/response shapes are typed instead of crashing.

### Added

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
