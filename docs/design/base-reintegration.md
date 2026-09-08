# Base reintegration: keeping a running line current, and reconciling its PR

Status: proposal (2026-09-08). Extends `research-lines.md`, which owns the
per-agent branch and the "merge main in at run start" rule. Prompted by a
real case, PR #12 on gpt-speedrun.

## The problem, concretely

agent-04's line started 2026-09-04 in the morning. That evening a sibling
merged #10, moving the record from 8192 to 7808. agent-04's line kept
iterating on its own 8192-era base for three more days and opened PR #12
claiming 7232, against a base that had been superseded on day one.

The claim was real: 7232 came from setting `warmdown_iters` to 6400, the same
knob #10 set to 4352, on a monotonic trend, and it holds against the current
base. But the PR conflicted, and by the time it opened, the run had
terminated, so nothing in the kernel could reconcile it. A human merged it by
hand.

Two gaps produced this:

- A line merges main only at run start. A long depth run therefore drifts
  arbitrarily far from main as siblings merge, and `research-lines.md` already
  names the danger ("a stale line reverting others' wins").
- The kernel has a stale-and-conflict wake for an in-review PR whose base
  moves, but it needs a live, parkable run to target. Once a run ends by
  opening its PR, there is no session left to wake, and the PR is stranded.

## Proposed protocol

1. **Re-pin at each wake, not only at run start.** When a line wakes for its
   next depth iteration, the kernel merges main into the line and hands the
   agent a short digest of what changed since it last ran — the sibling record
   moves and the files they touched. The agent decides what to re-run given
   the new base. This bounds drift to a single iteration instead of the whole
   run.

2. **Never mid-experiment.** A running eval keeps its base. Re-pinning happens
   only at the iteration boundary, the sleep-to-wake seam, so no in-flight
   measurement is invalidated and no launch is wasted.

3. **Re-measure the baseline at the new merge-base.** This is the roadmap's
   "baseline re-run at merge-base." `research-lines.md` already makes every
   claim name the baseline pair it was measured against, so a re-pinned line
   stays legible about which base each number used.

4. **Keep a PR reconcilable after the run's terminal.** A line whose PR is
   open stays reachable until the PR is merged or closed, so a base that moves
   after the PR opens fires the existing conflict wake — merge main,
   re-measure, push — instead of stranding the PR for a human.

## Open questions for the owner (decide before building)

These are the semantic choices; the mechanics follow from them.

- **Re-pin cadence.** Every wake, or only when main actually moved? Only-when-
  moved avoids needless re-merges and re-measures, at the cost of a cheap
  check each wake.
- **The "what changed" digest.** How far back does it reach, and how much does
  the agent see — just that the record moved, or the sibling's diff? This is
  what the agent reasons over to decide whether to abandon or keep its current
  line of attack.
- **Superseded axes.** When a sibling lands a strictly better result on the
  same knob the line is exploring (another agent's better warmdown, say), is
  the line told to drop that axis, or left to rediscover that it is beaten?
  This is the `scaling.md` "wake-and-reintegrate for superseded siblings"
  question.
- **Post-terminal cost.** Keep the run parked while its PR is open, holding a
  slot, or re-instantiate a lightweight reconciliation session only when the
  PR actually conflicts?
- **Whose GPU.** Re-measuring a baseline at a moved merge-base costs a run. Is
  it always paid, or only when a claim is actually being re-evaluated against
  the new base?

## Why this is the right shape

The base pin is correct during a run — you cannot measure improvement against
a moving target. The failure is not the pin; it is holding one pin for a
multi-day run and having no reconciliation once the run ends. Re-pinning at the
iteration boundary keeps the stability the pin gives while bounding the drift,
and extending the conflict wake past the run's terminal closes the orphaned-PR
gap. Both reuse machinery that already exists rather than adding a new one.
