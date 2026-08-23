# Building out the research loop: substrate, depth, parallel

**Status: plan (2026-08-22), reviewable before code.** The concrete build-out of
the north-star in `research-loop.md` (breadth × depth over one fire-and-wake
substrate). It sequences three efforts — consolidate the role-runner, then the
**depth** axis, then **parallel dispatch** — and threads the integrity gate
through all three. Pairs with `consolidation.md` (kernel-as-OS, the staged plan),
`agent-substrate.md` ("the role-runner: one loop replaces five drivers"),
`dispatcher.md` (fire-and-wake, the phases), `scaling.md` (the outer loop), and
`orchestrator-verify.md` (the gate).

## The thesis in one paragraph

`research-loop.md` frames the work as a grid, **M attempts × k iterations**, over
one pause-and-resume substrate. Today the system lives at `k = 1` (breadth-only).
The two things that make it a *research* loop rather than a batch of darts are
**depth** (the agent iterating on its own results — the `k` axis) and **parallel
dispatch** (a portfolio explored at once — breadth *within* a climb, the `M` axis
pulled inside a single objective). Both are the same operation underneath —
*dispatch work, end the session, wake on results, decide the next move* — so they
must be built on **one** substrate, not two. That substrate is the consolidated
role-runner. And because both axes scale how much the loop *does*, the
benchmark-integrity gate (our recurring failure mode; see `climb-lessons` in the
notebook) has to grow with them, or we scale gaming. Hence: substrate first, then
the two axes, with the gate as a cross-cutting governor.

## The shape: one substrate, two axes, one governor

- **Substrate — the composition seam above the role-runner.** Running *one* role
  session is already consolidated: climb/steward/follow-up/judges all go through
  `run_role` + a RoleSpec (`consolidation.md` — the five-drivers collapse is
  done). What is *not* uniform is composing MANY invocations: *run role X, await
  its result (inline or dispatched), decide the next invocation.* Today the climb
  loop does that ad-hoc for its own case; depth and parallel both need it as a
  shared primitive. That seam — not re-consolidating `run_role` — is the substrate
  this plan builds.
- **Depth (the `k` axis) — serial.** After the agent sees its own measured
  result, it may iterate — revise, retry a different tack, refine — up to a
  contract-set `k`. `k = 1` is today; the panel-on-wake slices
  (`orchestrator-verify.md`) already built the special case "a blocking finding
  wakes the author to revise."
- **Parallel dispatch (the `M`-within-a-climb axis) — breadth.** Fan out a
  portfolio on one objective — candidate variants, seeds, or lenses — await them
  through the dispatcher, and deadline-salvage the best that returns in time.
- **Governor — the integrity gate.** More depth and more parallel candidates mean
  more surface to game the benchmark. The suite / no-regression gate (see
  `Metric taxonomy` below) is not a competing feature; it is what keeps the extra
  spend honest.

## Dependency called out first: the dispatcher must go live

Parallel dispatch, and the cross-job form of depth (park → wake → iterate),
both stand on the **experiment dispatcher** (`dispatcher.md`). Today the
dispatcher is *proven but dark*: activation was watched end-to-end, but no pilot
benchmark runs long enough to park, so the park → wake → publish and
panel/revise sub-paths are unexercised on real Slurm. **Nothing on the `M` axis,
and no cross-session depth, is real until a genuinely long-eval benchmark makes
the dispatcher run for real.** Lighting it up — a benchmark whose eval parks,
observed through a full park → wake → PR cycle — is the entry gate to Phases 2b
and 3. Inline (single-session) depth (Phase 2a) does not need it.

## Phase 1 — the invocation-composition seam (thin, feature-driven)

**Not a re-consolidation.** Running one role session is already unified through
`run_role` + RoleSpec (`consolidation.md`); do NOT redo that. Phase 1 is the layer
*above* it: a small, shared way to express **run role → await result (inline or
dispatched) → decide the next invocation**, which the climb loop currently does
ad-hoc and which both axes need.

**Goal.** Factor the climb's implicit run→measure→decide loop into a seam that
takes (a) how to run the next role invocation and (b) how to decide from a result
whether/what to invoke next — so depth (loop) and parallel (fan-out) are two
callers of the same seam, not two new drivers.

