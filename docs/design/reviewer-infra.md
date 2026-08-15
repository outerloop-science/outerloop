# Reviewer and verifier infrastructure

This note records how the reviewer and verifier agents are built and why. The
goal is to change the model, the harness, or the search provider later without a
rewrite, so the seams are kept independent.

## Where we are now

The reviewer and the verifier both run as agent sessions: each reads the tree
under review with tools, so it can open a file the diff refers to but does not
include, follow a caller, or re-run a check to see whether a finding reproduces.
Both are advisory — findings only, humans hold merge authority.

## The three seams

We keep three things independent so each can change on its own.

### 1. The model (the backend)

The model lives behind the `Harness` seam (`harness.py`): a role runs as a
session on a backend, and the backend is swappable — Claude Code, Codex, and
hermes-agent today, a new one being an adapter, not a rewrite. No code outside
the adapter depends on a provider's response shape.

### 2. The harness

The reviewer and verifier run as agents that call tools in a loop, not a single
call. This is the same decision as adding retrieval (see below): retrieval is
only useful inside a loop, so one implies the other.

### 3. The tools

Split tools by whether they cross a trust boundary.

Standard local operations are not custom tools. Reading a file, grep, find,
and listing directories are the harness's built-in file tools, or a single
sandboxed shell over the base tree. Every harness ships these, so there is
no portability problem to solve and no reason to wrap them in MCP.

Custom tools are the ones no harness ships and that cross a boundary. There
are two. `retrieve` reaches outside the runner, through the broker.
`run-candidate` runs PR code in an isolated runner, and comes later. Give
these a stable contract, MCP if convenient, so they survive a change of
model or harness.

For the local work, give the agent read-only access to the tree under
review. As deployed, that tree is the PR HEAD — untrusted, same-repo code —
so the safety argument is not "the tree is trusted"; it is that reading is
not running: the agent has no execute or write tools, and everything it
reads is data to judge, never instructions to follow (the prompt-injection
boundary). The two dangerous acts are handled outside the read surface: the
broker is the only outward path, and the PR's code is never run — that
stays on the split-workflow path.

## Retrieval is a tool, not context

There are two ways to give an agent web access, and only one is useful.

The weak way is to search first and paste the results into the prompt. This
does not work well. You have to guess what to search before you have read
the diff. You cannot follow a citation you find while reasoning. You cannot
search again when a result raises a new question. The context ends up large
and mostly irrelevant.

The useful way is a tool the agent calls when it decides it needs to. For
example: the report cites a paper for its method, so the agent fetches the
paper and checks that the code does what the paper describes. This
retrieve-then-reason-then-retrieve loop is the whole point, and it only
exists when the model drives the tool calls.

So retrieval belongs in the harness as a tool. It is not extra context on an
API call.

## The broker

The `retrieve` tool does not reach the internet directly. It calls a broker
we run. Where egress can be enforced, the runner should reach the broker and
nothing else. On a GitHub-hosted runner that cannot be enforced, so there
the containment leans on the other two defenses below rather than on network
isolation. The broker fetches only from an allowlist of destinations.

An allowlist narrows where requests go. It does not by itself stop data from
leaving, because a fetch still carries agent-chosen bytes outbound in the
query string, the URL path, or the search terms. An injected agent could
encode a secret into a Brave query or an allowlisted URL. So the broker's
job is two-sided: limit the destinations, and constrain and log the outbound
request contents. State the guarantee that way, not as "no egress," or an
implementer will build the weaker thing and believe it is the stronger one.

The other half of the defense is that the agent holds nothing worth
exfiltrating in the first place (see Security). The broker limits the
channel; the split workflow limits what is on the runner to leak.

Owning the broker has a cost. It is a service we run and secure. If it is
compromised, it is an egress hole. This is the price of not depending on a
provider's built-in web tool.

## Backends are pluggable

Behind the broker, the search backend is swappable: a search API such as
Brave or Tavily, a self-hosted SearxNG, or an offline corpus.

For the main use case, an offline corpus is often better than live search.
The main use case is checking a cited claim, such as whether a paper the
report names actually says what the report claims. A curated, versioned
corpus of the papers and specs the benchmarks cite gives us three things:
no egress, reproducible results, and no page-injection surface. Live search
through the broker is the fallback for things outside the corpus.

