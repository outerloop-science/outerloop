# Changelog

All notable changes are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [SemVer](https://semver.org).

## [Unreleased]

### Changed

- Review and verification comments read like a colleague, not a legal
  notice (maintainer feedback): one calm italic header line per role, and
  findings rendered as prose paragraphs with the reference at the end
  instead of bullet fragments. The mechanical guard against forged
  endorsements (approval-language redaction) is unchanged.

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

- The benchmark steward (`steward` module + tick lane): maintainer-filed
  work orders (issues labeled `autoresearch:steward`, standing-gated) run
  a session whose territory is the solver's INVERSE — the contract's new
  `steward.allowed` paths (env generators, eval harness, tests), with the
  solver's scope and the always-forbidden set blocked in code. Its ruler
  is validation, not improvement: the orchestrator runs the target's test
  suite contained, re-measures the named benchmark with the CURRENT
  solver, and writes the reset record rows from its own measurement.
  Steward PRs are bot-authored, so the verifier reads them adversarially;
  identity is `steward-01` with its own key
  (`AUTORESEARCH_STEWARD_KEY_FILE`; the lane stays off until
  provisioned).

- Display precision follows convention, not float repr (maintainer
  decision): human surfaces — PR tables, BENCHMARKS.md, the rewritten
  candidate row — render at the benchmark's `display_digits` (contract
  knob, 2–12 significant digits, default 6); full precision lives only in
  `results/leader.json`, and every comparison runs on full floats, so
  display can never hide or fake an improvement.
- Follow-up pushes rewrite the PR body's measured candidate row in place
  (the one mechanical, orchestrator-owned number that must never go
  stale), scoped to the preamble so report text with a lookalike row is
  untouched; the narrative is never rewritten — the Edit block points at
  the replies.

- The roles now read each other (both gaps found live on the first
  verifier exchange): the verifier's context includes the PR discussion —
  bounded, with its own prior rounds marked as its own findings to
  re-check — and a follow-up that pushes code appends an **Edit** addendum
  to the PR body, so the report frozen at publish is never mistaken for
  the current state.

- The verifier (`verifier`, `verifier_cli`, reusable workflow
  `verify.yml`): adversarial integrity reads of BOT-authored PRs — the
  advisory reviewer's mirror. Gaming lens (harness exploitation,
  ruler-fishing, leakage, overfitting, unsupported claims, measurement
  gaps) with the contract and the frozen ruler's source fetched from the
  BASE branch into context; findings tagged by category; a header stating
  a clean read does not certify the result; its own spend-capped key
  (`ANTHROPIC_VERIFIER_KEY`). The advisory workflow's interim
  bot-PR-on-label override is now opt-in (`review_bot_prs_on_label`,
  default false) for verifier-less self-hosters — with a verifier
  deployed, the label routes bot PRs to it.

- `docs/design/roles.md`: the one-page cast — every role (live, next, and
  designed) with its authority and its "may never" column, mermaid flow
  diagrams for the improvement pipeline and review routing, the
  collusion-separation table, and where state lives. architecture.md's
  stale three-line Roles section now points there.

- Design docs for the meta track: `docs/design/meta.md` (pilot-vs-meta
  taxonomy, two-tier GitHub simulator, battery + scorecard with paired
  repetitions, sim-only self-improvement, benchmark-steward role) and
  `docs/design/public-surface.md` (threat model for public target repos;
  the `pull_request_target` audit checklist that gates any public flip).

- Per-repo budget shaping: a contract's `budgets:` may now set
  `session_max_turns`, `session_minutes`, `climb_job_minutes`, and
  `followup_job_minutes`. Contracts are untrusted, so every value is
  clamped into orchestrator-side [floor, ceiling] bounds (`limits` module)
  with CEILING == DEFAULT — contracts are merged by target-repo
  maintainers, so the knobs shape strictly downward; raising a budget is
  orchestrator-side config — and the session is shrunk to fit inside its
  job. The tick fetches the contract
  once per cycle and threads the limits through all three launch lanes.
