# Research planning and multi-agent scaling

**Status: design sketch, v1 — iterating with Mengye (started 2026-08-06).
Nothing here gates the phase-4 build; decided pieces get promoted into
architecture.md as they firm up.**

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

**Where plans live: GitHub Issues** (Mengye's call, for visibility), one
issue per search line on the target repo, plus a machine-readable copy in the
notebook's `plans/<target>/`. Bot-authored plan issues do NOT pass the
requested-lane gate (that would let the pipeline feed itself privileged
tasks); they run under the self-initiated budget after a **veto window** —
the plan issue sits at least one full day before its first run, so a human
can re-scope it with a comment or stop it with a label. Visibility without a
human-approval bottleneck; the gate stays where it was.

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

**Periodic consolidation runs.** On a cadence (or when K improvements have
accumulated since the last one), launch one coordinated large run that
combines the period's merged improvements, with leave-one-out ablations to
attribute the gain. Output: an updated leader, and a consolidation report to
the notebook + a digest issue. This is where the "many small wins" become a
defensible headline number — and where interactions between improvements
(which single-hypothesis runs cannot see) get caught. Budgeted separately
(`per: consolidation`), so weekly exploration never starves it.

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

**Isolation and communication.** Isolation by construction: an agent only
touches its assigned targets, so cross-agent races don't exist on the happy
path; the existing lease/CAS design (leases record holder ids) covers the
rest. Communication is deliberately indirect — through the notebook (lessons
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
    credential: seat            # or: api-key
    max_runs_per_week: 10       # frequency cap per Mengye
  agent-02:
    targets: [jepa-agent, egolearn]   # multiple repos per agent is fine
    credential: api-key
    max_runs_per_week: 6
```

**Seats on Torch (the practical auth question).** Claude Code supports
headless auth two ways: an API key (today's path, works), or a long-lived
OAuth token minted by `claude setup-token` from a subscription seat —
designed for CI, no browser on the cluster needed. Proposed flow: Mengye runs
`setup-token` locally once per seat, the token lands in
`~/.config/autoresearch/agents/<agent-id>/credential` (0600) on Torch, and
the harness's existing env-injection seam passes it as
`CLAUDE_CODE_OAUTH_TOKEN` instead of `ANTHROPIC_API_KEY` — a per-agent
config switch, no code restructuring. Seat limits then meter per-agent by
construction, and the budget module tracks session-hours per agent id so a
throttled seat backs off instead of erroring. Needs a live spike before we
rely on it: token lifetime, refresh behavior, and two seats running
concurrently from one Unix account.

## Open questions for the next turn

1. Veto window on bot-authored plan issues: one day? And is a
   `autoresearch:hold` label the right stop switch, or reuse `no-review`
   style naming?
2. Consolidation cadence: calendar (monthly) or accumulation-triggered
   (K merged improvements), or "whichever comes first"?
3. Seat vs API key per agent: start agent-01 (pilot repo) on the API key we
   have, and spike `setup-token` for agent-02 before research repos onboard?
4. Does the planner get its own model/effort tier (it is cheap, rare, and
   leverage-heavy — a Fable candidate?), separate from coding sessions?