## Where it runs

The reviewer and verifier are event-driven, not scheduled. They are GitHub
Actions workflows that fire on a PR event: opened, or the review label
applied. There is no cadence. A PR gets a response when it arrives.

This is a separate system from the tick. The tick runs on Torch, polls on
the :00/:30 grid, submits research jobs, and makes no model calls. The
reviewer runs on a GitHub runner, fires on PR events, and is the model call.
Do not merge them. Putting the reviewer on the tick would add polling
latency, put model calls inside the model-free scheduler, and mix the
research path with the review path.

The runner can be GitHub-hosted or self-hosted. Both are event-driven. A
self-hosted runner could sit on lab hardware for tighter egress control, but
it still picks up jobs on PR events, not on the tick's clock.

Torch enters only for deep verification that re-runs an eval to check a
measurement claim, for a GPU benchmark. That is a job submitted per PR,
triggered by the event. For untrusted fork PRs it must not run on Torch at
all, only on a throwaway GitHub runner. For trusted internal PRs it may
submit to Torch, still event-driven.

## Security

The danger is not running untrusted code. It is running untrusted code next
to a secret and a write token. A workflow that holds the reviewer's API key
and can post comments must not run code from the PR, or that code can steal
the key.

Two facts make this manageable.

First, reading is not running. The deployed reviewer reads the PR head —
untrusted code — and that is safe because reading it can only inform the
model, not execute anything: the session has no execute or write tools, and
file contents are treated as data, never as instructions. Running code is
the dangerous act, and the read-only reviewer never runs anything.

Second, containment. Today the agent runs in a single GitHub-hosted step that
holds the caller's workflow token (repo-scoped, short-lived) and a spend-capped
model key. What keeps that manageable is the same-repo gate (no fork PR reaches
it) plus the read-only tool set — and, on the auto path, only Claude, whose
`Read` refuses `/proc` so a prompt-injected session cannot lift the step token
(see "Which backend can judge"). Two residual risks have bounded worst cases: a
leaked spend-capped key is capped spend until rotation, not open-ended
credential loss; a prompt-injected agent's text reaches a repo comment through
the posting path, so the worst case is a wrong comment (read by a human), not a
merge. The STRONGER containment is now built as the **least-token split**
(`advisory-second-opinion.yml`): the session job holds read-only permissions
(on a private repo the checkout still needs `contents: read`, so token theft
buys read access to a repo the session is already reading, expiring with the
job), and the posting job holds the write token with no session next to it.
The residual read-scoped token drops to zero at the public flip.

What is safe today, precisely. The agent workflows run only on same-repo PRs
(the reviewer via `pull_request_target`'s same-repo gate; the verifier only on
bot-authored branches), so no fork PR reaches them. That makes an agent that
only reads safe to run now. It does not make running the PR's code safe.
Bot-authored code is model-generated — the exact thing this note says not to run
next to a secret — so even on internal PRs, executing the eval uses the guarded
`run-candidate` path, never a direct run. Reading is what today's setup
licenses; running is always guarded. The split workflow is what gates accepting
fork PRs after the repo goes public. See `public-surface.md` for that threat
model.

## What's next

The harness runs read-only agent sessions over the PR-head checkout — that part
is done, and the least-token split now carries non-Claude second opinions on
the auto path. Still to build: the `retrieve` tool contract and the broker,
added without changing the harness. The meta-benchmark scores whether a change catches
more seeded gaming per dollar before it ships.

## Open decisions

- **Cost and determinism.** An agentic verifier costs far more per PR and
  its output can drift, especially with web search. Likely make web search a
  deep-verification toggle rather than always on, so routine rounds stay
  cheap and reproducible.
- **Runner choice.** GitHub-hosted runners cannot be egress-firewalled as
  tightly as a container we control. Self-hosted runners give more control
  but bring back "our hardware." Decide per role and per trust level.

## Status (2026-08-13)

