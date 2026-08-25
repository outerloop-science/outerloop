# The role CLI: one tool surface per agentic role

**Status: plan (2026-08-24), reviewable before code.** The author's launch/sleep
tool (#133) proved a pattern worth generalizing: the agent-facing surface for
every kernel interaction is a **kernel-installed CLI**; any file or schema
behind it is **internal ABI**. This doc plans that rollout across the roles and
the orchestrator. Pairs with `research-loop-buildout.md` (Phase A/B — the first
two consumers), `agent-substrate.md` (the `act` syscalls this makes concrete),
and `consolidation.md` (kernel-as-OS).

## Why a CLI is the right surface

Two distinct wins, and each future application should name which one it buys:

- **Win A — crossing the sandbox/trust boundary.** The agent is contained; the
  kernel holds the PAT/keys/cluster and performs the privileged act. A CLI verb
  is how the agent *directs* an act it must never *execute*. (The launch/sleep
  tool; GitHub writes; curated egress.)
- **Win B — interactive validation replaces parse-and-repair.** Today a judge
  emits a schema-constrained final message and the role-runner parses,
  validates, and **repairs once** (`role_runner._repair_prompt`) — a lossy
  retry that burns a turn and can still fail. A CLI validates each field **on
  the call**: bad input fails immediately, in-session, with a message the agent
  acts on. The artifact is well-formed **by construction**. (Judge verdicts;
  planner work-orders; the author tool's fast checks.)

The invariants, proven on #132/#133 and non-negotiable everywhere:

1. **The CLI is never the trust boundary.** Every verb is re-validated
   kernel-side (authoritative validators, PAT-out-of-session, scope, budgets).
   The tool is ergonomics and fast feedback; authority stays in the kernel.
2. **Sandbox-side tools are standalone** (stdlib-only — the target repo has no
   autoresearch), which duplicates a little advisory validation, pinned by
   parity tests (`test_artifact_path_check_matches_the_kernel`).
   **Orchestrator-side roles** (reviewer/verifier/panel/planner/steward run on
   our own checkout, not a target sandbox) import the real validators — no
   duplication.
3. **No legacy on migration.** When a role's CLI lands, the surface it replaces
   (the schema-repair path for that role, the inference it replaces) is deleted
   in the same PR — never two ways to say the same thing.
4. **Verbs are RoleSpec-gated.** The RoleSpec already caps each role's
   tools/scope/key; the CLI's live verbs are part of that cap. It is ONE tool
   (`.autoresearch/syscall`) whose verbs the brief exposes per role — an author
   uses `launch`/`sleep`, a judge uses `finding`/`conclude` — and a syscall is
   TYPED, so the kernel dispatches by type (a sleep parks + wakes; a verdict is
   read back). Every role runs the tool the same way: a shell in the jail. Roles
   differ by system prompt, verbs, and output handling — not by a bespoke
   containment mechanism (the jail contains them all).

## End-state

One `research` CLI identity, instantiated per role with only that role's verbs:

One `.autoresearch/syscall` tool, its live verbs gated per role by RoleSpec:

| Role | Verbs | Win | Installed where |
| --- | --- | --- | --- |
| author | `launch` / `note` / `status` / `sleep` (#133) · `submit` (Phase B) · later `pr`, `retrieve` | A | sandbox (standalone) |
| reviewer / verifier / panel | `finding` / `conclude` (verdict as a syscall type) | B | orchestrator-side |
| planner | `plan propose` | B (+ policy-as-validation) | orchestrator-side |
| steward | contract-edit + GitHub-write verbs | A + B | orchestrator-side |

This is kernel-as-OS made concrete, and the same conclusion as the flow-DSL
rejection: **the syscall is the abstraction** — extension is new verbs and new
payloads, never a new language and never a kernel change per role.

## Phases

Ordered by dependency and value; each phase is its own reviewed PR (or two),
lands with tests, and deletes what it replaces.

### Phase 0 — finish Phase A (in flight, prerequisite)

The wake for `author-sleep` parks: gather each launch's results from the run
dir, deliver declared artifacts into `.autoresearch/results/<name>/`, resume
the **same session** through the climb's resume-entry (#129) with the results
data-fenced, update the budget file, and flip `AUTHOR_SLEEP_WAKE_READY`. Plus
the brief/skill section that advertises the tool (never advertised before it
can wake). **Acceptance:** an armed author launches, hibernates through a real
(faked-Slurm in tests) job, wakes with output + artifacts, continues, and the
whole loop is byte-identical when unarmed.

### Phase 1 — `submit`: review as a verb (buildout Phase B)

**Prerequisite: #133 merged** (the author launch/sleep CLI — in review as this
plan is written; the tool this phase grows a verb on).
`syscall submit` on the author tool: the agent declares its candidate ready;
the kernel seals, runs the gate (private seed) + panel as jobs, and wakes the
author with verdict + comments; the author revises / experiments more /
resubmits / concludes. Retires the orchestrator-driven panel-revision loop
(`panel_revisions` policy re-entry) **in the same PR** (invariant 3).
**Acceptance:** submit → sleep → wake-with-findings → further experiment →
resubmit → PR, on one session thread; a clean first submit still opens a PR.

### Phase 2 — judge verdicts as a syscall type (highest reliability leverage)

A verdict is not a second tool — it is a syscall TYPE on the one surface.
Reviewer/verifier/panel stop emitting one schema-constrained final message.
Instead: `python .autoresearch/syscall finding --file --line --confidence
--summary --detail [--blocking] [--kind] [--category]` per finding, then
`... conclude --notes ...` to commit — each call validated on the spot, the
verdict assembled kernel-side (`type: "verdict"`), well-formed by construction.
`read_verdict` deletes the judge parse-and-repair path (`output_schema` handling
in `role_runner`) for migrated roles — the recurring repair tax disappears.
Orchestrator-side, so it imports the kernel validators directly.

**The read-only-judge tension, resolved by unifying with the author:** running
the CLI needs a shell, and the earlier read-only-tool-set posture (claude no
Bash, codex `--sandbox read-only`, hermes `terminal` off) made "CLI-over-Bash"
awkward per backend — a three-cornered asymmetry (scoped grants, sandbox modes,
direct-ABI writes) that was defense-in-depth ON TOP of the jail every role
already runs in. That asymmetry is entirely a security *choice*, not a technical
limit — every harness can run a shell (the authors do). So judges run a shell in
the jail, exactly like authors: the CLI is then uniform across all backends and
there is no per-backend machinery. The security floor is the author's existing
posture — a scrubbed env (no `/proc`-readable token), the tokenless split (no
PAT in the session; findings are data, a separate step posts), an ephemeral
jail, and the egress posture — which contains a shell-judge just as it contains
an author.

**Landed (the surface unification):** the one surface — `finding`/`conclude`
verbs, the typed ABI, `read_verdict`, the force-owning `install_tool` — replaces
the standalone `verdict` tool. `run_role` gates the verdict path on
`spec.verdict_tool` alone (the deployment builds the harness to match the role).
INERT until a spec sets `verdict_tool=True`. **Follow-up (harness unification):**
route judges through the same executing-harness-in-a-jail setup authors use
(differing only by system prompt + verbs + output handling), flip `verdict_tool`
on, and delete `build_reviewer_harness`'s read-only branches and the judge
parse-and-repair path.

**Acceptance:** a judge session produces a verdict with zero repair rounds;
a malformed call is corrected in-session; the judge can run the tool and
provably nothing else (an attempted other command is refused); the repair-loop
code for migrated roles is gone; verdict quality (findings parse rate) is
measurably ≥ today's.

### Phase 3 — the finish: GitHub writes as verbs

`pr open` / `pr comment` / `ledger update`: the author (or its wake) explicitly
directs the publish the kernel currently infers from session output. PAT stays
kernel-side (Win A); "title too long", "body cites out-of-scope path" fail
in-session. This is `research-loop.md`'s agent-driven finish given its concrete
surface. **Acceptance:** the finish flows through verbs; inference-based
publish paths are deleted where superseded; a PR body/claim problem surfaces to
the agent before anything is pushed.

### Phase 4 — planner + retriever verbs (when those roles/tools land)

- `plan propose --benchmark --phenomenon --evidence`: the work-order schema as
  a CLI whose validation **encodes policy** — a `--mechanism` field does not
  exist, so "phenomena and evidence, never mechanisms" is enforced by the
  surface, not by hope.
- `retrieve <query>`: curated egress performed kernel-side against the
  allowlist; "host not allowed" is an in-session error. Centralizes the egress
  posture (author keeps internet; hardening is allowlist-shaped, never an air
  gap).

## Non-goals

- **No flow DSL** — rejected in `research-loop-buildout.md`; this plan is more
  verbs on the same syscall abstraction, not a language.
- **No MCP server / backend-specific tooling** — the CLI-over-Bash surface is
  what keeps claude and codex (and any future backend with a shell) identical.
- **No trust migration** — no phase moves authority into a tool; every verb's
  kernel-side validator is the boundary before and after.
- **Not a rewrite of role plumbing** — `run_role`/RoleSpec stay; the CLI is a
  surface on top, and only replaced *surfaces* (schema-repair, publish
  inference) are deleted.

## Sequencing summary

1. **Phase 0** — the wake + flip the interlock + advertise (completes Phase A).
2. **Phase 1** — `submit` (buildout Phase B), retiring panel-revision-as-stage.
3. **Phase 2** — judge-verdict CLI, deleting the parse-and-repair tax.
4. **Phase 3** — the finish as verbs (agent-driven finish realized).
5. **Phase 4** — planner/retriever verbs, when those land.

Phases 2 and 3 are independent of each other (either can go first if priorities
shift); both depend only on Phase 0's tool-install plumbing being proven live.
