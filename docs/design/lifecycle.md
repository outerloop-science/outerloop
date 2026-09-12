# The run lifecycle: states, messages, and what the kernel decides

**Status: design pass (2026-09-12), for review before code.** A re-read of the
whole lifecycle against one rule, from Mengye: benchmark verification and
launching jobs are rigid; everything else is the author deciding, through
tool calls. It absorbs Phase C of `research-loop-buildout.md` and supersedes
the follow-up sections of `roles.md` and `orchestrator-verify.md`. Built from
three inventories of the code as of main `8944625` (states and transitions,
inbound messages and syscalls, standing design rulings) and cross-checked by
an independent read of the note against the code; the numbers in brackets
refer to the kernel-decision inventory posted on the PR that carries this
note.

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
change: who may send a message and who may merge, that a human's commit is
never overwritten, isolation of the session, crash-proof liveness, a report on
every ending, the line notebook sealed at every terminal. Everything else is
judgment, and judgment belongs to the author: what to run, what a result
means, whether to answer a reviewer with an experiment or a sentence, when to
submit, when to stop. Where the kernel makes such a call today, that call
goes.

## What exists

Five states, one of them dead. `implementing` (a session runs), `waiting`
(parked on jobs), `in-review` (a PR is open), `concluding` (declared, never
written), `ended` with six endings. A run parks in three shapes: an
author-sleep park (the author launched and slept), a candidate park (the
gate's measures are jobs) and a submitted park (a candidate park the author
asked for). Each shape has its own wake path with its own decisions.

A run in review does not park in the record's sense. The tick services it on
a separate pipeline of two thousand lines that reads new comments, resumes
the session with a fixed prompt, and then decides for it: an in-scope edit is
re-measured as the PR's new candidate, which on a cluster is a fourth park
shape kept in `followup_stage`; a number inside the floor leaves the ledger
row alone; the row is folded into the sealed commit and pushed; a moved base
has a ladder of sync, conflict, withheld and superseded; a pushed change gets
a panel re-read with its own revision cap; the steward repeats most of this
under another key. Reviewers who asked for an ablation got the ablation
pushed as the PR head or, until this week, nothing when the push step failed.

The inventory counts fifty-six places where the kernel decides something
about the science or a reviewer's intent. About half are the gate, the meter,
authorization and merge authority, and stay. The rest cluster in five places:

| Cluster | Examples | Inventory |
| --- | --- | --- |
| review time | re-measure any edit; the base-sync ladder; six withhold wordings; "worse than before, stated plainly"; abandon when the head moved; the panel re-read cap | 25–37 |
| submit policy | on a metered benchmark a submit is refused until the run has launched; a metered finish without a submit is scored no-improvement, panel only; a submit needs a report | 7, 49, 50 |
| the finish | blocking findings at a plain finish open a draft; a degraded panel drafts; never arm when the base moved | 13–18 |
| base moves | the kernel re-pins the base and instructs the agent to merge; conflict and behind prompts written by the kernel | 26, 54 |
| the outer loop | an issue must name exactly one benchmark; which benchmark to climb next; the steward's mission text | 39, 42, 43 |

Messages reach a session by five channels: the brief at start, the launch
wake text, an `extra_update` that leads a submitted park's wake, the panel's
wake text, and the follow-up prompt with its four preambles. Thirteen verbs
exist, two of them the judges'. A session has no verb that posts anything to
GitHub. A comment cannot reach a session parked on jobs. A follow-up session
has no syscalls at all. Advisory panel findings never reach the author. The
sibling view is frozen at session start. The wake text and the brief also
carry research advice (keep experimenting while budget remains, conclude when
launches are exhausted, ready means measured, one hypothesis one change-set)
written into kernel code. Eleven retry counters bound the pipeline, most of
them protecting one hard-coded step from another. Two copies of the first
publish exist, one for a candidate wake and one for an inline finish, and an
inline submit drops the sibling launches staged beside it and asks the
author to stage them again.

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
run: before a PR, with a PR open, after a reviewer writes. The four park
shapes become one park with a job list that may be empty; the wake paths
become one.

