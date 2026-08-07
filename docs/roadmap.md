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

- [x] **Gating spike** (2026-08-05, Torch): Claude Code 2.1.223 native binary
      (no node needed) checksum-installed to `~/.local/bin`; headless `-p
      --output-format json` on a cpu_short compute node returns cost, usage,
      session id, and stop reason — everything `SessionResult` needs. Two
      concurrent sessions (shared `$HOME`, separate workspaces, `--allowedTools
      Write --permission-mode acceptEdits`) both correct, no state corruption.
      Auth: **API key** (static; no lifetime/refresh concerns) — subscription
      OAuth (`claude setup-token`) untested, needs an interactive browser
      login; decision: pilot on API-key auth, revisit a seat when volume
      justifies it. Backend picked: **Claude Code**
- [x] `brief`: typed `SessionBrief` + pure builder (task, contract, ruler,
      capped lessons + recent reports, budget state), stored with every run —
      context engineering as a versioned, testable artifact (see architecture)
- [x] `harness`: backend-agnostic seam (brief text in, `SessionResult` out);
      Claude Code adapter (scrubbed session env, timeout, key-redacted
      transcript to disk) + fake adapter for tests
- [x] Runs span sessions: per-run HOME + native resume (`resume_session_id`)
      + bounded wake prompt (`render_wake`) so a run hibernates through
      multi-day experiments with its agentic context intact (orchestrator-side
      wake scheduling lands in phase 4 with `compute`)
- [ ] Session sandboxing: scrubbed env (no PAT/billing keys), hard timeouts,
      transcript secret-scan before storage
- [ ] `budget`: per-run and weekly caps; session/token proxy metering for the
      subscription backend

Torch pilot findings (2026-08-06, live on compute nodes): the full production
session path works — redirected per-run HOME with env-key auth, brief on
stdin, tool use, per-session cost/id in JSON out. Resume restores context
across sessions AND across nodes (shared-FS state); a 3-tick self-resubmitting
sbatch chain ran unattended. Two lessons encoded above: sessions never touch
GPU partitions (utilization floor), and wake prompts must explicitly lift any
standing instruction they supersede — a resumed agent honors stale constraints
(observed: it refused a wake task that contradicted its brief).

## Phase 4 — Torch tick + compute

- [x] Wake delivery per the architecture's fail-safe layers: afterany
      dependency job (`compute.submit_after`; mechanism verified live
      2026-08-06, incl. on failed experiments) + expiring run lease with
      handoff + tick sweep + deadline floor (pending-cancel / gone-on-good-
      query / defer-on-query-failure) + `stuck` state. Session dispatch
      behind the `WakeDispatcher` seam (real dispatcher lands with phase 5)

- [x] The chain: `scripts/tick_chain.sbatch` — successor top-up to depth 2,
      `--dependency=singleton`, absolute `--begin` cadence grid, sbatch
      retry/backoff; successors submitted FIRST so nothing below can break
      the chain
- [x] Deploy shim (same script): pull main with the bot PAT → `uv sync
      --locked` → exec tick; every step best-effort (bad merges crash ticks,
      never the chain). Bot PAT on Torch + repo in the token's selection
      still owed by the maintainer before live deploy
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
- [ ] Contract `environment.container` (per-target Apptainer image; see
      architecture "Environments and containers") wired into experiment launch
- [ ] Task pinning: target SHA + contract hash at task start, re-validated each
      poll; baseline re-run at merge-base
- [x] `orchestrator.climb_once` (2026-08-06): one implement→evaluate→verify
      cycle on ONE configured benchmark (maintainer decision: start with one, tsp, not
      everything); baseline re-measured from the pre-session tree, candidate
      re-measured by the orchestrator (never the agent's claim), direction-
      aware threshold, PR body with results table + agent report. Sessions
      run contained (Apptainer --containall, workspace + run-home binds only,
      key via APPTAINERENV_ env). Git glue (clone/push/PR wiring) + live
      pilot climb next
- [ ] Full loop live on the pilot: bounded iterations; PR with results
      table, one human code-owner approval per merge
- [ ] Every run ends with a research report — hypothesis, outcome, takeaways,
      next steps — posted to the PR or the ledger (negative results included)
- [x] `in-review` follow-up (followup.respond_once + CLI): merge/close →
      endings; org-member comments wake the authoring session (resume, data-
      fenced, scope/drift/re-measure on changes) → reply as bot. Tick wiring,
      issue intake, and workspace GC still pending
- [ ] Staged injection tests against the pilot before any research target

Candidate second target (2026-08-06): a private toy-scale research repo —
real research code, CPU-runnable, seeded, single-command evals. Two things
to settle before opting it in: it is paper-bearing (needs research-repo
confidentiality care, and no injection testing), and its contract needs one
deterministic headline metric over a frozen config grid rather than an
open-ended sweep space.

## Phase 6 — Reporting & research memory

- [ ] `autoresearch-notebook` repo (permanently private): runs/, lessons/,
      plans/, digests/ per the architecture; bot PRs auto-merge on a green
      secret-scan check, no approval required
- [ ] Task selection reads lessons/ + recent runs/ (the loop's research memory)
- [ ] Periodic distillation pass: raw reports → bounded lessons/<target>.md
- [ ] Weekly digest aggregates per-run reports; cost ledger; leaderboard history

## Phase 7 — Research targets & scale-out

- [ ] Verification agent (scaling.md Part 2d): verifier mode on the review
      chassis — bot-PRs only, metric-gaming prompt, own capped key, and the
      silence-is-not-endorsement header. REQUIRED before any statistically-verifiable
      target onboards
- [ ] jepa-agent as the first research target: `.autoresearch.yaml` + bot Write
      grant + token scope, once its benchmark harness lands and the pilot's
      PR-quality bar is met
- [ ] egolearn as second research target; second harness backend if volume warrants
- [ ] API model tiering informed by pilot data; cloud burst if queues block

## Beyond 1.0 — external-facing (design: design/external.md)

- [ ] GitHub App identity replacing the machine user
- [ ] Storage interface: notebook-repo backend → walled multi-tenant store
- [ ] Experiment-backend interface: consumer-side runners, verifiable rewards

## Manual prerequisites (maintainer)

- [ ] Create the bot machine user; invite to org (free seat on Team plan); mint
      the fine-grained PAT per the architecture's spec
- [ ] Subscription seat or lab-managed account for the pilot harness
- [ ] API billing with hard spend caps + the separate reviewer key + a third capped key for the verification agent (before any category-2 target)
- [ ] Choose the sponsoring Torch account for bot-submitted jobs
- [ ] Decide transcript retention period and project-space location


## Actions economy (decided 2026-08-07)

- [x] Single `ci` job (job-minute rounding was 5x the real usage); advisory
      review on open + `autoresearch:review` label, never per push
- [ ] Same consolidation on the target repos' workflows
- [ ] Mid-term: self-hosted runner on the lab workstation (outbound-only
      polling; frees private-repo minutes entirely). When repos go public,
      hosted minutes become free anyway
