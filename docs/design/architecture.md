# Architecture

Decisions confirmed 2026-08-05; revised the same day after an ops and a safety
review.

## Purpose

A background agent that co-develops the lab's benchmark-bearing repos (jepa-agent,
egolearn): picks work from their roadmaps and benchmark gaps, implements on a
branch, runs GPU experiments, opens a PR when a metric improves, reviews PRs, and
reports weekly. Humans keep the merge button.

## Decisions

| Axis | Decision |
| --- | --- |
| Compute | Torch-first: GPU jobs via sbatch under a sponsoring account, tagged, low-priority, hard GPU-hour budgets. Cloud burst deferred. |
| Agent/LLM | Pilot on ONE subscription harness (Claude Code or Codex — whichever passes the headless-auth spike first); first-party API alongside (tiered models, hard $ caps). **Provider-diverse by design, Anthropic as the pilot**: every LLM touchpoint sits behind a seam (`Completer` for reviews, `Harness` for sessions) so a second provider is a new implementation, not a rewrite. Third-party aggregators, self-hosted: deferred. |
| GitHub | Org machine user (bot) with fine-grained access to opted-in repos + a contract file per repo. No GitHub App for now. |
| Scheduling | Self-resubmitting sbatch chain on Torch (scrontab is disabled there); nothing SSHes in — all connections outbound. Reviewer role on GitHub Actions. |

## Target-repo contract

A repo opts in with two things: bot access, and `.autoresearch.yaml` at its root.
Target repos never import this repo.

```yaml
benchmarks:
  - name: pusht-planning
    command: uv run python -m jepa_agent.eval --env pusht
    metric: success_rate        # higher is better
budgets: { gpu_hours_per_run: 8, runs_per_week: 10 }
scope:
  allowed: [src/, tests/, projects/]
roadmap: docs/roadmap.md
```

The schema lives here (`autoresearch.contract`). The loader hard-codes invariants
no YAML can override: `.github/`, the contract file itself, and the roadmap are
always forbidden write paths; the contract is read from the default branch only;
**autoresearch is never a valid target of itself**.

## Roles

1. **Advisory reviewer** — comments on PRs in opted-in repos. Constraints: fixed
   "advisory, not an approval" header; never comments on bot-authored PRs; one
   review thread per PR; maintainers can opt a PR out by label. Ships first; no
   GPU needed.
2. **Benchmark climber** — the research loop below.
3. **Maintenance** — CI fixes, issue triage. Later.

## Scheduling and connectivity

2FA is handled by direction: **all connections are outbound**; recovery that
needs inbound access is a human with the runbook.

- **The chain**: each tick keeps two successors queued (`--dependency=singleton`
  + absolute `--begin` times on a fixed cadence grid, so the schedule doesn't
  drift), submitted with retry/backoff. One failed submit doesn't kill the loop;
  two consecutive failures leave the watchdog alert as the signal.
- **Tick order**: (1) read the pause sentinel on the state branch — if set, exit
  *without resubmitting*; (2) acquire the lease by compare-and-swap commit to the
  state branch — a non-fast-forward push means another tick is alive: exit;
  (3) write heartbeat with lease expiry; (4) work — sessions under a hard timeout
  safely below the partition walltime; (5) refresh heartbeat, exit. Stale leases
  are reaped by the next tick.
- **One state store**: an unprotected `state` branch on the notebook repo (task
  queue, leases, sentinel, run ledger). The bot already has write there; direct
  pushes give the lease its compare-and-swap; the notebook's `main` stays
  PR-gated. The bot holds read-only on this repo (deploy pulls only — never
  write); target-repo grants come at the phase that needs them (the reviewer
  uses the target repo's own Actions `GITHUB_TOKEN`; the first target Write
  grant comes with the phase-5 opt-in). Issues and PRs are for humans —
  reports, digests, alerts.
- **Watchdog**: a scheduled GitHub Action; alert-only (it cannot reach Torch) —
  it opens an issue and emails on stale heartbeat. Restart is a human action;
  the lease makes restarts safe, the runbook makes them fast.
- **GPU jobs**: submitted `--nice`/requeueable so humans always win the queue at
  deadlines; the pause sentinel doubles as a deadline blackout switch.
- **Deployment = pull at tick start.** The batch script is a minimal, stable
  shim ordered so updates can never kill the chain: submit successors first
  (with the already-queued shim), then `git reset --hard origin/main` +
  `uv sync --locked` in the deploy clone (Torch home dir), then exec the
  orchestrator from the fresh checkout. Merges to main are live at the next
  tick; a bad merge crashes the tick but not the chain — heartbeat records it,
  the fix is a revert PR. Slurm spools batch scripts at submission, so shim
  changes propagate one tick-generation later. The pull authenticates with the
  bot PAT: the bot holds a **Read** collaborator role on this repo, so the
  intersection rule makes the same token read-only here while R/W on its
  targets — no extra deploy credential. Write stays impossible at the account
  layer (read denial would be theater anyway: sessions share the filesystem
  with the deploy clone, and this repo holds no secrets). Deploying from a
  pinned tag instead of main: deferred unless bad merges reach the loop.

