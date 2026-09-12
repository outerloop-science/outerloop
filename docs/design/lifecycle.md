# The run lifecycle: states, messages, and what the kernel decides

**Status: design pass (2026-09-12), for review before code.** A re-read of the
whole lifecycle against one rule, from Mengye: benchmark verification and
launching jobs are rigid; everything else is the author deciding, through
tool calls. It absorbs Phase C of `research-loop-buildout.md` and supersedes
the follow-up sections of `roles.md` and `orchestrator-verify.md`. Built from
three inventories of the code as of main `8944625` (states and transitions,
inbound messages and syscalls, standing design rulings); the numbers in
brackets refer to the kernel-decision list in the first inventory, kept in
the PR that carries this note.

## The rule

The kernel owns two things because nobody else can be trusted with them:

- **The gate.** The credited number is the kernel's paired measurement of a
  sealed tree on the fixed benchmark under a private seed, with scope checked
  on the diff and the suite re-measured when shared paths moved. The author
  never sees the seed, never edits the ledger, and cannot make a number.
- **Launching.** Jobs run outside the sandbox under the kernel's containment,
  on the lane the contract names, metered in counts and GPU-hours, always
  queued, cancelled when the run ends.

Around those sit invariants that are not science and are not the author's to
change: who may send a message and who may merge, isolation of the session,
crash-proof liveness, a report on every ending, the line notebook sealed at
every terminal. Everything else is judgment, and judgment belongs to the
author: what to run, what a result means, whether to answer a reviewer with an
experiment or a sentence, when to submit, when to stop. Where the kernel makes
such a call today, that call goes.

## What exists

Five states, one of them dead. `implementing` (a session runs), `waiting`
(parked on jobs), `in-review` (a PR is open), `concluding` (declared, never
written), `ended` with six endings. A run parks in three shapes: an
author-sleep park (the author launched and slept), a candidate park (the
gate's measures are jobs) and a submitted park (a candidate park the author
asked for). Each shape has its own wake path with its own decisions.

A run in review does not park. The tick services it on a separate pipeline of
two thousand lines that reads new comments, resumes the session with a fixed
prompt, and then decides for it: an in-scope edit is re-measured as the PR's
new candidate; a number inside the floor leaves the ledger row alone; the row
is folded into the sealed commit and pushed; a moved base has a ladder of
sync, conflict, withheld and superseded; a pushed change gets a panel re-read
with its own revision cap; the steward repeats most of this under another key.
Reviewers who asked for an ablation got the ablation pushed as the PR head, or
nothing when the push step failed.

The inventory counts fifty-six places where the kernel decides something
about the science or a reviewer's intent. About half are the gate, the meter,
authorization and merge authority, and stay. The rest cluster in five places:

| Cluster | Examples | Inventory |
| --- | --- | --- |
| review time | re-measure any edit; the base-sync ladder; six withhold wordings; "worse than before, stated plainly"; abandon when the head moved; the panel re-read cap | 25–37 |
| submit policy | a submit is refused until a launch has returned results; a metered finish without a submit is scored no-improvement, panel only; a submit needs a report | 7, 49, 50 |
| the finish | blocking findings at a plain finish open a draft; a degraded panel drafts; never arm when the base moved | 13–18 |
| base moves | the kernel re-pins the base and instructs the agent to merge; conflict and behind prompts written by the kernel | 26, 54 |
| the outer loop | an issue must name exactly one benchmark; which benchmark to climb next; the steward's mission text | 39, 42, 43 |

Messages reach a session by five channels: the brief at start, the launch
wake text, an `extra_update` that leads a submitted park's wake, the panel's
wake text, and the follow-up prompt with its four preambles. Twelve syscalls
exist. A session has no verb that posts anything to GitHub. A comment cannot
reach a session parked on jobs. A follow-up session has no syscalls at all.
Advisory panel findings never reach the author. The sibling view is frozen at
session start. Eleven retry counters bound the pipeline, most of them
protecting one hard-coded step from another.

## The lifecycle

### Three states

| State | Meaning | Leaves it |
| --- | --- | --- |
| `running` | a session is live in a job | the session ends: it slept (park), or it stopped (end) |
| `parked` | no session; the run waits for jobs it launched, for messages, or both | a wake (the same session resumes), or a human ends the PR |
| `ended` | terminal, with a report | never |

A PR being open is a fact about a run, recorded in `pr_url`, not a state. A
parked run with a PR is what `in-review` was. `implementing` is `running`;
`waiting` and `in-review` are `parked`; `concluding` is deleted. The six
endings stay as they are: merged, rejected, negative result, budget exhausted,
aborted, stuck. They are how a human reads the board, and every one still
produces a report.

### One engine: park and wake

A run parks when its session ends with a sleep. A run wakes when the kernel
has something to deliver: the jobs the session slept on have finished, or a
message arrived for a run that is not waiting on jobs. A wake resumes the
same session with everything in its inbox rendered as one data-fenced text.
That is the whole engine, the same on every backend and in every phase of a
run: before a PR, with a PR open, after a reviewer writes. The three park
shapes become one park with a job list that may be empty; the three wake
paths become one.

