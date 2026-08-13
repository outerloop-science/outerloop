# Reviewer and verifier infrastructure

This note records how we intend to build the reviewer and verifier agents.
The goal is to be able to change the model, the harness, or the search
provider later without a rewrite. It was written before we built, so the
seams were agreed first. The REVIEWER half is now deployed — see "Status"
at the end; the sections between record the design and the verifier's
current shape.

## Where we are now

The reviewer is now an agent session (see Status). The verifier is still
one API call: we gather a fixed bundle of context (the diff, the contract,
the eval modules, the tests, the PR thread), send it in a single request,
and post the reply. This is cheap, fast, and safe: the model runs no tools
and executes nothing.

The one-call shape has one limit. A single call cannot explore the code.
It cannot open a file the diff refers to but does not include, follow a
caller, or run the tests to check that a finding reproduces. On the pilot
this was fine. On a larger target it misses things — which is why the
reviewer moved to an agent session first.

## The three seams

We keep three things independent so each can change on its own.

### 1. The model

The reviewer already talks to a `Completer` interface. `AnthropicCompleter`
is one implementation. Swapping the model is a config change, not a rewrite:
Claude on Bedrock, Vertex, or Foundry, an OpenAI-compatible endpoint, or a
local model all fit behind the same interface. Keep it that way. No code
outside the completer should depend on Anthropic-specific response shapes.

### 2. The harness

The reviewer should become an agent that calls tools in a loop, not a
single call. This is the same decision as adding retrieval (see below):
retrieval is only useful inside a loop, so we cannot have one without the
other.

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

Second, the split workflow keeps the blast radius small, not zero. The agent
runs sandboxed with no write token and no secret beyond a spend-capped API
key. It writes findings to an artifact, and a separate minimal step with the
write token posts them. Two residual risks remain, and each has a bounded
worst case. If the spend-capped key leaks, the worst case is capped spend
until we rotate it, not an open-ended credential loss. If the agent is
prompt-injected, the injected text reaches a repo comment through the
posting step, so the worst case there is a wrong comment, not a merge. The
posting step must treat the artifact as data, not instructions, and findings
are read by a human.

What is safe today, precisely. `verify.yml` runs only on same-repo,
bot-authored branches, so no fork PR reaches the reviewer. That makes an
agentic reviewer that only reads safe to run now. It does not make running
the PR's code safe. Bot-authored code is model-generated, which is the exact
thing this note says not to run next to a secret. So even on internal PRs,
executing the eval uses the guarded `run-candidate` path, never a direct
run. Reading is what today's setup licenses; running is always guarded. The
sandbox and split-workflow work is what gates accepting fork PRs after the
repo goes public. See `public-surface.md` for that threat model.

## Sequencing

1. Stand up the harness with a sandboxed, read-only shell over the base
   tree. This needs no broker and no custom tools, because reading the
   trusted base is safe.
2. Define the `retrieve` tool contract and the broker, and add them without
   changing the harness.
3. Before making the agentic reviewer the default, score it against the
   single-pass version on the meta-benchmark. The metric is whether it
   catches more seeded gaming per dollar. "Feels better" and "is better,
   safely" can differ, so measure it.

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
second sandbox; codex's read-only sandbox (bwrap) cannot init on GitHub-hosted
runners, so codex reviews run via the cluster harness instead. Findings that
would need a second layer (fork PRs, live web search) stay future work. The
completer path is retained until the lab repos migrate, then removed.
Gating is unchanged by the swap: reviews stay advisory, blocking findings
gate per roles.md, and humans hold merge authority.

## Which backend can judge, and where (observed 2026-08-13 — version-specific, re-verify)

The rule that survives version bumps: **a judge must read the tree without
executing arbitrary code while secrets are reachable.** Two ways to meet it —
native reads, or execution inside a real jail. The per-backend facts below are
EMPIRICAL and dated; treat them as observations to re-test, not fixed truths.

- **Claude Code (2.1.229):** native reads (Read/Grep/Glob), write droppable →
  read-only with no execution. Judges on any runner, including GitHub-hosted.
  The trusted default.
- **hermes-agent (0.20.1):** native reads too (`file` toolset, `terminal`
  disabled) → no execution, so a GitHub-hosted run is low-harm (its bundled
  `write_file` is inert in an ephemeral container: no push, no committed
  output, scrubbed env). But it has no mechanical read-only boundary — `file`
  bundles write with read, and `approvals.deny` gates shell commands, not the
  write tool. So: safe to EXPERIMENT with as a judge on GitHub-hosted, not
  TRUSTED until an upstream read/write toolset split or an OS read-only
  bind-mount.
- **codex-cli (0.130.0):** reads by executing shell, so it needs a jail. Its
  default `--sandbox read-only` FAILED to init on GitHub-hosted runners — at the
  network-namespace (loopback) step, not the filesystem step. NOT tested, and
  the open levers: a sandbox config that skips the network namespace
  (`-c sandbox_permissions=...`; a judge wants no egress anyway), or codex's
  `--dangerously-bypass-approvals-and-sandbox` (shipped FOR externally-sandboxed
  environments) inside a read-only bind-mount. So "codex can't judge on
  GitHub-hosted" is an observation of the default, not a proven limit — verify
  the config space before relying on either conclusion. Codex judging on the
  cluster (apptainer, where its sandbox inits) is proven.

**The durable invariant** (an OS fact, not a tool version): run an executing
session unsandboxed only inside a STRONG external jail (read-only bind-mount +
no egress). A weak jail is not enough — the env scrub protects the session's
OWN env, but a same-uid executing child can read the parent CLI's
`GITHUB_TOKEN` via `/proc/<ppid>/environ`. Non-executing backends (Claude,
Hermes-file) cannot do this, which is why they are safe on any runner.

Re-test on version bumps: a codex sandbox backend that inits on GitHub-hosted,
a hermes read-only toolset, or a GitHub runner that grants namespaces would each
rewrite this section.
