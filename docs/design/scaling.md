# Research planning and multi-agent scaling

**Status: design sketch, v2 — iterating with the maintainers (started 2026-08-06;
round-1 decisions folded in 2026-08-06). Nothing here gates the phase-4
build; decided pieces get promoted into architecture.md as they firm up.**

## Part 1 — Planned search, not drive-by tasks

Today's task selection proposes one hypothesis at a time from benchmark gaps
and roadmaps. The upgrade: a **planning agent** that owns a target's search
*program*.

**The leader.** Per benchmark, a small ledger records the current best
configuration: config, metric, SHA of the run that set it, updated only when
a PR merges or a consolidation run (below) completes. The leader is the
reference point for all search — the planner proposes movements *from* it,
one knob at a time.

**The plan.** The planner reads the leader, recent reports, lessons, and the
budget, and emits a search plan: which hyperparameter or architectural choice
to probe next, and why. Every plan entry is motivated twice, and says so
explicitly:

- *scientific hypothesis* — "EMA decay is the stability bottleneck at long
  horizons; if true, τ-sweep at small scale shows divergence order flipping"
- *experiment economics* — expected information per GPU-hour; what this
  eliminates from the search space if the answer is negative.

**Where plans live: GitHub Issues** (maintainer decision, for visibility), one
issue per search line on the target repo, plus a machine-readable copy in the
notebook's `plans/<target>/`. Bot-authored plan issues do NOT pass the
requested-lane gate (that would let the pipeline feed itself privileged
tasks). Lifecycle of a plan issue (DECIDED 2026-08-06):

- posts, then starts under the self-initiated budget **one day later** by
  default — the veto window;
