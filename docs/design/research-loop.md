# The research loop: breadth, depth, and one substrate

**Status: north-star (2026-08-20; mechanism crystallized 2026-08-23), not a
spec.** Captures how we want the research workflow to *feel* so the concrete
builds don't quietly harden into a restrictive one-shot pipeline. It gates
nothing; it exists to keep us honest. Pairs with `dispatcher.md` (the
fire-and-wake substrate), `agent-substrate.md` (experiment submission as an
`act` syscall), and `scaling.md` (the outer loop, seats, coordinated runs).
`research-loop-buildout.md` is the concrete, phased build-out of this picture.

## The picture in one paragraph

Automated research is a **search that spends compute on two axes** over one
pause-and-resume substrate. **Breadth** is the outer loop: many attempts, run
independently over time. **Depth** is within an attempt: an agent tries
something, sees the result, reasons on it, forms or kills a hypothesis, and
tries again. Neither axis is the workflow; the workflow is *how much we spend on
each*, and the substrate underneath — fire off a job, end the session, wake when
it finishes — is one primitive the **author itself invokes**, whether what comes
back is an experiment's output or a review's verdict.

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

## The mechanism: one syscall, author-directed (2026-08-23)

The whole loop reduces to **one primitive, triggered by the author**. The author
lives in a sandbox. Anything it cannot compute there — an eval, a training run,
any real experiment — runs *outside*. To get a result, the author **dispatches
the job and sleeps; the substrate wakes it with the result delivered back into
the sandbox.** That is the entire kernel-author interface for doing research:

- **Run an experiment** → dispatch, sleep, wake with the output. Free-form
  experimentation signal; early in an attempt this is most of what happens.
- **Submit for review** → "I think this is ready," sleep, wake with the gate's
  verdict and the panel's/reviewers' comments. Review signal; the same syscall
  with a different payload.

The author interleaves these however its judgment says: experiment, experiment,
submit, read the comments, run *another* experiment, revise, resubmit. Review is
**not an orchestrator stage** the run passes through — it is something the
author *asks for*, exactly like an experiment. (The gate's paired measurement
still runs as jobs with no session holding a GPU; that machinery is now the
*inside* of the submit payload, not a stage of its own.)

**Launching and sleeping are separate acts.** `launch` is non-blocking — the
author can start a job and keep working, start several, or submit and *then*
decide when to sleep. `sleep(handles)` hibernates until the named jobs finish
and wakes the author with their results. (A fused launch-and-sleep call may
exist as syntactic sugar; the split is the primitive, so *not* sleeping is
always an option.) Submits batch the same way: submit several candidates, sleep
until all the reviews are back.

**The session clock is visible, and sleep refreshes it.** The session itself
runs under a job walltime; each harness round reminds the author how much time
remains before it would be forced to sleep. And a sleep with nothing to wait on
is legitimate: "checkpoint me and re-schedule me" — the author wakes in a fresh
job with a fresh clock, spending a sleep count like any other. Running out of
walltime becomes an author-managed handoff, not a kill.

**Depth is this syscall's budget — independent generous counts per kind.**
Launches, submits, and sleeps each get their own `k` count (or a wall-clock
minute budget). Launches meter external compute; the **sleep count meters wake
cycles** — it is what bounds a session's lifetime, since clock-refresh sleeps
would otherwise make it immortal — and batching is still rewarded (ten launches
under one sleep burn one sleep). The counts are visible in the prompt, the
author knows a submit burns its count, and running low draws a **warning, never
an enforced reserve** — the author's judgment, not the meter, decides when to
stop. `k = 1` recovers today's one-shot behavior.

## What the kernel dictates — and what it must not

The contract between kernel and author is deliberately minimal: *here is the
codebase and scope; here is the benchmark your result will be judged on; produce
a better program; here is your budget.* Everything between — what to measure
along the way, which ablations to run, what signal to trust, what to return —
is the **author's judgment**. The kernel must not prescribe a dev/eval protocol,
a fixed program to run between sleeps, or a structured decide-next policy; any
of those is the same over-structuring mistake in a new costume.

