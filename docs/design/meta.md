# Benchmarking autoresearch itself

**Status: design sketch, v1 (2026-08-08 maintainer direction; sequencing
agreed loose — the security audit in public-surface.md and the verifier come
first, sim self-improvement explicitly last). Nothing here gates current
phases; decided pieces promote into architecture.md as they firm up.**

## Why

Changes to the loop's core — task selection, briefs, planners, base models,
multi-agent designs — currently get correctness review but no *capability*
measurement. "Did this make the agent a better researcher?" is answered by
anecdote. The remedy is the same one the loop imposes on its own targets: a
frozen ruler and orchestrator-measured claims, applied one level up.

## Pilot vs. meta-benchmark (two instruments, not one)

|                | pilot (integration canary)              | meta-benchmark (capability instrument) |
|----------------|------------------------------------------|----------------------------------------|
| question       | does the *system* run against a real, foreign repo? | did the *decision core* get better? |
| lives          | separate repo, forever                   | inside this ecosystem                   |
| exercises      | real GitHub, real Slurm, permissions, contract discovery, deploy cadence | orchestration + research quality, plumbing faked or pinned |
| regression =   | plumbing broke                           | the agent got worse                     |

The pilot must stay a separate repo precisely because its job is simulating
"someone attached autoresearch to their project", friction included. The
meta-benchmark lives in-tree so the battery versions in lockstep with the
loop's interfaces and a loop PR can land with its A/B evidence atomically.

## The simulator

Every seam the simulator needs already exists because the unit tests demanded
it: `GitHubClient` takes an injectable transport, `Workspace` clones from
local bare repos, `SlurmCompute` takes an injectable runner, the tick takes
injected compute/clock/dispatcher, and the harness is a protocol. The
simulator is those doubles assembled into a runnable environment plus a
driver that advances ticks programmatically.

**Tier (a) — everything faked.** Fake GitHub (in-memory issues/PRs/comments
/labels with real-API semantics), local bare repos, fake Slurm with scripted
job lifecycles, scripted harness. Runs in CI. Measures orchestration
correctness under scenarios unit tests can't compose: multi-tick lifecycles,
duplicate-launch races, kill/crash recovery, lane interleaving, budget
accounting over weeks of simulated time.

**Tier (b) — simulated GitHub, real compute, real sessions.** The target is a
repo snapshot; sessions and evals run for real (contained, on the cluster);
GitHub is still the fake, so nothing posts anywhere. Measures research
capability on non-trivial tasks — e.g. "find a better optimizer" on a
speedrun-style training snapshot, where the metric is wall-time to a loss
target. Costs real money and GPU hours; runs on demand, not in CI.

**Fidelity rule.** The fake GitHub must reproduce the real API's sharp edges
— three independent comment id namespaces, `reviewDecision`, `mergeable_state`
lag, label-event vs. label-state, author associations — because the live
system's correctness code is built against exactly those. A sim cleaner than
reality benchmarks a system that does not exist. Every time live GitHub
surprises us, the surprise becomes a fake-GitHub test case.

## The battery

A battery instance = frozen (repo snapshot, contract, baseline score,
scripted-or-live task source). A battery run = the loop driven end-to-end on
an instance set, producing a scorecard:

- **time-to-first-improvement** (ticks in tier (a), wall-clock in tier (b))
- **improvement per dollar / per GPU-hour**
- **negative-result honesty**: seeded gaming opportunities (eval-writable
  rulers, cacheable eval calls, saturating tests) must be *refused or
  caught* — the integrity vetoes firing is a pass, silence on a seeded
  exploit is a fail
- **lifecycle hygiene**: no stranded records, no duplicate launches, every
  run ends in a report — asserted mechanically over the final state root

**Statistics.** LLM stochasticity dominates single runs. A/B comparisons use
paired repetitions (n ≥ 5 per instance per arm) on identical instances;
report medians with spread, not single numbers. A loop change "helped" only
by the same ε-and-direction discipline the contract imposes on solvers.

## Self-improvement in the sim (last, unhurried)

The live self-target ban is absolute and stays (contract loader invariant).
Inside the simulator, the target is a *fork/snapshot* of autoresearch, the
eval command is the battery score, and the frozen ruler is the battery
harness itself — which the improver cannot touch, per the same
scope/drift/orchestrator-measured machinery every other target gets.
Proposals that survive the sim return to the real repo only as ordinary
development PRs: review-until-quiet, human merge, no exceptions. The sim is
where the loop may experiment on itself; the live deployment never is.

## Benchmark steward (role sketch; builds after the verifier)

Benchmarks decay: agents saturate them (reach 1.0 → v2; probe's frozen test
band; sokoban at 1.0), and timing metrics need noise policies. Today a human
authors the fix. The steward is the autonomous version, and its design is
collusion-avoidance first:

- **separate identity, credentials, budget** from every solver agent;
- objective = **discriminative power** (headroom restored, solvability
  proofs, reproducible baselines, noise floors) — never solver score;
- moves goalposts only through the existing plan-issue machinery: vetoable
  issue, aligned to the target's agent-unwritable `vision:`, enacted
  exclusively by a human-merged contract/env PR;
- the **verifier** reviews steward PRs adversarially (is this change
  restoring discrimination, or quietly making a colluding solver look
  better?), and solver runs mid-flight are protected by the existing
  contract-hash abort.

First work orders exist from the agents' own reports (see roadmap): probe v2
(trivially saturable), a timing-noise floor for speedup. These validate the
role on real demand before any automation.

**Growth path (maintainer direction 2026-08-09)**: the steward's mission is
tiered — maintain (headroom, exploits, noise floors) → extend (harder
metrics, new metrics on existing tasks, evaluation protocols adopted from
the literature, cited) → invent (new benchmarks designed within the
target's `vision:`, like a research scientist would). Invention ends at a
proposal by construction: the steward implements env/eval/tests inside its
territory, but the contract's benchmark list is agent-unwritable, so its
PR carries a ready-to-paste contract entry and the human enacts it. Each
tier uses the same governed channel — standing-gated work orders (later:
vetoable self-proposed plans), verifier's adversarial read, human merge.

## Progress webpage

Everything is already in git: `results/leader.json` history, run reports,
plan issues, BENCHMARKS.md. A static generator renders charts (leader
trajectories per benchmark), run timelines, and report indexes — GitHub
Pages on public repos, a private artifact before that. No server, no new
state, no new trust surface (the generator reads git, writes HTML).

## Build order

1. Tier-(a) simulator + a 3-instance battery (pilot snapshot, a seeded-gaming
   instance, a lifecycle-chaos instance) — loop PRs gain measured evidence.
2. Scorecard + paired-repetition driver; wire into an on-demand workflow.
3. Progress webpage over the pilot's git state.
4. Steward on the pilot (after the verifier ships).
5. Tier-(b) capability instances; then, last, sim self-improvement.
