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
  → PANEL: parallel read-only sessions, one per lens
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

## What changes on GitHub

- `verify.yml` on targets thins to defense-in-depth (a re-check of the
  posted PR) or retires once the pre-PR loop is live; the choice can be
  per-target.
- The **advisory reviewer for human PRs is untouched** — it is an external
  service with standalone value, and none of this note applies to it.

## Sequencing

1. **Verify step in the climb job**: after measurement, run the verifier
   spec through the role-runner against the local pr-head/base checkouts the
   climb already has — no GitHub round-trip. Findings gate PR posting.
2. **Author wake on blocking findings**: loop inside the climb job, same
   session id, re-measure on every revision. Round cap enforced by the
   kernel.
3. **Panel**: add the code-reviewer lens; enable codex/hermes backends for
   it (no token adjacency in the orchestrator).
4. **Transcript in the PR body**; thin or retire the GitHub-side verifier
   per target.
5. **Retriever egress** for the author, with fetch logging; network
   isolation joins the containment work when it lands.

Each stage is a reviewed PR; the loop is climb-only until it has run clean
on the pilot, then the steward's PRs adopt the same panel.
