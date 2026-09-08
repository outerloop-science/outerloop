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
The kernel already reconciles a PR whose base moves AFTER it opens: an open PR
keeps its run in the in-review state, and the tick's follow-up conflict wake
(`followup.py`) fetches the moved base into the workspace and asks the agent to
merge and re-measure. So the only real gap is the first bullet — a line that
never re-syncs main mid-run and opens its PR against a base superseded days
earlier.

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
4. **Healing a PR after its base moves: already handled, no new mechanism.**
   An open PR's run stays in-review, and the follow-up conflict wake already
   fetches the moved base and asks the agent to merge and re-measure. The
   note's earlier "post-terminal" framing was wrong (terra, #341): the run is
   not terminated while its PR is open. Base reintegration is therefore stage 1
   alone — closing the during-run drift; the post-open case needs nothing new.
5. **Who pays the re-measure GPU: only when the agent keeps its change.** The
   re-measure is then a normal launch against the line's own budget. No eager
   baseline re-runs on every merge.

**Build order.** The whole feature is one stage: re-pin at wake with the digest
(decisions 1–3), the re-measure paid only on keep (decision 5). Decision 4 needs
no code — the existing follow-up conflict wake already covers a base that moves
after the PR opens.

## Why this shape addresses the problem

The base pin is correct during a run — you cannot measure improvement against
a moving target. The failure is not the pin; it is holding one pin for a
multi-day run and having no reconciliation once the run ends. Re-pinning at the
iteration boundary keeps the stability the pin gives while bounding the drift,
and extending the conflict wake past the run's terminal closes the orphaned-PR
gap. Both reuse machinery that already exists rather than adding a new one.
