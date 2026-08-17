# Roadmap

Kept current by whoever lands the change (maintainer rule 2026-08-08: the
roadmap is updated in the same PR that moves it, not batched later).
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
- [ ] Experiment/eval dispatch design: docs/design/dispatcher.md —
      eval-as-a-job first, triggered by the 2026-08-16 steward eval-timeout
      class. (Distinct from the `WakeDispatcher` wake-session seam above.)

- [x] The chain: `scripts/tick_chain.sbatch` — successor top-up to depth 2,
      `--dependency=singleton`, absolute `--begin` cadence grid, sbatch
      retry/backoff; successors submitted FIRST so nothing below can break
      the chain
- [x] Deploy shim (same script): pull main with the bot PAT → `uv sync
      --locked` → exec tick; every step best-effort (bad merges crash ticks,
      never the chain). Bot PAT on Torch + repo in the token's selection
      still owed by the maintainer before live deploy
- [x] Pause sentinel, heartbeat at tick start (now with disk preflight
      payload), stale-lease reaping (TTL + tombstone CAS)
- [x] `compute`: sbatch submit / sacct status / cancel behind an injectable
      runner (GPU-hour caps land with the experiment path)
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
- [x] Full loop live on the pilot (2026-08-07): requested lane (issue #7 →
      PR #8), in-review follow-up (comment → resumed-author reply), and
      self-initiated selection (least-recently-attempted, budget + cooldown
      bounded, one active run per target) all exercised; auto-merge armed by
      approval for bot PRs
- [x] Every run ends with a research report — hypothesis, outcome, takeaways,
      next steps — in the PR body and the run dir; requested-lane runs also
      report to their issue on every terminal path. Hardening series merged
      2026-08-07/08: crash containment (#43), disk preflight + scratch-first
      caches (#44), auto-merge armed only when a human review is required
      (#45), publish-time freshness (moved base merged + re-measured, #46),
      killed-climb endings (SIGTERM containment + implementing sweep, #49),
      per-repo budget shaping + self-deadline (#51). Measured on Torch
      2026-08-08: Slurm delivers NO signal to job processes before SIGKILL —
      the sweep and the self-deadline are the real teardown paths
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

- [x] Verification agent (scaling.md Part 2d): verifier on the review
      chassis — bot-PRs only, gaming prompt, contract fenced in and ruler read
      from the base checkout, own capped key, silence-is-not-endorsement
      header; label routes by author. Deployed on yolo-jepa (first target:
      `verify.yml` caller + repo-level `ANTHROPIC_VERIFIER_KEY`); each new
      target onboards by copying the caller and setting the key
- [x] Benchmark steward CODE (maintainer direction 2026-08-08/09: the
      steward agent does env work, not hand edits): separate identity
      (`steward-01`, own key), territory = the contract's `steward.allowed`
      with the solver's scope forbidden in code, work orders =
      standing-gated `autoresearch:steward` issues, validation ruler run by
      the orchestrator (suite + re-measure + orchestrator-written record
      resets), verifier reads its PRs adversarially. DEPLOY pending:
      steward key + pilot contract `steward:` section + the four work-order
      issues (denoise v2 [PR #18 draft as spec], reach v3, probe v2,
      speedup noise floor)
- [x] Suite no-regression gate (yolo-jepa lessons: both gamed climbs bought
      their number by exploiting one benchmark's structure): the contract
      declares `scope.shared` paths; a solver diff touching them has every
      sibling benchmark re-measured on both sides (paired seed) and loses
      credit if one regresses beyond its own floor — an honest negative,
      not an abort. Env-specific diffs stay cheap (one benchmark measured).
      Re-gated on the merged tree when the base moved. Follow-up pushes are
      NOT yet suite-gated — named gap, wire with the follow-up re-measure.
      Credit rule for the future generalist inversion (maintainer 2026-08-15):
      shared-path CONTACT prices the suite pass but never grants suite-wide
      credit. A per-benchmark fork is legitimate STAGED research (mechanism
      proven on the toy first, larger benchmarks later, possibly coupled with
      other innovations) when claimed as such — the offense is a claim/evidence
      mismatch, never the fork. Multi-row credit must be evidence-based
      (siblings improved in the same pass, or mechanism verified shared);
      claim-vs-mechanism consistency is the verifier lens item; per-benchmark
      eval configs stay ruler territory so shared code cannot observe the
      benchmark identity
- [x] Pre-PR verification loop CODE (design: docs/design/orchestrator-verify.md):
      the panel (verify + review lenses, multi-opinion at the seam) runs
      inside the climb job after measurement — a PR becomes the OUTPUT of
      verification; blocking findings wake the author (round-capped, fully
      re-measured and re-gated); capped-out blocking opens a DRAFT PR and
      never arms auto-merge; the transcript rides in the PR body. DEPLOYED
      in code: the tick passes --panel verify,review to every climb job by
      default (off-switch AUTORESEARCH_PANEL=""). Cluster prerequisite: the
      verifier key file at ~/.config/autoresearch/verifier_key on the tick
      account — a missing key fails climbs LOUDLY by design. GitHub-side
      verify.yml thins per target after the pilot runs clean
- [ ] jepa-agent as the first research target: `.autoresearch.yaml` + bot Write
      grant + token scope, once its benchmark harness lands and the pilot's
      PR-quality bar is met
- [ ] egolearn as second research target; second harness backend if volume warrants
- [ ] API model tiering informed by pilot data; cloud burst if queues block

## Meta: benchmarking autoresearch itself (maintainer direction 2026-08-08)

Sequencing agreed loose — no deadline pressure; item 5 explicitly last.
Distinction to preserve: the PILOT is the integration canary (system
feasibility under realistic attachment — foreign repo, real GitHub/Slurm
friction; stays a separate repo forever), while the META-BENCHMARK is the
capability instrument (does the decision core improve?) and lives inside
this ecosystem so the battery versions in lockstep with the loop.

1. [ ] Public-surface security audit before ANY repo with our workflows goes
       public — `pull_request_target` + secrets + fork PRs is the classic
       exfiltration shape; also: external code never executes on lab
       hardware, and RELEASING gains a hostile-interaction section
       (design doc: design/public-surface.md)
2. [ ] GitHub simulator + meta-benchmark battery (design/meta.md): tier
       (a) all-fake (injectable transport, local bare repos,
       programmatic tick driver — CI-runnable orchestration correctness);
       tier (b) sim GitHub + REAL Slurm/sessions for capability tasks (e.g.
       find a better optimizer on a speedrun snapshot). Battery = frozen
       (repo snapshot, contract, baseline) instances; loop changes run A/B
       against the incumbent with paired repetitions (LLM variance dominates
       single runs); metrics incl. time-to-first-improvement, improvement
       per dollar/GPU-hour, integrity-veto honesty (seeded gaming must be
       caught)

Roles/flow reference for this whole section: design/roles.md.

3. [ ] Progress webpage: static generator over git state (leader.json
       history, reports, plan issues) — charts, run timelines; no server
4. [ ] Benchmark steward live on the pilot (phase-7 entry above)
5. [ ] Self-improvement in the sim (LAST, explicitly unhurried): the live
       self-target ban stays absolute; inside the simulator the target is a
       snapshot of autoresearch and the eval is the battery score;
       improvements return only as human-reviewed development PRs

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