Two boundaries stay kernel-owned, and only these:

- **The gate is the untouchable ruler.** The one authoritative measurement on
  the fixed benchmark, run inside the submit payload. The author never gets the
  frozen test/seed to iterate against (ruler-fishing is our recurring failure
  mode; see `climb-lessons`), and the suite / no-regression gate keeps extra
  spend honest.
- **The meter.** Launches, submits, sleeps, and minutes are counted by the
  kernel, generously; the counts are the author's to see and plan against.

## The non-restrictive principle

Because the machinery is one primitive, we do **not** have to choose breadth
vs. depth up front, and we must not hardwire the narrow case:

- Build the wake to **resume the author in general** — the syscall returns its
  result into the *same* session — not only to re-enter a grader's decision.
- Keep the budget a **tunable policy**, not a constant. `k = 1` is a setting,
  not an assumption.
- Treat "does depth pay here?" as an **empirical question** the outer loop can
  measure, per benchmark.

A consequence worth naming: **parallel dispatch stops being a separate axis to
build.** An author that can dispatch before sleeping can dispatch *several*
things before sleeping — a portfolio within an attempt is the author's choice on
the same syscall, not new orchestration.

## What we build first, and why it isn't a commitment

The crux is the **suspend-on-syscall, wake-with-result** capability: the author
invokes the dispatch tool, the session hibernates (no GPU, no held job), and the
substrate later resumes *that same session* with the external result as the
call's return. The park/wake plumbing (dispatcher, `afterany` wakes, session
resume) already exists orchestrator-triggered; the build is handing the trigger
to the author. Build it once for experiments; submit-for-review layers on as a
payload, not a second mechanism.

## The finish is agent-driven too

Turning a credited improvement into a PR is not the orchestrator's job to
mechanize. The first-pass finish today does the rigid thing — merge the base
branch as it stands *now*, re-measure, and hard-fail if the candidate no longer
beats the freshly-merged base. That is fragile (a concurrent merge can kill a
real improvement) and it miscasts the orchestrator as the judge of research.

The finish belongs to the agent, on the same wake machinery:

- **Show a benefit; don't stack on top.** Like a research paper, a change
  demonstrates its method beats *its baseline* — it does not have to be rebased
  onto the newest `main` to be valid. Keep the paired baseline/candidate claim;
  do not force "re-beat whatever `main` became."
- **Complications go back to the agent.** A moved base, a conflict, a suite
  that no longer looks clean — the wake hands these to the agent, which pulls
  the latest, merges, does more research/coding if it needs to, and opens the PR
  only when there is no conflict *and* the experiments still show the benefit.
  Same "wake the agent with new information" as every other depth step.
- **Staleness is re-waking, not merging.** A PR that goes stale later wakes the
  agent again (perhaps a human pings it) to pull the latest and edit the PR —
  never an orchestrator auto-merge.
- **Humans merge, for now.** The human merges at their discretion. Eventually a
  planner agent may hold some merge authority — a deliberate, later exception to
  the README's "humans hold merge authority," decided explicitly rather than
  drifted into.

So the improved-wake is not a mechanical commit-and-push; it is the first place
the depth axis touches the outside world. Build it as an agent-wake, not as
orchestrator plumbing.

## Two kinds of win

A contribution does not have to be state-of-the-art to be real. We credit two
kinds, and want **both**:

1. **SOTA** — the best absolute number on the benchmark. Inherently
   competitive: it is about being on top, so it does care about the current
   best.
2. **A composable win** — beat your own baseline with a clean, orthogonal story
   that plugs into other methods on the leaderboard and still delivers. Not
   necessarily the single best number, but a genuine building block: "+2% that
   composes with anything."

This is what "show a benefit, like a research paper" means concretely — a paper
can be a new SOTA *or* a composable technique, and both are valid.