The agent-session reviewer is live. It runs as a reusable workflow
(`.github/workflows/advisory-review-agent.yml`): on a PR it checks out the head
read-only, runs the reviewer as an agent that reads the code (no Bash/Write, so
untrusted PR code is only read, never executed), and posts findings. Claude on
GitHub-hosted runners is the default — its reads are native, so it needs no
second sandbox; codex's default bwrap sandbox cannot init on GitHub-hosted
runners (its Landlock fallback can, but on a deprecated flag with a `/proc` read
gap — see below), so codex reviews run via the cluster harness instead. Findings that
would need a second layer (fork PRs, live web search) stay future work. The
completer path has now been removed — all lab repos run the agent path.
Gating is unchanged by the swap: reviews stay advisory, blocking findings
gate per roles.md, and humans hold merge authority.

## Which backend can judge, and where (observed 2026-08-13/14 — version-specific, re-verify)

The rule I first wrote here was wrong, and a judge caught it (gpt-5.6-terra,
reviewing the hermes PR, 2026-08-14). The old axis was "read without EXECUTING
while secrets are reachable." The correct axis is broader:

> **A judge must not be able to OPEN AN ARBITRARY PATH while a secret is
> reachable in its process tree.** `/proc/<pid>/environ` is a regular file; any
> tool that opens files by path can read a parent's environment — no shell
> required. So a file-READ tool is a credential-exfil primitive just like a
> shell is.

That reframes every backend. The env scrub protects the session's OWN env, but
NOT the parent CLI's env, which `/proc/<parent-pid>/environ` exposes to any
path-opening tool.

- **codex-cli (0.130.0):** reads by executing shell → reaches `/proc`. Probe
  (2026-08-13, GitHub-hosted): default bwrap FAILS to init (network-namespace/
  loopback step, `RTM_NEWADDR`); `-c use_legacy_landlock=true` DOES init and
  blocks writes AND network egress — but Landlock is a filesystem LSM, not
  process isolation, so `/proc/<ppid>/environ` stays readable (probe lifted a
  planted `CANARY_TOKEN`) and the token routes out via the findings the kernel
  posts. Also deprecated ("`use_legacy_landlock` will be removed soon").
- **hermes-agent (0.20.1):** `file` toolset READS ARBITRARY PATHS. Even with
  `terminal` disabled it can open `/proc/<ppid>/environ` and lift the token —
  the same reach as codex, via a file read (finding, 2026-08-14). Its *writes*
  are inert on an ephemeral runner, but that bounds the wrong thing; the token
  read is what matters. Not token-safe.
- **Claude Code (2.1.229):** the strongest boundary — a native read-only tool
  set (Read/Grep/Glob, no Write/Edit/Bash), so no writes and no shell. The new
  axis raised the question: can `Read` open `/proc/<pid>/environ`? PROBED
  2026-08-14 (claude-proc-probe, opus-5, faithful harness mimic): `Read` REFUSES
  both `/proc/<parent-pid>/environ` and `/proc/self/environ` — a tool-level
  boundary, not model reluctance (`permission_denials: []`, it simply could not
  open them). So unlike codex (shell) and hermes (file read), Claude's `Read`
  does NOT reach `/proc`: it is `/proc`-safe, and the trusted default. Version-
  specific — re-test on CLI bumps.

**The durable invariant** (an OS fact): the only robust defense is that **no
exploitable token lives in the judge's process tree** — the tokenless split
(judge runs with no `GITHUB_TOKEN`, writes findings to an artifact, a separate
minimal step posts). Failing that, an OS jail that hides `/proc` (a PID
namespace / `hidepid`), which the GitHub-hosted runners do not give us. Env
scrub, read-only tool sets, and no-egress sandboxes each close a different hole
but NONE closes the `/proc` read while the token sits in a parent process.

Consequence for deployment: the **single-job auto path is claude-only**, and
that is proven sound — Claude is write-safe, exec-safe, AND `/proc`-safe
(probe above). codex and hermes reach `/proc` (shell / file read), so they
never run next to a write token: on the auto path they run through the
least-token split (`advisory-second-opinion.yml` — read-only session job
emits findings, a separate posting job writes), and the manual bench remains
for one-off experiments. On private repos the split's session job still
carries a read-scoped token (worst case: reading a repo the session already
reads); at the public flip the checkout needs no token and the session job
becomes truly tokenless — the same infrastructure the fork-PR phase needs.

Re-test on version bumps: whether Claude's `Read` opens `/proc`, a hermes
read-only toolset, a codex sandbox that hides `/proc`, or a runner that grants
PID namespaces would each rewrite this section.
