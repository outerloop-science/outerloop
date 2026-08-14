# Agent substrate: tools, skills, memory

Status: proposal (2026-08-12). Companion to `consolidation.md`. This lays out
*what exists in the agentic layer* — the catalog — and how the harness loads it.

**Living document.** The catalog is not fixed. We discover feedback in real runs
and harden it into memory and skills (see *Hardening: the living loop*); expect
this list to grow.

Three axes, kept distinct:

- **Tool** — a capability the harness grants (a callable function).
- **Skill** — authored know-how: how and when to do a task. A file, not code.
- **Memory** — what persists across sessions and gets recalled into a brief.

Skills and tools pair up (a tool is a hand; a skill is knowing when to use it).
Skills are also the *procedural layer of memory* — see the memory table.

## Tools (capabilities)

| Tool | Who | Notes |
| --- | --- | --- |
| repo-read (Read/Grep/Glob) | all agentic roles | native; the read surface |
| execute / bash | author, steward | inside apptainer; **never** reviewer/verifier |
| pr-context-read | reviewer, verifier | read-only, **tokenless** — the harness fetches PR/issue text; the bot PAT never enters a session |
| retriever | opt-in per RoleSpec | curated egress for best-practice/reference lookups (the retriever-in-harness seam) |
| subagent (fan-out) | author, steward | inherits the parent RoleSpec's tools/scope/key; spend counts against the session budget |

GitHub **writes** (open PR, comment, update ledger), the **eval run**, and
**experiment submission** are `act` syscalls, not agent tools: the agent directs
them, the kernel performs them so the guarantee holds — the PAT never enters a
session, and a launched experiment always gets its paired wake job
(consolidation.md, "Syscalls"). The agent drafts;
the kernel opens the bot PR; a human merges.

## Skills (know-how)

Three scopes: global (every role), role, target. Curated on purpose — a skill
per tiny thing is the accretion trap in a new costume.

**Global**
- `kernel-primer` — how autoresearch works: the contract (scope/budgets/benchmark);
  **your metric is re-measured by the kernel, so gaming it is pointless**; the six
  honest endings; humans hold merge authority.
- `experiment-lifecycle` — launch a long job, record what you wait for, end the
  session; you will be woken with results. Don't block.