A sleep on no jobs is a checkpoint. It wakes when a message arrives or at the
checkpoint deadline, whichever comes first, and the wake text says which. A
parked run with a PR and no jobs waits for messages without a deadline; idle
waiting spends no wake attempt and cannot end as stuck.

### Messages

A message is the unit the kernel delivers. Every message has a source, a
thread (the PR when one exists, else the issue the run claimed), a trust
level (everything but the kernel's own budget and clock lines is data, never
instructions), and an arrival time. The tick writes messages into the run's
inbox; the wake drains it in arrival order.

| Message | Source | Written by | Delivered |
| --- | --- | --- | --- |
| launch result: exit code, bounded tails, declared artifacts | the author's own job | the sweep, when the job is terminal | at the wake the sleep asked for |
| gate verdict: the paired numbers, the floor, the suite, or an eval error; the sealed tree, the base and the contract it was measured under | the kernel's measurement of a submitted tree | the sweep, when the gate jobs are terminal | at the wake |
| panel verdict: blocking and advisory findings, the transcript | judge sessions | the panel run | at the wake |
| human comment or review, on the run's thread | a person with standing; others as context, never as triggers | the tick's poll | at the next wake |
| base moved: the digest of what merged and the siblings' numbers | git, main | the tick, once per new base | at the next wake |
| budget and clock: counts remaining, the review top-up, walltime | the kernel | the wake itself | leads every wake |
| PR merged or closed | a human, on GitHub | the tick's poll | ends the run; not delivered |

The author's session never sees a raw comment body outside a fence, never
sees the seed, and never sees a message the kernel did not write into the
inbox. Advisory findings are delivered like blocking ones; the author decides
what to do with them. The sibling view is refreshed at every wake, not only
at start. The inbox keeps a position per GitHub collection, since issue
comments, reviews and review comments carry independent id sequences, and
delivers each message once.

Human messages do not interrupt a sleep on jobs. They wait in the inbox and
arrive with the job results. The author who wants to answer sooner takes a
checkpoint sleep. The kernel stays out of scheduling.

The inbox is files: a directory beside the run's record, one file per
message named by a sequence number, written by temp-and-rename, never
deleted, outside the workspace so the session cannot forge one. The record
holds the delivered position. A message file is durable before any poll
position advances, and the delivered position advances only after the wake's
session leg ends, so a wake that dies re-delivers; double delivery is
harmless, as it is for today's leases. Records, leases and the inbox all sit behind
one small storage interface (put, list, get, conditional put) with a
filesystem implementation for Slurm and local compute; a cloud backend gives
it an object-store implementation and nothing else changes. No messaging
service is needed: delivery only happens at a wake, which the kernel
triggers, so nothing waits on a push. A job-completion callback may later
wake the tick sooner than its cadence; that is a trigger, not a transport.

### The author's moves

| Move | What the kernel does | Rigid part |
| --- | --- | --- |
| `launch` | seals the tree, submits a contained job on the contract's lane, records it in the ledger, delivers the result at the wake | containment, placement, metering |
| `sleep` | parks the run on the launched jobs (possibly none) | the sleep count |
| `submit` | seals the tree, runs the gate and the panel as jobs, delivers the verdict as a message; a credited verdict publishes | the gate; the publish |
| `reply` | posts text on the thread the message came from, with secrets redacted and self-approval scrubbed, as the follow-up's reply is today | standing of the poster; redaction |
| `end` | ends the run with the author's report and the last verdict as its ending | the report |

`reply` and `end` are new. A session that stops without sleeping or
submitting ends the run with what it has, unmeasured. That is a change: today
a plain finish is measured by the gate on the tree it left, and can publish.
Under this note only a submit is measured, on every benchmark, so the author
always asks for its number. The read-only verbs stay: `status`, `queue`,
`history`, `siblings`, `reports`, `sync`. The steward has the same moves under
its own key, with its own scope.

The research advice now written into the wake text and the brief moves to
the role's instructions, where it can be edited without a kernel change. The
kernel's wake text states facts: the messages, the counts, the clock.

### Submit, and the one publish

