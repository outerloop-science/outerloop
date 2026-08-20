# The research loop: breadth, depth, and one substrate

**Status: north-star (2026-08-20), not a spec.** Captures how we want the
research workflow to *feel* so the concrete builds don't quietly harden into a
restrictive one-shot pipeline. It gates nothing; it exists to keep us honest.
Pairs with `dispatcher.md` (the fire-and-wake substrate), `agent-substrate.md`
(experiment submission as an `act` syscall), and `scaling.md` (the outer loop,
seats, coordinated runs).

## The picture in one paragraph

Automated research is a **search that spends compute on two axes** over one
pause-and-resume substrate. **Breadth** is the outer loop: many attempts, run
independently over time. **Depth** is within an attempt: an agent tries
something, sees the result, reasons on it, forms or kills a hypothesis, and
tries again. Neither axis is the workflow; the workflow is *how much we spend on
each*, and the substrate underneath — fire off a job, end the session, wake when
it finishes — is the same whether the thing waking up is the grader or the
researcher.

## Two axes, one budget

Think of the compute as a grid: **M attempts × k iterations each.**

- **Pure breadth** (`k = 1`, large `M`): many fresh agents, each tries one
  thing, the gate says yes/no. Embarrassingly parallel and robust — a stuck
  agent never blocks the others. This is roughly where the system is today.
- **Pure depth** (`M = 1`, large `k`): one agent iterating on its own results —
  test-time scaling on the "keep thinking about *this* problem" axis.

The right split is **problem-dependent**: some benchmarks reward one deep chain,
others reward many cheap darts. So the split should be a **dial we can turn and
measure**, never a constant baked into the architecture. Whether depth pays on a
given benchmark is itself a question the system can answer.

## Staleness is the tax of breadth-only — and depth is its cure

Pure breadth has a hidden cost: every attempt starts **cold and amnesiac**. The
only way one attempt learns from earlier ones is by reading their **reports**,
and a report is (a) a *lossy compression* of the real reasoning and (b)
possibly *stale* — the code moved under it. "Read the prior report more closely"
is a patch on both problems, but it cannot beat either.

Depth dissolves the problem instead of patching it: an agent iterating on its
**own live results** never reads stale evidence — it knows exactly what it just
ran and why, uncompressed. So the depth axis is not merely "more compute"; it is
the principled fix for the staleness that breadth-only pays. (Cross-attempt
reports still earn their keep as cheap, lossy memory across the outer loop — they
are just not a substitute for a live research thread.)

## One substrate, two things it can wake

The pause/resume machinery is identical on both axes: **fire a job, end the
session, come back when it finishes.** What differs is *who wakes up*:

- Wake the **grader** → the gate's paired baseline/candidate/suite measurement
  runs as jobs without a session holding a GPU. (The near-term build.)
- Wake the **agent**, handing it the result → it keeps researching: another
  experiment, a new hypothesis, a revision. (The depth axis.)

The pre-PR panel revision — waking the author with blocking findings — is just a
**crippled, one-step instance** of "wake the agent with evidence." Generalize it
and you have the depth loop; the number of experiment-iterations an agent gets
before it must ship a candidate is the dial `k`, with `k = 1` recovering today's
breadth-only behavior.

## The non-restrictive principle

Because the machinery is shared, we do **not** have to choose breadth vs. depth
up front, and we must not hardwire the narrow case:

- Build the wake to **resume the agent in general**, not only to re-enter the
  grader's decision. The gate-wake is the *first use* of that machinery, not its
  shape.
- Keep `k` a **tunable policy**, not a constant. `k = 1` is a setting, not an
  assumption.
- Treat "does depth pay here?" as an **empirical question** the outer loop can
  measure, per benchmark.

## What we build first, and why it isn't a commitment

The **gate-wake** (dispatcher phase 1: measure the final candidate as jobs, wake
to decide) comes first — it is needed regardless of the depth dial, it de-risks
the exact pause/resume plumbing the depth loop reuses, and it unpauses the outer
loop. Building it agent-first (resume with evidence, the panel revision as the
first instance) means turning up depth later is a **knob, not a rewrite**.