### Messages

A message is the unit the kernel delivers. Every message has a source, a
trust level (everything but the kernel's own budget and clock lines is data,
never instructions), and an arrival time. The tick writes messages into the
run's inbox; the wake drains it in arrival order.

| Message | Source | Written by | Delivered |
| --- | --- | --- | --- |
| launch result: exit code, bounded tails, declared artifacts | the author's own job | the sweep, when the job is terminal | at the wake the sleep asked for |
| gate verdict: the paired numbers, the floor, the suite, or an eval error | the kernel's measurement of a submitted tree | the sweep, when the gate jobs are terminal | at the wake |
| panel verdict: blocking and advisory findings, the transcript | judge sessions | the panel run | at the wake |
| human comment or review, on the PR or the issue | a person with standing; others as context, never as triggers | the tick's poll | at the next wake |
| base moved: the digest of what merged and the siblings' numbers | git, main | the tick, once per new base | at the next wake |
| budget and clock: counts remaining, walltime | the kernel | the wake itself | leads every wake |
| PR merged or closed | a human, on GitHub | the tick's poll | ends the run; not delivered |

The author's session never sees a raw comment body outside a fence, never
sees the seed, and never sees a message the kernel did not write into the
inbox. Advisory findings are delivered like blocking ones; the author decides
what to do with them. The sibling view is refreshed at every wake, not only
at start.

Human messages do not interrupt a sleep on jobs. They wait in the inbox and
arrive with the job results. The author who wants to answer sooner sleeps on
nothing, which is a checkpoint sleep and costs a sleep like any other. The
kernel stays out of scheduling.

### The author's moves

| Move | What the kernel does | Rigid part |
| --- | --- | --- |
| `launch` | seals the tree, submits a contained job on the contract's lane, records it in the ledger, delivers the result at the wake | containment, placement, metering |
| `sleep` | parks the run on the launched jobs (possibly none) | the sleep count |
| `submit` | seals the tree, runs the gate and the panel as jobs, delivers the verdict as a message; an improved verdict publishes | the gate; the publish |
| `reply` | posts text, as written, on the thread the message came from | standing of the poster; redaction |
| `end` | ends the run with the author's report and the last verdict as its ending | the report |

`reply` is new. `end` is today's "the session stopped without sleeping",
given a name and a report; a session that simply stops still ends the run.
The read-only verbs stay: `status`, `queue`, `history`, `siblings`, `reports`,
`sync`. The steward has the same moves under its own key, with its own scope.

### Submit, and the one publish

A submit means: measure this sealed tree, and if it is credited, publish it.
The verdict comes back as a message whatever it says. A gate that says no is
not a terminal; the author reads the numbers and decides to try again, launch
more, or end. Today a plain finish's failed gate ends the run and a submitted
park's failed gate wakes the author; the second is right, and with `end` the
author can still choose the first.

The publish is the one rigid act after the gate. When no PR exists, the kernel
opens one from the sealed tree with the number and the author's report. When
a PR exists, the kernel moves its head to the sealed tree and posts the
number, after confirming auto-merge is disarmed. Blocking findings at a
publish open a draft, or keep the PR as it is, and are delivered to the author
with the verdict. The ledger row moves only when a credited number beats the
recorded best by the floor; every other number is posted and leaves the row
alone. A submit whose number is worse than the PR's current one still moves
the head, with the number stated plainly; the author chose it, the thread
shows it, and a human merges or not.

### Endings

A run ends when the author ends it, when a human merges or closes its PR,
when the meter runs out, or when the kernel cannot continue: a crash, a
tampered workspace, or a wake that made no progress `MAX_WAKE_ATTEMPTS`
times. Every ending writes the report, seals the line notebook, releases the
issue claim when no PR exists, and cancels the run's live launches. No other
path ends a run. In particular, a gate verdict never ends a run by itself,
and a reviewer's comment never does.

## What stays rigid

| Kept | Why it is the kernel's |
| --- | --- |
| the paired measurement, the private seed, the floor, the suite, the cached baseline rule, the zero-change rule (an unchanged tree cannot be credited) | the number must be nobody's claim |
| scope on the diff before anything is sealed, launched or measured | the out-of-scope edit could be to the ruler |
| containment, the lane from the contract, `--nice` on launches, always queue, cancel on end | the session cannot hold GPUs or credentials |
| launch, sleep and GPU-hour counts; refusal on exhaustion with the numbers | the meter is the only bound on spend |
| the publish: open or move the PR head to the sealed tree, the ledger row rule, disarm before a head moves, never arm when the base moved, humans merge | credit and merge authority |
| standing: which comments are messages, the bot's own markers, the task label; the issue claim and its release | authorization |
| leases, the sweep, deadline floors, the stuck cap, the outage latch, the tamper guard, the report on every ending, the line seal at every terminal | liveness and audit |

## What becomes the author's, and what is deleted

