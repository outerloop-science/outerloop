# Where the judges live, and how their output flows

**Status: living reference (v1, 2026-08-14).** Captures a design direction, not
a today-change. It records why the reviewer and the verifier — which share a
"read-only agent judge" implementation — belong in opposite homes, and what that
implies for their output, their identities, and how many of them we run. Pairs
with `reviewer-infra.md` (the seams and the threat model), `roles.md` (RoleSpec
and result-policy), and `consolidation.md` (the kernel as a multi-agent OS and
its syscalls).

## The thesis

The reviewer and the verifier look like the same thing — a read-only agent that
reads a diff and its surrounding code and returns findings. They are not the
same thing. They are pointed at different authors, triggered by different
events, and carry different stakes, and once you see that, their correct homes
are opposite:

- **The reviewer is a service for humans.** External trigger, advisory, a cheap
  drop-in any repo can switch on without buying into the climber. It stays a
  standalone GitHub Actions workflow.
- **The verifier is the second half of the climb transaction.** Internal
  trigger, gates an autonomous agent, may need GPU. It belongs inside the
  orchestrator.

The dividing line is **what each judge is pointed at**, and trigger, stakes, and
jail all fall on the same side of it.

## The verifier belongs in the orchestrator

The load-bearing argument is not security or sandboxing — it is that
**verification is already a kernel operation.** In the six-syscall vocabulary
(run-a-role, measure, gate, act, isolate, persist), verifying a climb is
`run-a-role(verifier)` → `measure`/`gate` → `act(post)`. It is expressible
entirely in what the kernel already does. So the question inverts: the
GitHub-Action verifier is not the baseline the in-orchestrator design must beat,
it is the **outlier** — a second event plane bolted onto a kernel that already
owns the other half of the same transaction.

Four things follow:

1. **The trigger is internal.** The kernel *opened* the bot climb PR, so it
   already knows verification is due. Routing "a PR was opened" out to GitHub and
   back to trigger a workflow is indirection for an event born inside the system.
   In-orchestrator, the climb is one atomic transaction — propose → verify →
   gate — instead of a handoff across two systems with a consistency gap. It also
   sheds machinery the reviewer needs but the verifier does not: no
   `pull_request_target`, no fork gate (bot PRs are same-repo by construction),
   no reusable-workflow secret plumbing.
2. **The strong jail is where it belongs.** The verifier gates an autonomous
   climber; the crown-jewel attack is a *gamed verifier* that merges bad climbs.
   That deserves the cluster's apptainer jail, which it gets for free by living
   where the author already runs.
3. **Compute is already there.** Deep verification re-runs a climb's eval on GPU.
   That is cluster-bound. In-orchestrator, the agentic-read verify and the
   deep-execute verify are one role on one substrate, not a split brain.
4. **The security objection collapses.** In-orchestrator, the verifier reads
   untrusted model-generated code near the powerful bot PAT (which can merge),
   whereas the GitHub Action runs with only a weak repo-scoped token. That sounds
   like a downgrade — but the orchestrator **already runs the author**, which
   *executes* that same model-generated code next to that same PAT, safely
   (apptainer, scrubbed session env, PAT never in the session). Execution is the
   harder case; read-only verification is strictly easier to contain. If you
   cannot safely verify there, you already cannot safely author there — and then
   the whole climber is unsafe regardless.

**Timing, not direction.** Better design is not the same as do-it-now. The
GitHub-Action verifier works today and is free and parallel; moving it in is real
work, coupled to two things maturing first: the orchestrator becoming
event-reactive (so verification does not wait on the `:00/:30` poll grid) and the
deep-verify/GPU path. Keep the GitHub-Action verifier as the interim — but do not
let "interim" quietly turn a reusable verify workflow into permanent legacy. That
reframes the current per-repo verifier migration as scaffolding, not the
end-state.

## The reviewer stays external — and its weak jail is correct

The reviewer fires when a *human* opens a PR — an event that genuinely
originates outside the system, so GitHub's event delivery is its natural home. It
is advisory, and it is a cheap outlet any repo can enable without the climber. Its
GitHub-hosted jail is weaker on the containment axis (no strong OS jail there) but
*stronger* on the blast-radius axis: ephemeral, off the lab network, nothing to
persist, no lab hardware to pivot into. For judging human PRs advisorily, that is
the right trade, not a compromise.

