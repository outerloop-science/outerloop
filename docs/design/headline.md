# Outerloop: the headline result and the public flip

Design note, 2026-08-27 (Mengye + agent scoping session). This is the plan for
the result that takes the repo public and seeds the white paper.

## Name and positioning

Working name: **Outerloop**. The click: training is the inner loop, research
is the outer loop — we built the outer loop as an operating system. The name
also frames the field's minimal baseline honestly: `karpathy/autoresearch`
(Mar 2026, a 630-line single-GPU propose/run/measure/commit loop) is the
origin cell of our dial space, not a competitor. "autoresearch" itself has
gone generic (and Karpathy's repo owns it publicly), so the kernel keeps the
word as a category term while the system takes the proper name.

Name assets, traction-gated (stars first, claims second): GitHub repo ships
under the lab org (clear); PyPI `outerloop` is a 4-years-dormant 0.0.1 with a
dead homepage (friendly ask, then PEP 541); `github.com/outerloop` is a
dormant 2013 user (GitHub support, later); HF org taken (publish under the
lab org). Domain: **outerloop.science** (available; .ai/.org/.dev/.io/.run
squatted — none by anything with mindshare, and research systems live at
their repo URL anyway).

## The claim

Autonomous research whose **accuracy and speed scale along explicit dials** —
panel (verifier accuracy per accepted step), width (parallel authors), depth
(iterations per attempt) — on **commodity Slurm**, with every accepted step
**honest by construction** and **zero human interventions inside the
improvement loop** (in `manual` mode the owner's merge is consent, not a
correction — interventions-per-accepted-step is measured per mode), under a
**graded autonomy mode** the target owner sets.

## Tracks

1. **Flagship — nanoGPT speedrun, step-count track.** Steps to 3.28 FineWeb
   val loss, frozen arch/data/batch (modded-nanogpt track 3 rules). Direct
   comparability with Prime Intellect's agent record (2,930 steps vs the
   2,990 human baseline; ~14k H200-hours; ~100 human interventions) without
   requiring identical hardware. New target repo with an `.autoresearch.yaml`
   contract; wall-clock track is a stretch goal pending Torch's GPU shape
   (OPEN: H100s per node, burnable budget).
2. **Comparability — the RSI exam.** A slice of AI4AI-Bench (frozen research
   repos, agent rewrites the training algorithm, rerun from scratch, scored
   by an evaluator hidden from the agent — philosophically identical to our
   sealed-sha + private-seed gate). Gives the paper an external RSI yardstick
   (AIDE² et al.) and maps our machinery onto the published Level-1 RSI
   criteria: fair human baseline, sustained multi-step trend, generalization
   beyond the optimized measurement (our suite gate), fixed budget (our
   contract budgets).
3. **Contribution — the scaling grid.** (width × depth × panel) measured on
   track 1: time-to-target and quality-per-accepted-step per cell, plus the
   honesty ledger (gate rejections, honest negatives, interventions = 0).
   Nobody in this space publishes scaling curves or verification stats.

## The systems argument

Feature the kernel: the literature runs resident loops (Karpathy: one
process, one GPU; Prime Intellect: markdown state + preempt scripts + a
monitoring agent + ~100 interventions; AIDE: in-process tree search). Ours is
**decentralized on Slurm**: no resident daemon — a self-perpetuating chain of
~16 s stateless ticks; all state in typed records on the shared FS; every
role (author session, eval, panel judge, wake) its own Slurm job — wake
dispatch sits behind an operator flag (`AUTORESEARCH_DISPATCH_WAKE`, on in
our deployment since 2026-08-20; retiring the dark-launch flag to
default-on is a listed cleanup); crash or preemption anywhere heals
through the sweep into honest endings.

We pay formal-infrastructure overhead and say so with measured numbers
(2026-08-26 live run): wake latency = cadence + grace + cadence (~30 min per
depth iteration), tick ≈ 16–18 s per 15 min (~2 % duty), deploy lag T+0 for
kernel code / T+2 cadences for the chain script. The overhead is **constant
per state transition** and amortizes: attempts get heavier (GPU training
hours per transition) and wider (N authors share one tick), so
overhead/total → 0. Cadence is itself a dial; the kernel already submits
`afterany`-dependent work at park time on the launch path, and extending
that fast requeue wake to checkpoint sleeps (today they ride the sweep —
"slow but correct", per the code) removes wake latency outright.