## Task granularity

A task is **one hypothesis, one PR** — but its evaluation scope follows the
target's artifact structure, declared in the contract:

- *Independent solvers* (the pilot): scope = a single benchmark; one PR moves
  one metric; solvers never share code (duplication is fine in throwaway code).
- *Unified benchmark* (jepa-agent): the artifact is one agent evaluated across
  a suite, so a change to the shared artifact (world model, planner, memory) is
  evaluated on the **full suite** and reported as the per-env breakdown plus the
  contract's aggregate — improvements and regressions both. No cherry-picking;
  contracts may declare suite aggregates (e.g. mean success rate) and
  no-regression floors.

What stays absolute regardless of scope: one hypothesis per PR (attributable
diffs), full-scope reporting, and **cross-target separation** — separate clones,
branches, budgets, and report streams per target (`runs/<target>/`,
`lessons/<target>.md`), never cross-target code reuse. The only global artifact
is this machinery; the only cross-project channel is process lessons in the
notebook. Promotion of agent-written code into a target's shared library is a
human-gated proposal like anyone else's, never implicit.

## The research loop

```
tick → pick task (benchmark gap / roadmap item / approved issue)
→ pin target-repo SHA + contract hash for the task
→ agent session in a fresh clone → implement on feat/auto/<slug>
→ CPU smoke test → sbatch GPU job(s) → poll on later ticks
   (re-validate contract hash each poll; mismatch invalidates the task)
→ analyze results, iterate (bounded retries)
→ metric improved? → baseline re-run at the merge-base, not a trusted static file
→ open PR: diff + results table + research report
→ human code owner reviews → agent addresses comments
→ weekly digest issue: tried / worked / cost
```

## Research reports and the notebook

Every run — success or failure — ends with a short, readable report: hypothesis,
what was tried, outcome, major takeaways, proposed next steps. Not just numbers.
Success reports go in the PR body for the human reviewing it; the canonical copy
of every report lands in the **notebook**.

The notebook is a separate, **permanently private** repo
(`autoresearch-notebook`) — the agent's lab notebook, plain markdown, readable by
humans and greppable by the agent:

- `runs/<target>/<date>-<slug>.md` — every run's report, failures included
- `lessons/<target>.md` — distilled lessons, bounded size; a periodic
  distillation pass compresses raw reports into it (humans edit freely)
- `plans/<target>/` — hypothesis backlog, experiment plans, roadmap proposals
  (changes to a target's actual roadmap remain human-gated PRs on the target)
- `digests/<year-week>.md` — weekly digest sources

Task selection reads `lessons/` plus recent `runs/` — the loop's research
memory. The agent writes via PRs that **merge without human approval**
(auto-merge once the secret-scan check is green; no required reviews) — prose,
not code, so the human review gate protects executable things while the PR
history keeps notebook changes visible, atomic, and revertable. It is separate
from both the state branch (which stays purely operational: queue, leases,
sentinel, ledger) and this repo, because it aggregates unpublished-research
insights across all targets and must never be part of any public flip. Raw
metrics stay in W&B; the notebook links to runs. No database or vector store —
grep + recency + distillation until that provably fails.

## Components (`src/autoresearch/`)

| Module | Job |
| --- | --- |
| `contract` | Schema + loader for `.autoresearch.yaml`, incl. the hard-coded invariants |
| `harness` | Run one agent session in a scrubbed environment (no PAT, no billing keys); capture transcript, diff, cost; secret-scan transcripts before storage. See "Harness and context engineering" below |
| `orchestrator` | Tick logic: sentinel, lease, task selection, session dispatch, state sync |
| `compute` | sbatch/squeue submit-and-poll behind one interface |
| `github` | Bot auth and push (orchestrator-side, after sessions end), PR/issue ops |
| `budget` | Hard caps: $ per run, weekly $, GPU-hours, PRs per week; subscription backends metered by a session/token proxy |
| `report` | Per-run research reports (takeaways + next steps), notebook writes + distillation, weekly digests, cost ledger, leaderboard history |

## Harness and context engineering

The harness is where the research bet lives: two agents with the same tools and
budgets are separated almost entirely by **what they see at session start and
what survives between sessions**. So context assembly is a first-class,
versioned, unit-tested artifact — not prompt strings scattered through
orchestration code.

**The session brief.** Every session starts from a `SessionBrief` built by a
pure function (`brief.build(...)`) from typed inputs, so tests can assert on
exactly what any given agent saw, and a bad run can be replayed from its brief:

| Section | Source | Budgeted |
| --- | --- | --- |
| Task: one hypothesis, expected metric movement, done-criteria | task selection | fixed |
| Contract: benchmarks, scope, budgets (verbatim) | `.autoresearch.yaml` | fixed |
| Ruler: how the metric is computed, how claims get re-verified | target repo docs | fixed |
| Lessons: distilled, bounded per-target lessons file | notebook `lessons/<target>.md` | hard cap |
| Recent history: last N run reports for this target (incl. failures) | notebook `runs/<target>/` | hard cap, newest first |
| Budget state: remaining GPU-hours/$/PRs this week | `budget` | fixed |

Everything else is deliberately absent: no other targets' data (cross-target
separation), no raw transcripts (distillation instead), no maintainer-private
text. Each brief is stored alongside the run report, so "why did the agent do
that?" is always answerable, and brief-construction changes are diffable
experiments in their own right — the knob we expect to tune most.

