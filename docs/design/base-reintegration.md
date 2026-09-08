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

## Decisions (owner, 2026-09-08)

The semantic choices, settled — the mechanics follow from them.

1. **Re-pin cadence: only when main moved.** Each wake does a cheap
   fetch-and-compare; it re-merges and re-pins only when a sibling actually
   landed something. No churn when nothing changed.
2. **What the agent sees: the record move, plus each sibling PR's metric and a
   one-line summary — not full diffs.** Enough to decide whether its line is
   still worth pursuing, without flooding its context.
3. **A superseded axis: tell, don't force.** The wake digest says its axis was
   beaten (e.g. "warmdown was taken further by #10"); the agent decides to
   pivot or push on. The kernel never kills a line for it.
4. **Healing a PR after the run ended: re-instantiate on demand.** A lightweight
   reconcile session is woken only when the PR actually conflicts; we do not
   hold a slot parked for days. The run's records persist and the session wakes
   against them.
5. **Who pays the re-measure GPU: only when the agent keeps its change.** The
   re-measure is then a normal launch against the line's own budget. No eager
   baseline re-runs on every merge.

**Build order.** Stage 1 is decisions 1–3 and 5: re-pin at wake with the
digest, and the re-measure paid only on keep. Stage 2 is decision 4, the
post-terminal reconcile, which reuses the existing stale/conflict wake.

## Why this is the right shape

The base pin is correct during a run — you cannot measure improvement against
a moving target. The failure is not the pin; it is holding one pin for a
multi-day run and having no reconciliation once the run ends. Re-pinning at the
iteration boundary keeps the stability the pin gives while bounding the drift,
and extending the conflict wake past the run's terminal closes the orphaned-PR
gap. Both reuse machinery that already exists rather than adding a new one.