A submit means: measure this sealed tree, and if it is credited, publish it.
The verdict comes back as a message whatever it says, naming the sealed
tree, the base and the contract it was measured under. A gate that says no is
not a terminal; the author reads the numbers and decides to try again, launch
more, or end. Today a plain finish's failed gate ends the run and a submitted
park's failed gate wakes the author; the second is right, and with `end` the
author can still choose the first.

The publish is the one rigid act after the gate, and there is one of it. When
no PR exists, the kernel opens one from the sealed tree with the number and
the author's report. When a PR exists, the kernel moves its head to the
sealed tree and posts the number, after confirming auto-merge is disarmed.
The move is a fast-forward only: the sealed tree must contain the PR's
current head. When a person pushed to the PR since the author last saw it,
the publish is refused and the fact is delivered as a message; the author
merges and submits again. A human's commit is never overwritten. The publish
is also refused when the base's contract no longer defines the benchmark
with the measurement the verdict was made under; a stale verdict is said,
not published.

Blocking findings at a publish open a draft, or keep the PR as it is, and are
delivered to the author with the verdict. The ledger row moves only when a
credited number beats the recorded best by the floor; every other number is
posted and leaves the row alone. A submit whose number is worse than the PR's
current one still moves the head, with the number stated plainly; the author
chose it, the thread shows it, and a human merges or not.

A steward submit is a ruler change and is measured as one: the full suite
runs, every sibling's eval is smoke-checked, and the credited number resets
the baseline instead of competing with it. That is the steward's publish; the
steward's copy of the review pipeline is not.

### The meter

One meter for the run's whole life: the launch count, the sleep count and the
GPU-hours the contract sets, spent from the first session to the last. When a
PR opens the counts get a small top-up the contract names, so review has
headroom, and every wake states the counts remaining and that the top-up was
added, so the author can plan for review while still climbing. A reply costs
nothing. A run that has spent everything can still reply and end.

### Endings

A run ends when the author ends it, when a human merges or closes its PR,
when the meter runs out, or when the kernel cannot continue: a crash, a
tampered workspace, or a kernel action that made no progress
`MAX_WAKE_ATTEMPTS` times, a failed publish retry included. A merge or close
ends the run at the next tick whatever it is doing: pending jobs are
cancelled, a session in flight finishes its leg and its publish is refused.
Every ending writes the report, seals the line notebook, releases the issue
claim when no PR exists, and cancels the run's live launches. No other path
ends a run. A gate verdict never ends a run by itself, and a reviewer's
comment never does.

## What stays rigid

| Kept | Why it is the kernel's |
| --- | --- |
| the paired measurement, the private seed, the floor, the suite, the cached baseline rule, the zero-change rule (an unchanged tree cannot be credited), the verdict bound to its sealed tree, base and contract | the number must be nobody's claim |
| scope on the diff before anything is sealed, launched or measured | the out-of-scope edit could be to the ruler |
| containment, the lane from the contract, `--nice` on launches, always queue, cancel on end | the session cannot hold GPUs or credentials |
| launch, sleep and GPU-hour counts; refusal on exhaustion with the numbers | the meter is the only bound on spend |
| the publish: open or fast-forward the PR head to the sealed tree, the ledger row rule, disarm before a head moves, never arm when the base moved, refuse when a human pushed or the contract moved, humans merge | credit, merge authority, and nobody's work overwritten |
| the steward's ruler measurement: full suite, sibling smoke checks, baseline reset | a ruler change must be verified as one |
| standing: which comments are messages, the bot's own markers, the task label; the issue claim and its release; one delivery per message | authorization and liveness |
| leases, the sweep, deadline floors, the stuck cap, the outage latch, the tamper guard, the report on every ending, the line seal at every terminal | liveness and audit |

## What becomes the author's, and what is deleted