**The backend seam.** `Harness` is one method: take a `SessionBrief` and a
workspace, return a `SessionResult` (diff produced, transcript path, cost,
stop reason). Backends are adapters: subscription CLIs (Claude Code first,
Codex next), first-party APIs, whatever comes later. Nothing provider-specific
crosses the seam in either direction — provider quirks (auth, retries, token
accounting) live inside the adapter; context policy lives in the brief builder,
shared by all backends. This is also what makes backend comparisons honest:
same brief, same task pool, different backend, diff the outcomes.

## Threat model

- **Untrusted text.** Task sources are attacker-writable once target repos are
  public. Only issues/comments authored by org members, or carrying a
  maintainer-applied `autoresearch:approved` label, become tasks; all other text
  is data, summarized in a no-tools context, never instructions.
- **Credential theft.** Sessions never see the bot PAT or billing keys: the
  environment is scrubbed, and pushes happen orchestrator-side after the session
  ends. Transcripts are secret-scanned before storage.
- **Self-modification.** The contract-loader invariants above: never
  self-targeting; contract, roadmap, and `.github/` unwritable everywhere.
- **Reviewer influence.** The advisory constraints above prevent the pipeline
  from nudging humans to approve its own work.
- **Standing rules.** The bot never merges and is never a code owner; target
  branch protection applies to it unmodified. Budgets are enforced in code —
  hitting a cap kills the run. PR rate cap protects human review bandwidth.
- **Kill switches**, fastest first: (1) set the pause sentinel — anyone with
  write access, no cluster needed; the chain self-terminates at the next tick;
  (2) suspend the bot account; (3) runbook: `scancel` the queued chain and GPU
  jobs (needs 2FA login).

## Secrets and confidentiality

- **Bot account access**: direct per-repo collaborator grants (Write) on exactly
  the opted-in repos — **never via team membership** (teams carry maintain,
  receive review requests, and inherit future grants invisibly). The collaborator
  list is the opt-in list.
- **Bot PAT**: fine-grained; repos enumerated to the opted-in set; permissions
  contents + pull-requests + issues only — **no workflow permission**; 90-day
  expiry with a rotation reminder in the ops calendar. Effective access is the
  intersection of account grants and token scope — both stay minimal.
- **Keys**: orchestrator env/0600 files in the cluster home dir. Exception, by
  design: the reviewer role holds its own separate, spend-capped API key in
  GitHub Actions secrets, revocable independently; fork PRs run without secrets.
  The reviewer's GitHub operations use the workflow's default `GITHUB_TOKEN` —
  the bot PAT is not involved in that role.
- **Transcripts**: contain target-repo code — same confidentiality as the code.
  Secret-scanned, then stored on project space (not scratch — 60-day purge) with
  a stated retention period; durable per-run metadata lives in the ledger so
  audits survive storage cleanup.

## Cluster facts (verified 2026-08-05)

- [x] scrontab disabled → the chain is the scheduler; `sbatch --begin` verified
- [x] Outbound HTTPS from login and compute nodes to GitHub and both LLM APIs
- [x] Lab Slurm accounts live; the short-job CPU partition allocated in seconds;
      Slurm 25.05
- [x] Login nodes are ephemeral Kubernetes pods — never park a daemon on one;
      the chain lives in the Slurm queue
- [ ] Headless CLI auth on a compute node (the phase-3 gating spike)
- [ ] Bot git push over HTTPS with fine-grained PAT (needs the bot account)

Account names, partitions, and hostnames stay in the lab wiki and untracked ops
notes, not in this repo.

## Deferred by default

- The second harness backend (pilot one; add the other when the pilot's volume
  data says it's worth it)
- GitHub App (revisit if PAT limits bite)
- Cloud compute backend (burst valve when Torch queues block)
- Third-party / self-hosted models
- Any form of auto-merge on code — never. The sole exception is the prose-only
  notebook repo.