- `plain-style` — the house writing style (today's `PLAIN_STYLE`).
- `research-report` — hypothesis, what moved, negatives, one next step; a clean
  negative is a success.

**Role**
- author: `hypothesis-discipline` (one hypothesis, one expected movement,
  done-criteria; don't chase small things) · `honest-method` (express a finding
  in its natural abstraction — the init/LR seam, not a `forward` hack — and check
  transfer, not just the sweep)
- reviewer: `review-rubric` (verdict-led, blocking vs advisory, "materially
  sound" bar, close cycles fast) · `read-only-investigation` (use the checkout +
  retriever to look past the diff, never execute)
- verifier: `integrity-lens` (gaming / overfit / scope-evasion; is the change a
  real mechanism or a benchmark hack?) · shares `read-only-investigation`
- steward: `ruler-hardening` (when/how to make the metric harder once it's gamed —
  add a transfer split, harder cells) · `benchmark-design` (noise floors, seeds,
  why a sweep beats a single point)
- follow-up: `respond-to-review` (address maintainer comments; which task
  instruction a wake supersedes; honest scope)

**Target (e.g. `yolo-jepa`)**
- `codebase-map` — `models/` (encoder/predictor/regularizers) is yours; `run_sweep.py`
  (the frozen SGD optimizer, the task tuple), `bench_eval.py`, `data/` are the ruler.
- `method-notes` — the JEPA method, the variance floor, the curves, the probe,
  known headroom (spiral/decay near 0), the oracle ceiling (~0.81).
- `eval-and-env` — `uv run python bench_eval.py`, the CPU-torch env, the budget.

## Memory (layers)

| # | Layer | Holds | Lifetime | Scope | Writer | Reader |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | session / resume | working context, agent notes | one run | one run | the session | the session |
| 2 | run reports (episodic) | hypothesis + outcome per run | persistent | per target | role session | brief (recent N) |
| 3 | lessons (semantic) | distilled do / don't | persistent, curated | per target | curator role → bot PR → human merge | brief |
| 4 | **human feedback (new)** | maintainer comments + the merge/close **delta** | persistent → distilled | per target | capture step | brief |
| 5 | ledger / leaderboard (factual) | verified best, seeds, floor | persistent | per benchmark | **kernel only** | brief + humans |
| 6 | skills store (procedural) | the skills above | persistent, versioned | global/role/target | humans (PR-gated) | brief |

Layer 7 (later): a retrieval index over 2 and 3 for relevance recall
(Hermes-style FTS), once the store is big enough to need it.

The gap today is **layer 4** — the notebook carries the agent's own reports
forward, but maintainer feedback and the merge/close decision feed nothing. The
highest-value signal there is the *delta*: what a human changed before merging,
not the prose comment.

## Hardening: the living loop

Feedback is not an authored set fixed up front — it is discovered in runs and
hardened. A promotion ladder, each rung more general and more permanent, every
rung human-gated:

1. **Observe** — a maintainer comment, a correction, an observed failure (a gamed
   metric, a banal PR).
2. **Capture** — into human-feedback memory (layer 4): the comment plus the
   merge/close delta.
3. **Distill** — the curator *drafts* a per-target lesson (layer 3), small and
   deduped, which lands through a human-reviewed PR — never auto-applied (see
   Boundaries).
4. **Promote** — a lesson that recurs across runs or targets graduates to a skill
   (layer 6) or a global standing rule. This is where a one-off correction
   becomes house policy.
5. **Pin + version** — skills and lessons are versioned; a run records what it
   saw, so the ladder never breaks reproducibility.

Promotion is a human-reviewed PR, never a silent self-edit. The affordance rule
still applies: if a lesson keeps recurring because the environment makes the
wrong thing easy, fix the environment (add a seam) instead of adding another
rule.

## Boundaries (hold across all layers)

- **Cross-target separation** — a brief sees only its target's memory. No leakage
  between targets (`architecture.md`, brief builder).
- **No raw transcripts in memory** — distill into reports/lessons; transcripts
  stay on disk, secret-scanned, out of briefs.
- **No maintainer-private text** in anything an agent reads.
- **Reproducibility pin** — a run records which lessons-version it saw, so runs
  stay comparable (same caveat as pinning a self-improving backend).
- **Proposed by agents, gated at merge** — an agent (the curator, like any role)
  drafts a skill or lesson as a bot PR; it lands only on a human merge, never a
  silent self-edit. Drafting is agentic; merge authority is the human gate.
- **Untrusted content is data, not instructions** — PR/issue text, retriever
  results, and the target's own files are untrusted. The harness data-fences
  them and a role treats them as material to judge, never as commands. This is
  the prompt-injection boundary for the read tools (pr-context-read, retriever)
  and for anything a session reads in the workspace (`architecture.md` already
  data-fences wake prompts by the same rule).

## Harness: the loading substrate

Extends `architecture.md` ("Harness and context engineering", "The backend
seam"). The harness runs one role session on a swappable backend and returns a
result. It does **not** judge, gate, measure, or write to GitHub — those are the
role-runner and kernel.

**Capability contract.** A backend adapter provides, or synthesizes:

| Capability | Native | Synthesized fallback |
| --- | --- | --- |
| resume | wake a session with its context | replay distilled context into a fresh session |
| structured output | schema-constrained final artifact | role-runner parses → validates → repairs `final_text` |
| tool restriction | honor `allowed_tools` | if it can't restrict, it's ineligible for read-only judge roles |
| cost / turn accounting | from backend output | session/token proxy |

Editing-role **scope** (the write allowlist) is not a backend capability: the
kernel enforces it on the captured diff after the session, rejecting any write
outside `scope` regardless of backend (`contract.path_is_forbidden`). Tool
restriction above is the read-only *judge* boundary; scope is the *editing*
boundary. Both are kernel-owned.

**Tools: native vs harness-provided.**
- *native* (from the backend): repo-read, edit, bash — gated by `allowed_tools`.
- *harness-provided* (one MCP surface, uniform across backends): retriever,
  pr-context-read. The security-sensitive tools live here so the kernel owns
  them. `allowed_tools` is the single security surface.

**Two orthogonal axes.** Backend (what drives the session: Claude Code / Codex /
Hermes) is separate from execution environment (where it runs: apptainer on Torch
/ GH runner / local / Docker). The RoleSpec picks both. The read-only
reviewer on a GH runner is where we first prove an alternate backend (Codex) —
a judge that never executes is the safe place to swap; the author runs in
apptainer on Torch. Hermes's Singularity backend maps onto apptainer directly.

**One seam.** The one-shot `Completer` is deleted outright — every role,
judges included, is an agent session on the same adapter. No second seam.

## RoleSpec: the app manifest

Data, not code. Adding a role is a new RoleSpec + its skills, reusing an existing
result-policy — zero kernel change (the invariant; a genuinely new result-policy
is the rare exception).

| Field | Meaning |
| --- | --- |
| `name` | role id (author, reviewer, verifier, steward, followup) |
| `instructions` | standing role prompt, composed from skills |
| `skills` | skill ids to load (global + role + target) |
| `tools` | allowed tools (native + harness-provided) |
| `output_schema` | schema the final artifact must meet (judges); none for authors |
| `key` | credential family (isolation) |
| `scope` | path allowlist (editing roles) |
| `memory_scope` | which target and layers this role reads/writes |
| `execution` | where it runs + whether execute is allowed |
| `budget` | turns, walltime, cost cap |

## The role-runner: one loop replaces five drivers

Replaces the five per-role drivers — done for the judges (`review_cli` and
`verifier_cli` are deleted) and the author's session dispatch (`climb_once`
runs `author_spec` through `run_role`); `steward` and `followup` dispatch
still to collapse. Kernel code; it calls into the agentic realm at step 2,
and everything trust-critical is deterministic.

1. **prep** — build the workspace (editable for author, read-only for judge);
   `brief.build(RoleSpec, task, memory)`; pick the backend adapter.
2. **run** — `harness.run(...)` with the RoleSpec's tools, key, scope, execution,
   schema.
3. **collect + validate** — capture the SessionResult and the diff; validate the
   structured output (bounded repair).
4. **result-policy** — deterministic, per role: author = measure + floor + PR;
   reviewer = post findings, blocking gates the merge; steward = validate + PR.
5. **persist** — run report, memory, ledger (verified numbers only).