| Today | After |
| --- | --- |
| an in-scope edit during review is re-measured as the PR candidate [25, 29] | nothing is measured unless submitted |
| the base-sync ladder: sync, conflict, behind, withheld, superseded, "the numbers above still stand" [25, 26, 30, 31] | one message, base moved; the author merges or not and submits or not |
| six withhold wordings for a reverted change [29] | the change is never reverted; the author's tree is the author's |
| abandon a finished re-measure when the head moved [34] | the publish is a fast-forward or a refusal delivered as a message |
| the panel re-read after a push, its two-revision cap [36, 37] | a submit runs the panel; findings are a message; the sleep count bounds revisions |
| `finish_attempts` | folds into `wake_attempts`: any kernel retry without progress counts |
| the kernel re-pins the base and tells the agent to merge; kernel-written conflict prompts [26, 54] | the base-moved message says what happened; no instruction |
| a submit is refused until the run has launched [49]; a metered finish without a submit is scored no-improvement [7]; a submit needs a report [50] | the meter and the gate are the constraints; the report is `end`'s and the publish's |
| a plain finish is measured; its failed gate ends the run as negative-result [9] | only a submit is measured; the verdict is a message; `end` is the author's |
| research advice in the wake text and the brief | the role's instructions |
| advisory findings never reach the author; siblings frozen at start | delivered; refreshed |
| two first-publish implementations; an inline submit that drops its sibling launches | one publish; one job-and-wake path on every backend |
| three cursors as record fields, `followup_stage`, `panel_wake_*`, `dirty_wake_head`, `extra_update`, `improve_prompt` | one inbox with per-collection positions, one renderer |
| the steward's copy of the pipeline; its mission text in kernel code [43, 45] | the steward is a role with the same moves and its own publish; its text is role configuration |
| `MAX_COMMENTS_PER_WAKE`, `PANEL_WAKE_CAP`, `finish_attempts`, `panel_wake_rounds` | gone; the sleep count and `wake_attempts` are the bounds |

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
inbox: positions per GitHub collection, last delivered message
meter: launches_used, sleeps_used, gpu_hours_used, review_topup_added
wake_attempts                          liveness only
```

Gone from the record: `followup_stage`, `followup_job_id`, `panel_wake_head`,
`panel_wake_text`, `panel_wake_rounds`, `dirty_wake_head`, the candidate and
submitted phases inside `stage`. `auto_blessed_head` stays only as long as
`merge: auto` does.

## Decisions

Settled (Mengye, 2026-09-12):

1. A human message waits for the jobs a run sleeps on; no interrupt.
2. One meter for the run's life, with a small top-up when the PR opens, stated
   in every wake.
3. A worse submit on an open PR moves the head, with the number posted
   plainly; the ledger row does not move.
4. `end` is a verb, so a report is asked for at the moment the author
   decides; a session that simply stops still ends the run with what it has.

Open:

5. **Auto-merge.** `architecture.md` lists "any form of auto-merge on code —
   never"; `install.md` documents `merge: auto` as an explicit per-repo opt-in
   under branch protection, and the code implements the opt-in. If the answer
   is never, the blessing, the arming and the disarm rule leave the record and
   the publish. Recommendation: keep the opt-in as the install guide states it
   and correct the architecture sentence; the repo owner turning the dial on
   is the human decision the principle protects.

## Sequencing

Each stage is one PR, reviewed, run from its commit on one fleet before any
release, and deletes what it replaces. A fleet never lacks a working path
between stages.

1. **One inbox, one renderer.** Every inbound message, including the ones
   delivered today, is written to the inbox and rendered by one function;
   pending blocking findings are read from the inbox from this stage on, so
   `panel_wake_text` can go. Advisory findings and a refreshed sibling view
   ride along. No lifecycle change yet.
2. **Messages reach a parked author.** `reply` exists. A run with an open PR
   parks; comments and base moves are inbox messages; the author can launch,
   reply and sleep in review. A session that edits code in review still goes
   through today's re-measure-and-push until the next stage replaces it.
3. **Submit in review, and `end`.** The publish moves the PR head by
   fast-forward; a gate verdict is a message on every path; the meter's
   top-up exists; the submit policies [7, 49, 50] go; the follow-up
   re-measure path no longer runs.
4. **Three states.** `running`, `parked`, `ended`; the record migrates on
   read. This stage lands only when no live record on any fleet carries
   `followup_stage`; the tick reports the count until it is zero. Then
   `followup.py`, the steward's pipeline copy, and the counters are deleted
   with their tests.

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