| Today | After |
| --- | --- |
| an in-scope edit during review is re-measured as the PR candidate [25, 29] | nothing is measured unless submitted |
| the base-sync ladder: sync, conflict, behind, withheld, superseded, "the numbers above still stand" [25, 26, 30, 31] | one message, base moved; the author merges or not and submits or not |
| six withhold wordings for a reverted change [29] | the change is never reverted; the author's tree is the author's |
| abandon a finished re-measure when the head moved [34] | a submit measures the tree the author has; the head moves to it |
| the panel re-read after a push, its two-revision cap, `finish_attempts` [36, 37] | a submit runs the panel; findings are a message; the sleep count bounds revisions |
| the kernel re-pins the base and tells the agent to merge; kernel-written conflict prompts [26, 54] | the base-moved message says what happened; no instruction |
| a submit is refused until a launch has returned [49]; a metered finish without a submit is scored no-improvement [7]; a submit needs a report [50] | the meter and the gate are the constraints; the report is `end`'s and the publish's |
| a plain finish's failed gate ends the run as negative-result [9] | the verdict is a message; `end` is the author's |
| advisory findings never reach the author; siblings frozen at start | delivered; refreshed |
| three comment cursors, `followup_stage`, `panel_wake_*`, `dirty_wake_head`, `extra_update`, `improve_prompt` | one inbox, one cursor, one renderer |
| the steward's copy of the pipeline; its mission text in kernel code [43–46] | the steward is a role with the same moves; its text is role configuration |
| `MAX_COMMENTS_PER_WAKE`, `PANEL_WAKE_CAP`, `finish_attempts`, `panel_wake_rounds` | gone; the sleep count is the wake budget |

Out of this pass, and named so nobody reads their absence as a decision: the
outer loop's choices (which issue, which benchmark next, an issue naming one
benchmark [39–42]) belong to a planner note; the declared-comparison gate and
the portfolio ledger stay in `research-loop.md`.

## The record

```
run_id, target, benchmark, agent_id, issue_number
state: running | parked | ended        ending, ending_note
pr_url                                 the fact a PR is open
session: backend, model, key_file, resume_session_id
park: jobs (afterany ids), sealed_ref, base_sha, base_branch, deadline
inbox_cursor                           the last message id delivered
meter: launches_used, sleeps_used, gpu_hours_used
wake_attempts                          liveness only
```

Gone from the record: the three cursors, `followup_stage`, `followup_job_id`,
`panel_wake_head`, `panel_wake_text`, `panel_wake_rounds`, `dirty_wake_head`,
the candidate and submitted phases inside `stage`. `auto_blessed_head` stays
only as long as `merge: auto` does; whether it does is a standing
contradiction between `architecture.md` and `install.md`, settled elsewhere.

## Open decisions

1. **Interrupting a sleep.** Recommended above: a human message waits for the
   jobs. The alternative, waking early on a comment, makes the kernel a
   scheduler and doubles the wake paths.
2. **The meter in review.** One meter for the run's whole life, topped up by
   the contract if a target wants generous review time. The alternative is a
   second budget with its own counters.
3. **A worse submit on an open PR.** Head moves, number posted, row unchanged.
4. **`end` as a verb.** Recommended: yes, so a report is asked for at the
   moment the author decides; a session that simply stops still ends the run
   with what it has.
5. **Auto-merge.** `install.md` documents `merge: auto`; `architecture.md`
   says never on code. Not this pass's to build, but the record shape depends
   on it.

## Sequencing

Each stage is one PR, reviewed, run from its commit on one fleet before any
release, and deletes what it replaces.

1. **One inbox, one renderer.** Every inbound message, including the ones
   delivered today, goes through the inbox and one wake renderer. Advisory
   findings and a refreshed sibling view ride along. No lifecycle change yet;
   `extra_update`, `panel_wake_text` and the follow-up preambles go.
2. **Messages reach a parked author.** `reply` exists. A run with an open PR
   parks; comments and base moves are inbox messages; the author can launch,
   reply and sleep in review. The follow-up pipeline no longer runs for
   comments; it still handles nothing else.
3. **Submit in review.** The publish moves the PR head; a gate verdict is a
   message on every path; `end` exists. The submit policies [7, 49, 50] go.
4. **Three states.** `running`, `parked`, `ended`; the record migrates on
   read; `followup.py`, the steward's pipeline copy, and the counters are
   deleted with their tests.

## Standing contradictions this settles

- Launches are submitted at sleep time, not when `launch` is called: kept.
  Staging is the primitive; the author keeps working after a launch and the
  jobs start when it sleeps.
- There is a live channel to a running session, the watcher, for read-only
  questions. `roles.md`'s "no live channel" is stale.
- A follow-up's change is not re-measured by the orchestrator; only a submit
  is measured. `roles.md` and `orchestrator-verify.md` are superseded here.
- The panel re-read loop after a push is retired; a submit runs the panel.
- A moved base is told, never forced; the finish's "merge and re-measure
  both sides" in `roles.md`'s flow is retired.
- One batch in flight per run is not a rule; launch several, sleep once.