This dissolves the "the reviewer's sandbox is weaker than the author's" worry
rather than requiring a substrate rebuild. The reviewer *should not* have the
strong jail; the strong jail belongs to the verifier, which gets it by living in
the orchestrator. (One backend caveat: on the auto reviewer path only Claude is
trusted, because its `Read` tool refuses `/proc` while codex's shell and hermes's
file-read do not — see `reviewer-infra.md` for the per-backend threat model.)

Consequence: target repos only ever host the *reviewer*. The verifier collapses
into the orchestrator, so a repo the climber targets needs no verify workflow at
all. The reviewer is the distributable product; the verifier is orchestrator-
central.

## Output: notes by default, a PR as a promotion

A GitHub-Action verifier is structurally **PR-first**: the bot opens the PR, then
the verifier comments — so by the time verification runs, half-baked research is
already on a human's screen. You cannot hold it back. Only an in-orchestrator
verifier can gate *before* anything is human-visible.

So the model is **notes by default, the PR as a promotion**:

- A climb is a *candidate*. The verifier's output lands as a note in research
  memory — the always-on record the orchestrator reasons over.
- The kernel opens a PR **only when the climb clears the gate**: the climbed
  metric improved, the wider metric suite shows no regression, and the verifier
  finds no gaming. The note becomes that PR's opening body. From that point on,
  comments are the channel — once a PR exists, that is the right place for both
  the verifier and humans to speak.

It is one lever — *when the PR is born* — not two channels. It reframes
human-in-the-loop from **push-everything** to **pull + curated-push**: humans
browse research memory when they want (pull), and the gate decides what earns a
PR (push). Fatigue disappears because humans stop seeing the incompetent 90%.

**A draft-PR hybrid** gives both audit trail and quiet: the bot opens the PR as a
*draft* (it exists, both author and verifier can comment, full history), but no
human is requested or notified until the gate flips it to ready-for-review.

**The thing to protect against:** do not let "notes" become a black hole. Early
human correction is a *teaching* signal — a real one only happens because a human
saw the work (e.g. catching "this is task-overfitting, frame it as an initializer,
not an architecture choice" on an early climb). If every half-baked climb silently
vanishes into memory, that channel is lost. So the target is push-per-attempt →
**pull-or-promote**, not hide: keep the notes browsable, and consider a periodic
digest, so a human can teach on their cadence instead of being paged per attempt.

## Identities: author and verifier are different principals

Once a PR exists, author and verifier both speak on it, and a human must be able
to tell them apart. Three layers, the first being the important one:

1. **Separate identities.** The author and the verifier must not wear the same
   face. The author posts as the climber bot; the verifier posts as a *distinct*
   account (a second GitHub App). A judge speaking in the author's voice is both
   confusing and a trust problem — the verdict must not be forgeable as the
   author. This also makes concrete that the verifier gating the author is a real
   check between two principals, not the author blessing itself.
2. **Native structure.** The author is the PR itself (diff, commits,
   description). The verifier speaks through GitHub **reviews** — a distinct
   mechanism from issue comments, with approve/request-changes state and inline
   annotations.
3. **Machine marker.** An HTML sentinel (e.g. `<!-- autoresearch:verifier -->`)
   so the kernel can find and update its own comment idempotently.

## How many judges: a perspective-diverse ensemble

Judges with different priors find disjoint bug classes. In one bake-off on a real
PR, a security-leaning model found a `/proc` token-leak in the workflow design
while a correctness-leaning model found a test that no longer tested what it
claimed — neither found the other's. So for the high-stakes verifier, run **more
than one judge with assigned lenses** (integrity/security, and correctness/repro)
and merge, rather than one general judge. The coverage is additive, not
redundant.

This composes with the gate: require *both* lenses to pass before a climb is
promoted. Cost is not the obstacle — judged reviews land at roughly one to ten
cents each (prompt caching keeps even the thorough models cheap), so two lenses is
about fifteen cents. Pick judges on latency and coverage, not price. A lean, sharp
judge and a thorough, exploratory one are complementary: run the lean one always,
add the thorough one where depth is worth the extra turns.

## What is now vs later

- **Now:** the reviewer is the live agent path (claude-only on the auto path); the
  verifier remains a GitHub-Action interim; output is PR comments.
- **Later, sequenced to the fork-PR / public phase and the event-reactive kernel:**
  the verifier moves into the orchestrator; the tokenless split lands (needed for
  the reviewer's non-Claude backends and for running untrusted code next to a
  token, per `public-surface.md`); output moves to notes-with-promotion; separate
  identities; the ensemble.

Nothing here is urgent. It is the shape to grow into, written down so the interim
is recognized as interim.
