# Changelog

All notable changes are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [SemVer](https://semver.org).

## [Unreleased]

### Added

- `autoresearch.github`: REST client + git workspace behind a token-provider
  seam (GitHub App-ready), env-injected git auth, dry-run mode (phase 1).
- `autoresearch.contract`: schema + loader for `.autoresearch.yaml` with suite
  aggregates and hard-coded safety invariants (phase 1).
- Repository scaffold: package skeleton, CI gates (lint/types/test/lock/gitleaks),
  pre-commit hooks, governance docs, architecture design, roadmap.