**Why first.** Built separately, depth and parallel each grow their own
orchestration — the mistake the config-driven author work (#125, replacing the
abandoned #123) taught us. One composition seam lets a new axis be an *app* on the
kernel.

**Thin, not speculative.** Extract exactly what the first depth slice (2a) needs
from the existing loop; let Phase 3 pull the fan-out shape. The test of "thin
enough" is that Phase 1 lands with **no user-visible behavior change** — same
climbs, same PRs, the loop just expressed through the seam.

**Acceptance.** The inline climb runs through the composition seam; a `k = 1`
result-driven decision is expressed as data (not lane-specific control flow); the
existing suite stays green; no new endings or gate behavior.

## Phase 2 — the depth axis (`k` as a dial)

**Goal.** Generalize "a blocking finding wakes the author" into *results-driven
depth*: on seeing its own measured result, the agent decides whether to iterate,
bounded by a contract dial `k` (and a spend budget), never a constant baked into
the code.

- **2a — inline depth (no dispatcher).** Within one session/job, the agent
  measures, reflects, and iterates up to `k`. This extends `panel_reads` /
  `panel_revisions` (already `k = 1` for the panel case) into general
  result-driven iteration. Ships without the dispatcher.
- **2b — cross-session depth (needs the dispatcher live).** The agent dispatches
  a real eval, the session ends, a wake resumes it *with the result*, and it
  iterates. This is `research-loop.md`'s "wake the agent with evidence" at
  arbitrary `k`, and it is the honest form for long evals.

**Design guards.** `k` and the depth budget live in the contract, measurable per
benchmark ("does depth pay here?" is itself a question the system answers). Each
iteration is a fresh gated candidate — depth never smuggles an unmeasured change
past the gate. A no-resume backend still drafts rather than blind-revising (the
`supports_resume` gate we already have).

**Acceptance.** A benchmark can be run at `k > 1`; the report shows the iteration
chain and what each step changed; a run that stops improving halts at `< k`
rather than burning the whole budget; depth is off (`k = 1`) by default until a
contract opts in.

## Phase 3 — parallel dispatch (`M` within a climb)

**Goal.** One objective explored as a **portfolio** — candidate variants, seeds,
or lenses — dispatched concurrently, coordinated by the agent, with
deadline-salvage picking the best that returns in time. This is the
`parallel-climb` note (portfolio + parallel lenses + deadline-salvage) made real.

**Leans hardest on Phases 1 and the live dispatcher.** Each portfolio member is a
role invocation (Phase 1) whose eval is a dispatched job (`dispatcher.md`
Phase 2/3, live). Fan-out, await-any, and salvage are dispatcher concerns, not
new per-lane machinery.

**Open questions to settle in-doc before building** (some already queued in
`parallel-climb`): how a portfolio shares vs. isolates workspaces; whether
members interact (a leader reading laggards) or stay blind; how the budget splits
across `M`; how deadline-salvage ranks partial returns; how parallel members
compose with depth (does a surviving member get `k` iterations?).

**Acceptance.** A climb can dispatch `M > 1` candidates on one benchmark;
partial returns are salvaged at the deadline; the winner is chosen by the gate,
not by which finished first; `M = 1` stays the default.

## The governor, threaded through: the integrity gate

Our climb-lessons keep showing the same failure — the agent games the
benchmark's structure rather than generalizing. Depth gives it more turns to
find a crack; parallel dispatch gives it more darts at one. So the gate scales
*with* the engine or the engine scales gaming:

- The **suite / no-regression gate** — a win on the climbed benchmark must not
  regress the others — has to be in force before Phase 2/3 turn up the volume.
- The **metric taxonomy** (climbed / diagnostic / gate / composite / group /
  suite) that the gate needs is its own companion doc; this plan depends on it
  but does not specify it. Land the taxonomy + suite gate alongside Phase 2, not
  after Phase 3.
- Depth's per-iteration gating and parallel's winner-by-gate rule (above) are the
  same principle applied locally: every extra unit of spend passes the same bar.

## Non-goals

- Not the outer breadth loop (many independent attempts over time, seats,
  coordinated runs) — that is `scaling.md`. This doc is depth, and breadth
  *within a single climb*.
- Not a new backend or harness — the role-runner is the existing seam
  (`consolidation.md`); backends stay swappable underneath.
- Not the metric taxonomy spec — referenced, companion doc.

## Sequencing summary

1. **Phase 1 — the invocation-composition seam** (thin; `run_role` itself is
   already consolidated). No dispatcher dependency. Enables everything after.
2. **Light up the dispatcher** on a real long-eval benchmark — the entry gate to
   2b and 3.
3. **Phase 2a — inline depth**, then **2b — cross-session depth** (after the
   dispatcher is live), with the **suite gate + metric taxonomy** landing
   alongside.
4. **Phase 3 — parallel dispatch**, on the composition seam + live dispatcher,
   with the gate as the winner-selector.

Depth before parallel deliberately: depth extends something already built
(panel-on-wake) and is cheaper to make honest; parallel is the larger new
capability and depends on more being in place.