- an `autoresearch:stop` label vetoes it, at posting time or any time after
  (a stopped line's in-flight run Aborts);
- an explicit `autoresearch:approved` label from a maintainer (provenance
  verified, as in the intake gate) starts it immediately — approval promotes
  the plan to the requested lane, reusing the existing label and gate rather
  than inventing a parallel one;
- **stop always wins, and is terminal**: with both labels present the line
  does not run; approval applied after a stop never resurrects it (the veto
  is a safety switch, not a debate) — reviving a vetoed idea takes a new plan
  issue, so the reversal is as visible as the veto.

## Part 2 — The experiment ladder and coordinated runs

**Resource tiers in the contract.** Something like:

```yaml
tiers:
  smoke: {cpu_only: true, minutes: 10}        # correctness, not science
  small: {gpu_hours: 2}                        # proof-of-idea
  large: {gpu_hours: 24, per: consolidation}   # earned, never default
```

**Proof-of-idea before scale.** When the planner (or a run in flight)
observes that a hypothesis genuinely needs large resources, it must first
design the *minimal small-tier experiment set* that would justify the spend —
proxy horizon, data subset, fewer seeds — with explicit go/no-go criteria
written before the small runs launch (pre-registration, agent edition). A
large run request cites its small-tier evidence in the plan issue.

**Periodic consolidation runs.** Triggered by accumulation — **K
benchmark-consequential merges since the last consolidation** (DECIDED
2026-08-06; not calendar time) — launch one coordinated large run that
combines the period's merged improvements, with leave-one-out ablations to
attribute the gain. Output: an updated leader, and a consolidation report to
the notebook + a digest issue. This is where the "many small wins" become a
defensible headline number — and where interactions between improvements
(which single-hypothesis runs cannot see) get caught. Budgeted separately
(`per: consolidation`) so weekly exploration never starves it — but the
consolidation line carries its own ceiling (a minimum interval and a per-run
GPU-hour cap), so a burst of merges cannot trigger unbounded large runs.

## Part 2b — Maintenance work and the vision (added in round 1)

Not every valuable merge moves a benchmark. **Maintenance work** — refactors,
test coverage, tooling, documentation, dependency health — is a first-class
task category, not hill-climbing exhaust:

- Merged PRs are classified at close: *benchmark-consequential* (moved a
  contract metric; counts toward the consolidation trigger K) or
  *maintenance* (does not count toward K, but is reported and appears in the
  digest — its value is legibility and long-term velocity, not the metric).
- Maintenance tasks are motivated by the **vision** (below) and repo-health
  signals rather than metric gaps, carry the same one-hypothesis-per-PR
  granularity ("this module's duplication blocks the next two search lines"),
  and draw from a bounded share of the weekly run budget so they can neither
  starve nor be starved by benchmark work.

**Vision lives in the contract.** The contract gains a `vision:` field — a
short high-level statement of where the codebase is going, which the planner
reads when proposing both search lines and maintenance work. Because the
contract is agent-unwritable (loader invariant), the vision changes only by
a human PR to the target repo: governed, visible, versioned — the same forum
mechanism as everything else. Agents may *propose* vision changes in reports
or issues; they cannot enact them.

## Part 2c — Results: three layers, one blur resolved (round 3)

A codebase, a leaderboard, and an experiment server are different things;
results tracking keeps them separate instead of blurring them:

| Layer | Lives | Written by | Contains |
| --- | --- | --- | --- |
| **Telemetry** | the experiment tracker your org already uses (W&B, in our case) | experiment jobs, via the target repo's own config-driven logging | curves, configs, artifacts — the deep-dive surface; agent runs tagged with agent + run id, filterable next to human runs |
| **Ledger** | a `results` branch in the target repo — append-only `ledger.jsonl` + a full rendered history table | the orchestrator after every measured run (improvements, negative results, aborted); humans via one command that evals + appends | EVERY measured number with provenance: benchmark, value, sha, who (agent id or human), date, W&B link. Intermediate baselines and lab members' numbers live here — attributable, in-repo, zero PRs |
| **Headline** | `BENCHMARKS.md` + `results/leader.json` on main | the orchestrator only, riding inside improvement PRs | current best per benchmark + milestones (rows where the leader changed) |

Rules that fall out:

- **No result ever creates its own main-branch PR.** Headline updates ride
  inside improvement PRs that merge on their own merits; everything else goes
  to the ledger branch (a push, not a PR — data, not code, same reasoning as
  the notebook's auto-merge) or to W&B. Public research repos' main history
  stays exactly as clean as their science.
- **Humans share baselines through the ledger**, one command: run the eval,
  append the JSON row with your name, push to the results branch. No W&B
  account needed to be counted; no PR needed to be seen.
- **History is the ledger, not the leader.** `leader.json` is a snapshot;
  "what was the best before X" and every intermediate baseline is a ledger
  query. The rendered history table on the results branch shows leader
  transitions per benchmark.
- **W&B credentials never enter agent sessions** (credential-free sessions
  are an invariant); experiment jobs may log with the target repo's own
  config. Whether agent experiments get a scoped W&B service account or log
  anonymously-tagged is a phase-6 decision.
- The pilot keeps its simpler shape (headline-on-main only): every merge
  there is important by construction, and it has no W&B.

Implementation lands with phase 6 (reports); nothing here blocks the live
climb.

## Part 2d — Verification (decided 2026-08-07)

Three verification regimes, by what the target's metric permits:

1. **Directly verifiable** (the pilot): the frozen eval validates solution
   structure (invalid output raises, never scores), the metric is
   deterministic, the orchestrator re-measures, fingerprints pin the tree.
   No judgment needed.
2. **Statistically verifiable** (ML targets): protocol replaces proof —
   frozen held-out evals on frozen config grids, seeds, the noise floor,
   full-suite reporting, leader-never-regresses, and consolidation runs with
   leave-one-out ablation, where lucky seeds and flukes die.
3. **Judgment-verifiable**: a **verification agent** skims the code and the
   claim for what protocol cannot catch — ruler-gaming inside allowed scope,
   test-set leakage, seed-fishing, overfitting to frozen instances, claims
   the diff does not support.

The verification agent is the advisory reviewer's complement, and the two
never overlap:

| | advisory reviewer | verification agent |
| --- | --- | --- |
| runs on | human-authored PRs | **bot-authored PRs only** |
| hunts | general defects | metric-gaming specifically |
| inputs | diff + file context | diff + contract + eval code + claimed numbers + run report |
| output | findings, never approvals | findings, never approvals — and its header states that **absence of findings is not an endorsement** |
| credential | its own capped key | its own capped key, distinct from both the reviewer's and the harness's |

This stays compatible with the reviewer-influence rule (nothing in the
pipeline may nudge humans toward merging its own work) because the verifier
only ever raises suspicions: silence is explicitly meaningless, and its text
passes the same approval-language sanitization as the reviewer's. It ships
on the same reusable-workflow chassis (a verifier mode: inverted bot gate,
gaming-focused prompt) and is **required before the first
statistically-verifiable target onboards** — judgment is the layer category-2
targets cannot do without.

## Part 3 — Multi-agent: identity, isolation, seats

**Why multiple agents at all** (practical, not aesthetic): subscription seats
carry monthly limits, so one agent serving every opted-in repo both hits the
ceiling and tangles unrelated targets' cadences. Repo-level assignment is the
natural sharding.

**Identity.** Agents are persistent, named, and few: `agent-01`, `agent-02`.
Each owns, on the shared filesystem: a state directory (its runs, its
leases), a branch namespace (`feat/auto/<agent-id>/<slug>`), and a credential
file. Every lease, run-state file, PR, and report already carries a run id;
they now carry the agent id too — provenance for "who did this" across the
whole forum.

**Isolation and communication.** Isolation by construction: the assignment
is a **partition, and validated as one** — every tick fails loudly if any
target appears under two agents, so a copy/paste in the YAML cannot silently
create the races this design claims cannot exist. Shared write surfaces that
remain (the leader ledger, `plans/<target>/`) get the same lease/CAS
treatment as run state, with holder ids recorded. Communication is deliberately indirect — through the notebook (lessons
are per-target, shared by whoever serves that target next) and GitHub. No
agent-to-agent channels until a concrete need appears; the forum-simplicity
argument applies to agents talking to agents too.

**Scheduling.** One tick chain per agent (own sentinel, own heartbeat, own
chain lease), watched by the same GH-Actions watchdog. A dead agent's chain
never takes the others down; pausing one agent is dropping one sentinel.

**Assignment config** (state branch, human-edited):

```yaml
agents:
  agent-01:
    targets: [autoresearch-pilot]
    credential: api-key         # the default for now (round-1 decision 3);
    max_runs_per_week: 10       #   `seat` is the future state, post-spike
  agent-02:
    targets: [jepa-agent, egolearn]   # multiple repos per agent is fine
    credential: api-key
    max_runs_per_week: 6
```

**Seats on Torch (the practical auth question — deferred until multi-agent
testing is ready; round-1 decision 3).** Claude Code supports
headless auth two ways: an API key (today's path, works), or a long-lived
OAuth token minted by `claude setup-token` from a subscription seat —
designed for CI, no browser on the cluster needed. Proposed flow: a maintainer runs
`setup-token` locally once per seat, the token lands in
`~/.config/autoresearch/agents/<agent-id>/credential` (0600) on Torch, and
the harness's existing env-injection seam passes it as
`CLAUDE_CODE_OAUTH_TOKEN` instead of `ANTHROPIC_API_KEY` — a per-agent
config switch, no code restructuring. Seat limits then meter per-agent by
construction, and the budget module tracks session-hours per agent id so a
throttled seat backs off instead of erroring.

Two honest caveats. *Isolation*: all agents run as one Unix UID on Torch, so
0600 separates agents from other users, not from each other — a session can
read a sibling agent's credential file by absolute path. Per-agent isolation
is therefore advisory until OS-level sandboxing lands (same residual risk,
same mitigation as the threat model: spend caps per credential, and nothing
more powerful than a metered LLM credential on the session account).
*Permissibility*: whether subscription seats may drive unattended autonomous
agents — several seats from one shared account — is a provider-ToS question,
not a technical one. Confirm against current terms before the seat path
becomes default; needs a live spike regardless (token lifetime, refresh,
concurrent seats).

## Round-1 decisions (maintainers, 2026-08-06)

1. **Veto**: `autoresearch:stop` label; default start one day after the plan
   issue posts; a maintainer's `autoresearch:approved` label starts it
   immediately (promotes to the requested lane).
2. **Consolidation**: accumulation-triggered on K benchmark-consequential
   merges. Maintenance merges are valuable but don't count toward K — see
   Part 2b. **Round 2 (2026-08-06): K defaults to 5–10 and the
   consequential threshold to a 10% relative metric delta (ε) — both
   contract-configurable, never hard-coded.**
3. **Credentials**: API keys for now (seats cost more up front); start a seat
   when multi-agent testing is actually ready. The `setup-token` spike waits
   until then.
4. **Models**: the planner runs on **claude-fable-5 at extra-high effort** —
   it is rare, cheap in absolute terms, and leverage-heavy (a bad plan wastes
   whole GPU-budgets; a good one is compounding). Coding sessions and the
   advisory reviewer stay on claude-opus-5. Maintenance-task planning is part
   of the planner's job and gets the same tier.

## Open questions for the next turn

1. K for the consolidation trigger (and does a failed consolidation reset the
   counter, or carry it?).
2. The maintenance budget share: a fixed fraction of weekly runs (e.g. 1 in
   4), or planner-discretion within a cap?
3. Who classifies a merge as consequential vs maintenance — the metric delta
   mechanically (moved ≥ ε → consequential), or the planner with the metric
   as evidence?
4. `vision:` wording for the pilot repo's contract — a two-sentence draft to
   iterate on: "A proving ground where optimization skill is demonstrated on
   frozen, deterministic benchmarks. Solvers should stay small, readable, and
   self-contained — clever beats large."
