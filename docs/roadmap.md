# Roadmap

Phases ship in order; each is a reviewed PR. The reviewer role delivers value
before any target repo has a benchmark harness; the climber needs one
(jepa-agent roadmap phases 1–3, egolearn evals).

## Phase 0 — Scaffold ✅

Package skeleton, CI gates, governance docs, architecture (ops+safety reviewed).

## Phase 1 — Contract + GitHub plumbing

- [x] `contract`: pydantic schema + loader, including the hard-coded invariants
      (no self-targeting; contract/roadmap/`.github/` always forbidden)
- [x] `github`: bot auth, clone/branch/push, PR + issue operations
- [x] Dry-run mode: everything logs, nothing posts

## Phase 2 — Advisory reviewer

- [x] PR-triggered advisory review via GitHub Actions on opted-in repos, with the
      constraints from the architecture: advisory header, never on bot-authored
      PRs, one thread per PR, opt-out label, org-member/label gate on inputs
- [x] Separate spend-capped API key in Actions secrets; no secrets on fork PRs
- [ ] Pilot on jepa-agent; measure signal-to-noise with the humans reviewing

## Phase 3 — Harness

- [ ] **Gating spike first**: headless subscription-CLI auth on a Torch compute
      node — token lifetime, refresh behavior, concurrent-session safety. Pick
      the ONE backend that passes; API backend as automatic fallback
- [ ] `brief`: typed `SessionBrief` + pure builder (task, contract, ruler,
      capped lessons + recent reports, budget state), stored with every run —
      context engineering as a versioned, testable artifact (see architecture)
- [ ] `harness`: backend-agnostic seam (`SessionBrief` in, `SessionResult`
      out); Claude Code adapter first, dry-run/fake adapter for tests
- [ ] Session sandboxing: scrubbed env (no PAT/billing keys), hard timeouts,
      transcript secret-scan before storage
- [ ] `budget`: per-run and weekly caps; session/token proxy metering for the
      subscription backend

## Phase 4 — Torch tick + compute

- [ ] The chain: two queued successors, `--dependency=singleton`, absolute
      `--begin` cadence grid, sbatch retry/backoff
- [ ] Deploy shim: submit-successors → pull main → `uv sync --locked` → exec;
      pull uses the bot PAT (bot has a Read collaborator role here; add this
      repo to the token's selection)
- [ ] Pause sentinel + lease (compare-and-swap on the state branch); heartbeat at
      tick start; stale-lease reaping
- [ ] `compute`: sbatch submit / squeue poll, jobs tagged + `--nice`, GPU-hour caps
- [ ] Watchdog Action: stale-heartbeat alert issue + email escalation
- [ ] Ops runbook: manual restart, `scancel` procedures, PAT rotation calendar
- [ ] Transcript storage on project space with stated retention

## Phase 5 — Benchmark-climb pilot

Target: [autoresearch-pilot](https://github.com/agentic-learning-ai-lab/autoresearch-pilot)
— a non-research-bearing proving ground (tsp / denoise / speedup; deterministic,
CPU-only, contract and baselines already committed). Decouples this roadmap from
the research repos' porting timelines; mistakes are free; adversarial testing
(injection via issues) is staged here, never on research repos.

- [ ] Bot opt-in on the pilot: Write grant + token scope
- [ ] Task pinning: target SHA + contract hash at task start, re-validated each
      poll; baseline re-run at merge-base
- [ ] Full loop on the pilot benchmarks; bounded iterations; PR with results
      table, one human code-owner approval per merge
- [ ] Every run ends with a research report — hypothesis, outcome, takeaways,
      next steps — posted to the PR or the ledger (negative results included)
- [ ] Staged injection tests against the pilot before any research target

## Phase 6 — Reporting & research memory

- [ ] `autoresearch-notebook` repo (permanently private): runs/, lessons/,
      plans/, digests/ per the architecture; bot PRs auto-merge on a green
      secret-scan check, no approval required
- [ ] Task selection reads lessons/ + recent runs/ (the loop's research memory)
- [ ] Periodic distillation pass: raw reports → bounded lessons/<target>.md
- [ ] Weekly digest aggregates per-run reports; cost ledger; leaderboard history

## Phase 7 — Research targets & scale-out

- [ ] jepa-agent as the first research target: `.autoresearch.yaml` + bot Write
      grant + token scope, once its benchmark harness lands and the pilot's
      PR-quality bar is met
- [ ] egolearn as second research target; second harness backend if volume warrants
- [ ] API model tiering informed by pilot data; cloud burst if queues block

## Beyond 1.0 — external-facing (design: design/external.md)

- [ ] GitHub App identity replacing the machine user
- [ ] Storage interface: notebook-repo backend → walled multi-tenant store
- [ ] Experiment-backend interface: consumer-side runners, verifiable rewards

## Manual prerequisites (Mengye)

- [ ] Create the bot machine user; invite to org (free seat on Team plan); mint
      the fine-grained PAT per the architecture's spec
- [ ] Subscription seat or lab-managed account for the pilot harness
- [ ] API billing with hard spend caps + the separate reviewer key
- [ ] Choose the sponsoring Torch account for bot-submitted jobs
- [ ] Decide transcript retention period and project-space location
