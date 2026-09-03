# The pre-PR verification loop

**Status: proposal (2026-08-15).** The mechanics of verification as a step of
the climb transaction. `judge-placement.md` argues *where* the verifier
belongs (inside the orchestrator) and why the reviewer stays external — this
note does not repeat that argument. It specifies the loop that runs there:
what happens between "the candidate measured improved" and "a PR exists",
who reads what, and when the loop stops. Pairs with `roles.md` (specs and
result-policies), `reviewer-infra.md` (threat model), and `consolidation.md`
(syscalls).

## Why pre-PR

Today the verifier reads bot PRs after they open. Moving it before the PR
changes what a PR *is*: the output of verification rather than the input to
it. A maintainer never sees half-baked work — the recurring cost of watching
early drafts was named the day the notes-vs-PR question first came up. Three
more things fall out of the same move:

- **Sessions never sit next to a token.** The orchestrator posts through the
  `act` syscall; judge sessions hold only their own model key. The
  GitHub-runner constraint that forced claude-only verification (codex and
  hermes can reach a runner's `GITHUB_TOKEN` via `/proc` or file reads)
  does not exist here — every benched backend is eligible for the panel.
- **Event-driven, not clock-driven.** The verify step fires when the
  candidate measures improved, inside the climb job — no second event plane.
- **The strong jail.** Judge sessions adopt the climb's apptainer
  containment when this step lands; today's GitHub-side judges run in the
  runner sandbox instead. Backend eligibility above rests on the token
  point, not on the jail.

## The loop

```
candidate measured improved (and suite-gated, when shared paths were touched)
  → PANEL: judge sessions, one per lens (run sequentially)
      verifier      — integrity lens (gaming, leakage, claim consistency);
                      strongest model
      code-reviewer — correctness/cleanliness lens; may run on a cheaper
                      or different-family backend
  → verdicts merged MECHANICALLY (kernel): any blocking finding wakes the
      author — same session, findings data-fenced, never instructions
  → author revises → orchestrator RE-MEASURES (always — a revision is a new
      candidate; scope, drift, floors, and the suite gate all re-apply)
  → panel re-reads
  → stop: a round with no new blocking findings, or the hard cap
  → PR posted, carrying the verification transcript
      (rounds, verdicts per lens, what changed between rounds)
```

Rules the loop takes from the house review policy:

- **Iterate only on blocking findings.** Advisory notes ride into the PR as
  notes; they never trigger a round.
- **Hard cap: one revision round after the initial read** (two rounds
  total), matching the house review policy. A loop that caps out with
  blocking findings still open posts a **draft PR** with those findings at
  the top: visible and plainly not merge-ready. Hiding capped-out work would
  suppress exactly the signal a human should see — a run that cannot
  converge is information.
- **Silence is never endorsement.** A panel session that errors or runs out
  of budget yields a skip stub in the transcript, and the PR says so.
- **Disagreement is information.** When lenses conflict (one blocks, one
  passes), the PR reports both verdicts; the kernel never adjudicates
  judgment — the human does.

## Lenses, not one judge

The verifier and the code reviewer stay separate sessions with separate
briefs, merged only at the verdict level:

- The **verifier** is a prosecutor: is the number real, is the benchmark
  being gamed, does the claim match the mechanism. Diluting that brief with
  style suggestions measurably softens it, and the backend bake-off showed
  models settle into complementary lenses rather than one covering both.
- The **code reviewer** points the existing advisory rubric at bot code
  pre-PR. Its findings default advisory: clean code must not veto sound
  science, and gaming findings must never be buried under style notes.
- The author's own **self-review skill** runs before the panel ever sees the
  diff. It raises draft quality and shortens rounds; it substitutes for
  nothing — the graded thing cannot produce its own grade, and same-model
  self-review shares the model's blind spots.

**Claim consistency, precisely.** A per-benchmark fork in shared code is
legitimate *staged* research when claimed as such — mechanism proven on the
toy benchmark first, larger benchmarks later, possibly coupled with other
innovations. The offense the verifier hunts is a claim/evidence mismatch: a
staged change claimed as universal, an aggregate raised while a component
collapsed, a number the mechanism cannot explain. The suite gate prices
shared-path contact mechanically; whether generality was *claimed honestly*
is the verifier's judgment (docs/roadmap.md, credit rule).

## Author egress: the retriever, not a browser

The author gets exactly one door to the outside: the retriever-in-harness
seam. The harness fetches from an allowlist (papers, library docs, package
registries), returns the content data-fenced, and logs every fetch into the
run record. No raw browsing, and `curl` closes when the containment story
grows network isolation. In order of weight:

1. **Contamination** — a browsing solver can look up its benchmark; with the
   retriever, "did the author fetch anything that touches the ruler" is an
   auditable question and a verifier lens item.
2. **Injection** — web content is untrusted instructions entering a session
   that writes measured code; data-fencing is the same boundary used for
   wake prompts and PR text.
3. **Reproducibility** — fetches are recorded with the run, so a hypothesis
   sourced from a page is replayable.

## Backend capability contract: add survivable compaction

Long runs accumulate context in the backend's session store, not in the
harness (which is stateless by design: brief in, result out, resume by id).
Each backend compacts its own window — validated for Claude Code, unproven
on our bench for codex and hermes. "Survivable compaction" therefore joins
resume, structured output, tool restriction, and cost accounting in the
backend capability contract, with an adversarial bench test: *does a
compacted author still honor the contract's scope and ruler rules?* The
mirror failure is already on record (a resumed agent honoring stale
constraints), so constraints summarized away is a real class, not a
hypothetical. The layers above the window stay as designed: externalize
before hibernating (author skill), data-fenced wakes re-ground facts, and
distillation of reports into lessons is the curator role — nothing
load-bearing lives only in a compressible medium.

## Re-reading a follow-up

A PR's content changes after publish in exactly one kernel-driven way: a
follow-up session answers review feedback with a code change, which the
responder re-measures and pushes. That push replaces the content the panel
blessed, so the record's `auto_blessed_head` is cleared — the tick arms a
self-merge only on an exact head match, so a changed PR could only ever be
merged by a human. The follow-up now closes that gap the same way the climb
opened the PR: **the same panel reads the new head.**

- The responder posts its reply and writes the record (cursors advanced,
  blessing cleared) FIRST, then runs the panel. Judges take minutes; a
  responder killed mid-read must cost an unarmed PR, never a silent push
  or a repeated wake.
- The panel's `base/` is the merge-base of the pushed head and the
  kernel-pinned base sha (after a base sync the two coincide); its contract
  text is that base's. The claim is a *follow-up re-measure on an open PR*,
  naming the PR's previous number — never a fresh pre-PR improvement claim.
- The read is posted on the thread under the follow-up marker, with the
  panel transcript and one closing line that says who merges.
- **Bless** — `auto_blessed_head = pushed sha` — only when the read is clean
  (no blocking finding, no degraded lens), the tree's contract says
  `merge: auto`, and the workspace HEAD still equals the pushed sha after
  the judges ran (they hold a shell next to the checkout). The tick then
  arms once GitHub reports the PR clean and up to date.
- Everything else is written down and left to a human: blocking findings
  (the panel's explicit demand), a degraded read, a manual dial (the owner's
  preference, not doubt), a panel that could not run, no trusted base.

The tick threads the climb's `--panel` to author follow-ups (never the
steward's — its PRs are not panel-blessed) and adds one read's walltime to
the job on top of the author's budget, both under the partition cap; it tells
the follow-up how many minutes the read actually got (`--panel-minutes`), so
a cap that eats the allowance costs the read — skipped, and said so on the
thread — never the author's session. The follow-up builds the panel only
after the run's OWN author is resolved; a judge key that equals that
author's credential (role separation on the keys themselves, not the paths)
drops the panel for this job and posts the skip, the same way — the reply
never depends on the panel. A panel config that would die at startup is
left off the follow-up (the reply still goes out; the PR stays
human-merged; the tick's contract alarm already names the misconfig).

**The revise loop is a wake type.** A blocking re-read (findings, not a
degraded lens) is recorded on the run — the panel's data-fenced findings and
the pushed head they were read on — and the tick submits a follow-up for it
exactly as it does for a maintainer's comment or a base move: every step a
job, on the one follow-up channel. The woken author addresses or rebuts the
findings; a code change is re-measured and re-read, and a read that still
blocks sets the wake again. Bounded by `PANEL_WAKE_CAP` revisions, after
which the findings are left to a human (the panel's explicit demand stands).
A wake is spent when a follow-up services it, and superseded by any later
push (it fires only while GitHub's head is the read head); an open panel
wake also holds off the auto-arm, whatever the record says about blessing.

## Measuring a follow-up's change

A follow-up's re-measure runs where the contract says the benchmark runs.
`gpus: 0` benchmarks are measured inline in the follow-up job, as before. A
GPU benchmark is never measured on that CPU node: the follow-up **seals** the
session's change as a commit on the PR's current head (the climb's snapshot,
line memory excluded), asks the dispatched measurer for one measure of that
tree on the GPU lane, and — on a cluster — **parks**: it posts the author's
reply now (the comments are serviced), records the sealed sha, its retaining
ref, the eval job ids and the fresh seed on the run, and ends. Nothing is
pushed yet. The tick polls those jobs; while they run, no comment is serviced
and nothing is armed (the sealed change lands first, so the next answer is
given on the tree the maintainer will actually see). When every job is
terminal the tick submits a follow-up, which finishes on the sealed tree:
ledger under the cross-seed floor, disarm, one commit with the standard
message, push, the measured-number comment, the candidate row, the body
edit, the record (blessing cleared, stage cleared, snapshot released) — and
then the panel re-read of the pushed head, exactly as for an inline change.
A synchronous compute (LocalCompute) returns the value at once and the same
follow-up applies it; a failed dispatched eval is abandoned and said on the
thread, with the workspace back on the pushed head.

Placement is derived from the contract's `gpus:` — the author never chooses
where its change is measured, and the follow-up carries the same cluster
coordinates the climb does.

## What changes on GitHub

- `verify.yml` on targets thins to defense-in-depth (a re-check of the
  posted PR) or retires once the pre-PR loop is live; the choice can be
  per-target.
- The **advisory reviewer for human PRs is untouched** — it is an external
  service with standalone value, and none of this note applies to it.

## Sequencing

1. **Verify step in the climb job** — BUILT (`panel.py` + the loop in
   `climb_once`; `build_panel_runner` prepares the checkouts): after
   measurement, the panel reads local pr-head/base worktrees, no GitHub
   round-trip. Findings gate PR posting.
2. **Author wake on blocking findings** — BUILT: loop inside the climb job,
   same session id, fully re-measured and re-gated per revision, cap
   enforced by the kernel; capped-out blocking opens a DRAFT PR and never
   arms auto-merge.
3. **Panel** — BUILT at the seam (lens list = kind x backend; the climb CLI
   parses `--panel verify:claude,review:hermes:MODEL`, and a hermes entry
   additionally needs `REVIEW_HERMES_REPO` — the pinned clone — in the
   climb job's environment); claude wired on the cluster, other backends as
   their host support lands (no token adjacency here, so eligibility is
   config).
4. **Transcript in the PR body** — BUILT (plus the run report); thinning or
   retiring the GitHub-side verifier stays per-target.
5. **Retriever egress** for the author, with fetch logging; network
   isolation joins the containment work when it lands.

Each stage is a reviewed PR; the loop is climb-only until it has run clean
on the pilot, then the steward's PRs adopt the same panel.