**The ablation is free and honest:** the `Compute` seam means the "python
loop" baseline is our own `LocalCompute` mode — same agents, same gates,
monolith vs decentralized, measuring overhead one way and preemption
survival the other. No strawman reimplementation.

## Autonomy modes

Drop the "human keeps the merge button" framing as a principle — it is the
DEFAULT, not a law. Merge policy becomes a contract knob (`merge:
manual | auto`, target owner's declaration, like a harness permission mode):
`auto` = gate + panel clean → the PR merges itself. NOTE the current
arming path is review-required-shaped (`arm_auto_merge_when_review_required`
— GitHub only arms against a pending requirement), so `auto` mode needs a
small publish change: merge directly when the gate CI is the sole
requirement, arm otherwise. Graded
autonomy is paper material: interventions per accepted step, measured at
each grade.

Hard prerequisites before any repo flips to `auto` — every taste standard
must live IN the gate once the human checkpoint is optional:
- the min_delta publish audit (yolo#16 shipped at +0.0379 against a 0.04
  floor; under `auto` it would have merged itself);
- the suite no-regression gate;
- the aggregation standard (a mixture of individually sub-floor tweaks must
  not clear the floor — the verify lens learned it in #167).

## Baselines and ablations (three tiers, no citation dump)

- **Tier 1 — on our substrate (config flips):** LocalCompute monolith;
  the grid origin cell (width 1 / depth 1 / panel off ≈ the Karpathy-loop
  semantics); autonomy modes manual vs auto.
- **Tier 2 — external, same benchmark:** Prime Intellect's step record +
  intervention/compute ledger; `karpathy/autoresearch` run **as-is** on our
  hardware (single GPU, 5-minute jobs — cheap); AIDE² via the shared
  AI4AI-Bench track.
- **Tier 3 — cite-and-position only:** AI-Scientist v2, Agent Laboratory,
  the framework surveys (paper-writing pipelines; different goal).

Comparison axes: substrate (resident loop vs stateless tick chain) · state
model (in-memory/markdown vs typed records + leases) · verification (none /
self-report / measured gate + panel taste) · autonomy (fixed vs mode dial) ·
scaling (1 GPU / bespoke cloud / any Slurm) · interventions per accepted
step · failure model (driver dies vs sweep heals).

## Build list (sequenced)

1. **min_delta gate audit** — DONE (#169: the gate enforces the contract's
   declared floor; strictly-greater boundary).
2. **Suite no-regression gate** — ALREADY BUILT (decide PHASE 2:
   `suite_regressed` is floor-aware per sibling, same-seed paired, fails
   closed; this list previously carried a stale "to build" — Mengye caught
   it).
3. **`merge: manual|auto` contract knob** + repo-settings runbook + the
   publish change (merge directly when the gate CI is the sole requirement;
   arm otherwise) — the LAST auto-mode item.
4. **Width dial**: portfolio climbs on one benchmark (lift
   MAX_ACTIVE_RUNS_PER_TARGET; attempt dedup/selection; merge/race policy
   for concurrent PRs).
5. **Multi-target servicing** (headline + pilot must coexist; the tick
   report fields are already shaped for it).
6. **GPU eval jobs** + noise-floor/min_delta calibration for the speedrun
   contract (seed-hacking defense = our private-seed re-measure).
7. **Speedrun target repo scaffold** (contract, ruler, baseline
   reproduction).
8. **Grid runs + baselines**, then the write-up.

Go-public gate (unchanged, tracked elsewhere): the pull_request_target
fork-PR exfiltration gap must close before any repo flips public.

## Open questions

- Torch GPU shape and burnable budget (decides wall-clock ambitions).
- AI4AI-Bench slice size (how many of the 10 tasks).
- Whether the public repo is this repo renamed or a fresh `outerloop` repo
  with this one as history (the merge-commit mirror rule constrains the
  mechanics either way).