Two consequences:

- **The gate credits beating your baseline (type 2 by default); SOTA is a
  separate, tracked-but-not-required axis.** We do not collapse everything into
  "must beat the freshest `main`" — that only ever credits type 1 and throws
  away real composable wins. It is also exactly the fragile moved-base hard-fail
  we are dropping.
- **The leaderboard is a portfolio, not a single champion.** It carries the
  SOTA entry *and* the composable contributions, each legible about which
  baseline/method it is measured relative to, so composability is visible. Two
  orthogonal +2% wins are two entries — and combining them is itself a third
  experiment worth running.

The agent's finish declares which kind of win it is claiming, and the PR tells
that story; the human (or, later, a planner) judges.

**Composability with SOTA is a bonus, not a bar — and simplicity is its own
win.** A composable block need not stack onto the full SOTA. If SOTA is
`A+B+C+D` and we find `E`, then `A+E` landing *near* SOTA is a **cleaner** result
than `A+B+C+D+E` — fewer moving parts for the same place, and parsimony is a
real research value, not just the number. And even when `E` adds nothing on top
of the full stack (`A+B+C+D+E ≈ A+B+C+D` within noise), `E` is worth keeping as
a **future choice** if it could *substitute* for part of the stack (`E` in place
of `B+C+D`). So the portfolio holds building blocks and their relationships —
composes-with, might-replace — scored on performance *and* simplicity *and*
optionality, never a single number. Composition itself becomes a search: "is
`A+E` a cleaner path to near-SOTA?" and "can `E` replace `B+C+D`?" are
experiments the portfolio generates.

## Who picks the comparison — the agent frames, the gate keeps it honest

The `A+E` win only makes sense against the *right* comparison, and choosing it
is where the current design falls short of the vision.

**Where the gate is today.** The gate measures `base_sha` (the full current
`main`, e.g. `A+B+C+D`) against the candidate, head-to-head. So a candidate that
is `A+E` — `E` swapped in for `B+C+D` — must beat the *whole* stack: `E` alone
has to match everything `B+C+D` contributed. That bar rejects exactly the wins
we want to credit: `A+E` landing *near* SOTA with fewer parts, or `E` as a
viable *substitute*, both lose on the single number even though they are real
contributions. Head-to-head-vs-`main` only ever credits SOTA.

**Where it needs to go.** The fix is not "always compare to `A`" — letting an
agent pick a weak baseline is the gaming door we must not open. It is to split
the two roles cleanly:

- **The agent frames the comparison.** It constructs the configs its story needs
  — `A`, `A+E`, `A+B+C+D`, `A+E`-minus-`B+C+D` — runs those ablations itself (the
  depth axis, via an eval tool), and makes a *specific* claim: "`A+E` is within
  noise of `A+B+C+D` with three fewer components," or "`E` replaces `B+C+D`:
  `A+E ≥ A+B+C+D`."
- **The gate keeps it honest.** It re-runs *exactly the comparison the agent
  declared* on the fixed benchmark. The agent chooses the framing; it can never
  fake the numbers. This is the same un-gameable measurement that made the whole
  system trustworthy, pointed at a claim instead of a fixed head-to-head.
- **Transparency, not prohibition, stops weak-baseline gaming.** Every portfolio
  entry is legible about the baseline it was measured against, so "measured vs
  `A`, not the SOTA stack" is *visible* — and the well-rounded judge (and the
  human) weighs whether the framing is honest and the win is real. A weak
  baseline is allowed but exposed, not hidden.

**The concrete gap.** Today `candidate_sha = base_sha + the agent's edits`, and
there is no notion of measuring config `X` vs config `Y` beyond base-vs-candidate
— and `A` is not even a commit that exists when `main` is `A+B+C+D`. Closing
this is the agent-runs-experiments / declared-claim work: the same depth-axis
substrate, letting the agent build and compare the configs its claim needs while
the gate verifies the one it declared.
