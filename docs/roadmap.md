# Roadmap

Phases ship in order; each is a reviewed PR. The reviewer role delivers value
before any target repo has a benchmark harness; the climber needs one
(jepa-agent roadmap phases 1–3, egolearn evals).

## Phase 0 — Scaffold ✅

Package skeleton, CI gates, governance docs, architecture (ops+safety reviewed).

## Phase 1 — Contract + GitHub plumbing

- [ ] `contract`: pydantic schema + loader, including the hard-coded invariants
      (no self-targeting; contract/roadmap/`.github/` always forbidden)
- [ ] `github`: bot auth, clone/branch/push, PR + issue operations
- [ ] Dry-run mode: everything logs, nothing posts

## Phase 2 — Advisory reviewer

- [ ] PR-triggered advisory review via GitHub Actions on opted-in repos, with the
      constraints from the architecture: advisory header, never on bot-authored
      PRs, one thread per PR, opt-out label, org-member/label gate on inputs
- [ ] Separate spend-capped API key in Actions secrets; no secrets on fork PRs
- [ ] Pilot on jepa-agent; measure signal-to-noise with the humans reviewing

## Phase 3 — Harness

- [ ] **Gating spike first**: headless subscription-CLI auth on a Torch compute
      node — token lifetime, refresh behavior, concurrent-session safety. Pick
      the ONE backend that passes; API backend as automatic fallback
- [ ] Session sandboxing: scrubbed env (no PAT/billing keys), hard timeouts,
      transcript secret-scan before storage
- [ ] `budget`: per-run and weekly caps; session/token proxy metering for the
      subscription backend

## Phase 4 — Torch tick + compute

- [ ] The chain: two queued successors, `--dependency=singleton`, absolute
      `--begin` cadence grid, sbatch retry/backoff
- [ ] Pause sentinel + lease (compare-and-swap on the state branch); heartbeat at
      tick start; stale-lease reaping
- [ ] `compute`: sbatch submit / squeue poll, jobs tagged + `--nice`, GPU-hour caps
- [ ] Watchdog Action: stale-heartbeat alert issue + email escalation
- [ ] Ops runbook: manual restart, `scancel` procedures, PAT rotation calendar
- [ ] Transcript storage on project space with stated retention

## Phase 5 — Benchmark-climb pilot

- [ ] `.autoresearch.yaml` lands in jepa-agent with one benchmark
- [ ] Task pinning: target SHA + contract hash at task start, re-validated each
      poll; baseline re-run at merge-base
- [ ] Full loop on that benchmark; bounded iterations; PR with results table
- [ ] Every run ends with a research report — hypothesis, outcome, takeaways,
      next steps — posted to the PR or the ledger (negative results included)

## Phase 6 — Reporting & research memory

- [ ] `autoresearch-notebook` repo (permanently private): runs/, lessons/,
      plans/, digests/ per the architecture; bot PRs auto-merge on a green
      secret-scan check, no approval required
- [ ] Task selection reads lessons/ + recent runs/ (the loop's research memory)
- [ ] Periodic distillation pass: raw reports → bounded lessons/<target>.md
- [ ] Weekly digest aggregates per-run reports; cost ledger; leaderboard history

## Phase 7 — Scale-out

- [ ] egolearn as second target; second harness backend if pilot volume warrants
- [ ] API model tiering informed by pilot data; cloud burst if queues block

## Manual prerequisites (Mengye)

- [ ] Create the bot machine user; invite to org (free seat on Team plan); mint
      the fine-grained PAT per the architecture's spec
- [ ] Subscription seat or lab-managed account for the pilot harness
- [ ] API billing with hard spend caps + the separate reviewer key
- [ ] Choose the sponsoring Torch account for bot-submitted jobs
- [ ] Decide transcript retention period and project-space location
