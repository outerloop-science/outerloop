# Research lines: per-agent branches, selfness, and clean main PRs

Status: design for review — no code yet. Owner decisions embedded
(2026-08-31); implementation phased below.

## Problem

Every attempt forks the target's current main. When main moves (one agent's
technique merges), every other line of work loses its code lineage: its
insights survive only as archived reports, and re-deriving a divergent stack
each run is economically punished into extinction. Observed on gpt-speedrun:
the 35-hour warmdown line's recipe survived a run boundary only through its
report, and two agents unknowingly swept overlapping Muon directions.

Three coupled capabilities fix this:

1. **Research lines** — an agent can continue its own prior stack.
2. **Selfness** — an agent's next job and current sleeps have continuity in
   its own memory (owner's definition): one memory thread per agent slot,
   across sleeps AND successive runs.
3. **Fleet awareness** — an agent can see sibling directions and choose
   complementary work (shipped: the author-pulled `syscall siblings` tool).

## Substrate: one branch per agent (owner decision)

Each agent slot owns `agents/agent-NN` on the target repo.

- **Run start**: check out the agent's branch (created from main if absent),
  then MERGE MAIN IN before the session begins. Conflicts are the session's
  first honest task — that is real research life, and it keeps divergence
  debt visible and continuously paid down.
- **Run end** (any terminal state): push the session's final tree to the
  branch as a SNAPSHOT commit. Today `snapshot()` seals the tree only on the
  measured/submit/no-improvement paths — session-error, session-budget, and
  session-outage endings return before sealing, and the wake path can drop a
  saved snapshot. Phase 1 therefore EXTENDS sealing to every terminal path
  (best-effort on error endings — a crashed session's tree is still notebook-
  worthy) and the push publishes that sealed sha; the push itself never
  invents a commit. Progress pushes are the
  agent's lab notebook: ungated, panel may skim cheaply later. One slot
  never runs twice concurrently, so pushes are fast-forwards.
- **Selfness memory rides the branch**: `AGENT_MEMORY.md` at the branch
  root, author-owned, bounded, updated each session, rendered into the brief
  as the agent's own memory (data-fenced). Memory, code, and beliefs travel
  as one lineage — and cross clusters for free, since the branch lives on
  the shared repo.
- **Selective integration is git**: harvesting a sibling's technique or
  main's progress = merge/cherry-pick, not bespoke machinery. One carve-out:
  `AGENT_MEMORY.md` is slot-private — any merge into a line keeps the
  RECEIVER's copy (kernel-side merge resolves that path as `ours`), so a
  sibling's memory never overwrites or conflicts with the receiving slot's.

Why branches over a kept-ref (sealed-sha) pool: continuity and integration
come from git itself; the lineage is reviewable history; the public repo
narrative is legible. Sealed shas remain the gate's measurement anchors.
Kept refs may return later as optional side-lines (one branch per agent in
v1).

## Credit semantics: unchanged, and main stays clean (owner decision)

- Lines change NOTHING about credit semantics — the credit model is
  research-loop.md's portfolio rule, restated: a claim beats its OWN
  declared baseline (the gate's paired base/candidate evals already
  implement exactly this), and SOTA is a tracked property of main, not a
  separate gate. Every claim is legible about which baseline it was
  measured against.
  Transparency, not a freshest-main requirement, is the anti-gaming defense.
  A line needs no new baseline infrastructure either way: the metric is
  absolute and each claim names its baseline pair.
- **A PR to main is ONE clean, ablated contribution**: the agent extracts
  the winning change from its branch, re-applies it onto main, measures THAT
  candidate, and submits the minimal diff. Never a wholesale branch merge of
  unablated accumulated tweaks. Branch = notebook; main = ledger.
- Enforcement: the brief teaches the extraction workflow; the panel's
  mandate extends to rejecting grab-bag candidates; a diff-stat advisory in
  the gate is a tripwire, not a blocker. The suite no-regression gate prices
  a stale line reverting others' wins.

## Clarifications from design review

- **Credit baseline with a divergent line**: the metric is absolute, so a
  line-based candidate gets a real number regardless of its base. Credit
  follows research-loop.md's portfolio rule (beat the claim's own declared
  baseline, always legible; SOTA tracked on main) — see "Credit semantics"
  above; lines add no third rule and no per-line baseline infrastructure.
- **Branch resets**: an agent may deliberately reset its line to main; the
  kernel performs it as `--force-with-lease` on the agent's OWN branch only
  (the only non-fast-forward ever allowed), recorded in the run report.
  Nothing may force-push any other ref.
- **Instruction-file isolation**: an agent branch is an AUTHOR-WRITTEN tree.
  Checking one out inherits the same sanitization as any untrusted tree
  (the CLAUDE.md/hooks instruction-smuggling class): harness-instruction
  files are neutralized in the working copy exactly as the reviewer's
  --bare/sanitize path does, so a line cannot inject instructions into its
  successor sessions — or a sibling's.
- **Multi-fleet naming**: `agents/<fleet>/agent-NN` when more than one
  cluster climbs the same target (fleet id from the kernel config; single-
  fleet targets may omit the segment). Prevents two clusters' agent-01
  colliding on one branch.

## Risks and answers

- **Divergence debt**: merge-main-at-run-start keeps it continuously paid;
  a line that stops paying loses at the moving baseline anyway.
- **Herding vs portfolio**: sibling visibility is pull-only with one
  steering sentence; lines pull the other way (diversity). Watch, don't
  over-steer.
- **Line abandonment**: branch age is visible; the steward may propose
  pruning; an agent may also deliberately reset its branch to main.
- **Injection surface**: branch content and AGENT_MEMORY.md are
  author-written — fenced as data wherever rendered, like reports.
- **Repo hygiene**: agent branches are bot-pushed refs on the public target;
  naming (`agents/`) keeps them grouped and obviously non-human.

## Phases

1. **Branches** (kernel): run-init checkout+merge-main WITH the
   instruction-file isolation above (the author clone path has no
   sanitize step today, so this phase adds one: harness-instruction files
   in a line checkout — CLAUDE.md, AGENTS.md, .claude/ — are reset to
   main's reviewed versions; the line's self-notes live in the data-fenced
   AGENT_MEMORY.md instead); terminal push, sealing extended to every
   terminal path; per-target contract opt-in (`lines: true`). ~2–3 PRs.
2. **Selfness memory**: AGENT_MEMORY.md render into the brief + author
   instruction to maintain it. 1 PR.
3. **Hygiene**: brief extraction workflow + panel mandate + diff-stat
   advisory. 1 PR.
4. **Board**: strip shows the line each agent continues; later a lines view.

Out of scope here: multi-line-per-agent (kept refs), planner-assigned
directions, cross-target lines.