- Self-deadline: climbs arm a SIGALRM at walltime-minus-margin (default
  120s, floor 60s, `--deadline-margin-s`) raising into the ordinary
  containment — the only pre-kill warning on clusters that deliver no
  signals to job processes (measured on Torch 2026-08-08). The tick passes
  each climb its own walltime via `--job-minutes`.

- The advisory reviewer posts one comment PER ROUND (numbered, stamped
  with the reviewed head SHA) instead of editing a single thread: under
  review-until-quiet, humans must see each round — comment edits fire no
  notifications and bury prior rounds in edit history. The upsert guarded
  against synchronize-era spam; runs are now open- or label-triggered
  only.

- Killed climbs now reach a recorded ending: the record stores the climb's
  own Slurm job id and a new sweep pass ends `implementing` records whose
  job is terminal — Slurm truth plus grace, outage never reads as dead.
  Previously only crashes (Python exceptions) were contained; kills
  stranded the record forever. A SIGTERM handler also raises into the
  ordinary containment, but measurement (Torch, 2026-08-08) showed Slurm
  delivers no signal to job processes there — on such clusters the sweep
  and the self-deadline (below) are the real teardown paths; the handler
  covers direct kills and other sites.

- Adding the `autoresearch:review` label runs a review on bot-authored
  PRs: the labeling EVENT (re-request by removing and re-adding, same as
  on human PRs) is an explicit ask. Superseded within this release by the
  verifier (below): the label now routes bot PRs to the verifier, and the
  advisory-reviewer override became opt-in (`review_bot_prs_on_label`,
  default false) for verifier-less self-hosters. The opt-out label still
  wins over contradictory signals.

- Review-until-quiet merge gate documented (CONTRIBUTING, CLAUDE.md):
  development PRs iterate advisory-review rounds with a judged termination
  criterion — code PRs stop at no new medium+/behavior-affecting findings,
  docs/process PRs get one round with nits batched, and a 4-round hard cap
  escalates to the PI. Contributor docs also updated for the single
  consolidated `ci` check (stale five-check wording).

- Publish-time freshness: when the base branch moves during a climb, the
  run branch merges the fresh base (merge commit, never rebase), the claim
  is re-measured on the merged tree, and the leader check runs against the
  fresh ledger — before anything is pushed; both sides are measured in
  throwaway worktrees of commits (equivalent pristine environments; the
  shas pin content). A conflicting merge or an absorbed improvement ends
  the run honestly instead of opening an unmergeable or unverified PR, and
  a final base re-check before push narrows (not eliminates) the window.

- Improvement PRs arm GitHub auto-merge at publish (best-effort): the bot
  still never merges — arming hands the merge to the human approval that
  branch protection requires, so approving is the last human action needed.

- Disk preflight (`disk` module): quota exhaustion is invisible on some
  clusters until a write fails, so the tick and the climb now write-probe
  their storage (plus a statvfs early-warning threshold, `--min-free-gb`)
  before launching new work. A failed preflight turns the launch lanes off
  for the tick, surfaces in the heartbeat and the tick summary, and never
  kills the chain; the climb refuses to start and tells the issue. The
  chain script defaults `UV_CACHE_DIR` and `APPTAINER_CACHEDIR` under the
  state root so host-side caches never land in a constrained $HOME.

- Crash containment in live climbs: the run record is saved before any
  network or clone work, the claim block runs inside the contained region,
  and every ending step (record, report, issue post) degrades independently
  — a full disk cannot block the GitHub failure report, and a network
  failure cannot block the record. The tick summary line now reports the
  intake and self-initiated lanes.

- Self-initiated lane: when the requested lane claims nothing and no run is
  active, the tick launches a climb on the least-recently-attempted contract
  benchmark, within the weekly budget and a per-benchmark cooldown. The
  planning agent later replaces this picker with motivated, vetoable plans.

- The requested lane: maintainer issues on the target repo become runs. The
  tick claims at most one qualifying issue per cycle (standing-gated,
  single-benchmark-named, claim-marker deduped); the issue text enters the
  brief data-fenced, the PR says "Addresses #N", and the run report lands
  back on the issue thread.

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
