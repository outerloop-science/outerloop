# Agent protocols: what A2A and MCP would give Outerloop

Status: design note, September 2026, reviewed by a second model. A question
from the owner: the agent-to-agent protocols have settled since we designed
the lifecycle. Which of them fit, what would adoption cost, and what would it
buy?

The short answer. A2A's data model (a task with a state machine, messages
made of parts, "input required", artifacts) resembles what the kernel and an
author already do, closely enough to sketch a mapping but not closely enough
to call our design a profile of it. Its transport (JSON-RPC over HTTP) does
not fit Slurm, where nothing serves HTTP and messages are files. The
position this note proposes: the durable per-run inbox stays the one message
store; an A2A adapter that feeds it is a candidate for the first
service-shaped agent (one that runs as a service, with an endpoint of its
own, rather than as a batch job the kernel starts), and it earns its place
by that integration, not before.
MCP is settled for the agent-to-tool layer; where it fits here depends on a
retriever decision two existing notes disagree on. Nothing needs building
today.

## The landscape, as of this month

**A2A (Agent2Agent).** Started by Google in April 2025, hosted by the Linux
Foundation since June 2025, version 1.0 in March 2026 and 1.0.1 in May 2026.
Over 150 organizations back it, and it ships in Azure AI Foundry, Copilot
Studio, Amazon Bedrock AgentCore and Google Cloud. An agent publishes an
Agent Card (identity, skills, capabilities, security schemes, endpoint). A
client sends a Message; the agent may answer with a Message directly or with
a Task that moves through submitted, working, input-required, auth-required,
completed, failed, canceled and rejected. Messages carry Parts (text, file,
structured data) and Tasks carry Artifacts. Long-running work is polled or,
optionally, pushed to a webhook or streamed; a task that needs something goes
to input-required and the client sends the next message with the same task
id. Three bindings: JSON-RPC 2.0, gRPC and HTTP+JSON. Extensions, declared
by URI in the Agent Card, may add data, methods and transitions, but not new
task-state values. IBM's ACP joined A2A in August 2025.

**MCP (Model Context Protocol).** The agent-to-tool layer, spoken by every
harness we run (Claude Code, Codex, hermes; the last two also act as MCP
servers). The 2026-07-28 revision made the core stateless (no session
handshake; requests carry their capabilities), added multi-round-trip
requests so a server can ask the client for input without a persistent
connection, and moved long-running work into a Tasks extension (a durable
task id that survives client restarts, polling, input-required, cancel).
Servers expose tools, resources and prompts; the client owns consent.

**The rest.** AGNTCY (Cisco, under the Linux Foundation) covers agent
discovery, messaging and observability, with its Open Agent Schema Framework
describing agents. Agent Network Protocol and the on-chain identity efforts
target decentralized markets. A June 2026 analysis of the governance these
protocols can express finds voting and dissent preservation absent from all
of them and deliberation, escalation and audit only partial; the authors
argue that governance is "a missing architectural layer above current
interoperability standards." That is consistent with our own line, which
is stronger: protocols carry messages, never authority.

## Outerloop's seams

A protocol only matters at a boundary between two parties that could be
built by different people. Outerloop has five.

1. **Kernel to author session.** The kernel starts a harness session with a
   brief, the session works, stages requests through the syscall tool
   (launch, sleep, submit, reply, end), and ends its turn. The kernel runs the
   rigid steps (launch jobs, measure, publish) and wakes the session with the
   results as messages in its inbox. On Slurm the session is a batch job with
   no inbound network; the channel is files in the workspace and under the
   run directory.
2. **Kernel to judges.** The panel lenses and the verifier are sessions too,
   run by `run_role`, recording findings and a verdict through the same tool.
   Their output is evidence for the author and the kernel, never acceptance
   of the research.
3. **Kernel to humans.** GitHub: PRs, comments, reviews, checks. The inbox
   already carries these as messages, and the outbox posts replies.
4. **Kernel to compute.** Jobs on Slurm or the local pool. Not an agent
   boundary; no protocol applies.
5. **Kernel to other agents.** A planner assigning directions, an adopter's
   own agent as an author, a steward or maintainer, sub-agent teams inside a
   run. Today this seam does not exist; the lifecycle doc lists it under
   "Later".

## A tentative mapping onto A2A

Written down so the next design item can check itself against it, not as a
compatibility promise. Two choices had to be made that the resemblance does
not make for us: what a task is, and what an ending is.

A task spans a run, not a leg. An A2A task in input-required stays the same
task after the client answers, while our leg ends at every sleep. So the
whole run is one task: working while a session runs, input-required at every
park, terminal at the ending. Legs are turns inside it.

An ending is a completion with an outcome, not a task state. A negative
result is successful work; a rejected PR is a human's decision, not the
agent rejecting the task. Only kernel-side failure (stuck, aborted) maps to
failed or canceled. The six endings travel as data on the final message.

| Outerloop | A2A | Note |
| --- | --- | --- |
| a run | one task, in one context | legs are turns within it |
| `running` | working | |
| `parked` on jobs, on a submit, or on the next human message | input-required | the kernel is the client: its next message is the answer (launch results, the verdicts, a human's comment); the reason travels as data; a checkpoint timeout needs no answer and is a kernel-side wake |
| `ended` | completed (negative result, merged, rejected, budget exhausted) or failed/canceled (stuck, aborted) | the ending is metadata, not a state |
| inbox message kinds | structured-data parts of a client message | kinds are data inside a part, not new part types |
| `launch`, `submit`, `reply` | what the agent asks for in input-required | see the caveat below |
| the report, the PR, the line snapshot | artifacts | |
| budgets and the meter | kernel-owned, delivered as data | A2A has no budgets |
| the gate, launching, publishing | the client's own work | never delegated |

The caveat. Reading the author's verbs as "the agent asks the client for
work" is a legitimate use of input-required, but A2A defines no semantics
for a launch, a measurement or a relayed reply. Those are an application
contract on top of the protocol. So an arbitrary A2A agent could not become
an author by pointing the kernel at its card; it would have to speak our
contract inside A2A's envelope. What A2A gives is the envelope and the
lifecycle vocabulary, which is worth something, and no more than that.

Two things do not map and should not. Authority: A2A carries no notion of
who may merge, spend, or measure; those stay kernel-side. Trust: an agent's
messages are data. The inbox renderer fences every body; any adapter would
feed the same renderer.

## Transport: one inbox, adapters in front of it

`lifecycle.md` already says the inbox is a durable store with put, list, get
and conditional put, that delivery happens only at a wake, and that a cloud
backend changes the store's implementation and nothing else. HTTP JSON-RPC
is not a store; it is an execution interface. So the honest shape is:

- **The inbox stays the one store**, on every backend. It owns replay,
  ordering, first-key-wins deduplication, cancellation and the wake policy.
- **An A2A adapter feeds it.** For an agent that lives as a service, the
  kernel acts as the A2A client: send with immediate return, poll the task,
  and append what comes back as inbox messages; answer the agent's
  input-required by running the kernel's own steps and sending the next
  message. Streaming and webhooks are optional in A2A and unnecessary for
  wake-based delivery. Inbound deduplication comes from the inbox's key
  rule. Outbound sends need their own care: a crash between a send and the
  record of its task id would resend and start duplicate remote work, so the
  adapter writes a send record (our message id, reused as A2A's message id)
  before it sends. A2A leaves deduplication by message id optional, so on a
  retry the adapter first lists the context's tasks and adopts the one that
  carries that message id; it resends only when none does.
- **Slurm and local compute keep the file channel** with no adapter at all.
  Nothing serves HTTP there and nothing needs to.

## MCP: a contradiction to resolve first

The syscall tool could be presented as an MCP server. `role-cli.md` rejected
that: the CLI-over-Bash surface keeps Claude Code, Codex and hermes
identical, runs inside the jail with no server process, and leaves the
kernel-side validator as the only trust boundary. All three harnesses now
take MCP servers by config, including headless runs, so the old asymmetry is
gone, but nothing has shown a benefit worth a server per session and a
config per backend. The verbs stay a CLI.

Where MCP fits is contested by our own notes. `agent-substrate.md` reserves
a harness-provided MCP surface for the retriever and PR-context read;
`role-cli.md` plans `retrieve` as another CLI verb and says no MCP server.
One of them has to yield. The choice is not about protocols: it is whether
read tools that the kernel owns should be one more verb on the file channel
or a discoverable server. Neither is built. MCP's Tasks extension would not
change the lifecycle either way; the kernel already owns waits, parks and
wakes, so a second durable-task mechanism inside a tool call has no job here.

## If Outerloop decentralizes: kernel to kernel

The owner's forward question: suppose every lab runs its own kernel and
kernels talk to each other. That is the case where a protocol stops being
speculative, because a kernel is what A2A was designed around: an always-on
service with an identity, taking tasks and returning artifacts. Sessions on
Slurm stay files; kernels on the network do not.

What kernels would say to each other, and what already carries it:

- **Shared research state on one target.** Attempts, hypotheses, results
  and reports for a benchmark several labs work on. This is already
  decentralized and already has a protocol: git. The research-log branch is
  the ledger, PRs are the human-facing channel, and every kernel reads and
  writes the same repository. What is missing for several kernels on one
  repository is coordination, not transport: who publishes the board, and
  per-kernel namespaces in the ledger so two kernels never fight over one
  file.
- **Delegated work.** A run on kernel A needs an experiment or a measurement
  that kernel B's compute can run. This is a task: a sealed tree, a command,
  a walltime, and back come numbers, logs and an artifact. It is the compute
  seam (`submit`, `status`) crossing a trust boundary, and an A2A task fits
  it directly: kernel A is the client, kernel B the agent; input-required
  covers "send me the data shards"; the result is an artifact.
- **Independent verification.** Kernel A asks kernel B to re-measure a claim
  it did not produce. Also a task, with a signed verdict as the artifact.
  A2A 1.0's signed Agent Cards give the identity; the protocol does not make
  the measurement honest. That needs attestation or replay, the crux the
  market note already names, and it stays a kernel-owned rigid step on the
  verifying side.
- **Discovery.** Which kernels exist, which benchmarks they host, what
  compute they offer. Agent Cards carry the description; a registry or the
  target repository itself carries the list. AGNTCY's schema work is the
  candidate for a registry if one is ever needed.
- **Authors across kernels.** The sibling view across labs on one target is
  the shared ledger again, read through git, not a message between kernels.

What a protocol does not solve there, and what would need its own design:
trust in another kernel's numbers (attestation, replay), credit and budgets
across kernels (A2A carries none; the Agent Payments Protocol launched
beside it is the closest thing), and provenance (which kernel measured what,
signed). Those are the same rigid steps every kernel keeps for itself today,
extended across a boundary.

The consequence for the present design is small and concrete: keep the
message model A2A-shaped and keep the transport behind the inbox, so that a
kernel-to-kernel adapter is the same adapter as the service-agent one, with
each kernel acting as client in one direction and agent in the other. The
market design (a separate private note) is the first place this would be
needed; git stays the substrate for everything that is about one repository.

## Decision: an A2A-shaped message model, and coordination inside one kernel

The owner's decision (2026-09-14): the message model becomes more A2A-shaped,
and this is the same effort as multi-agent author coordination inside one
kernel, which `lifecycle.md` had parked under "Later" and `scaling.md`
describes as the planner. This section is the design for both, revised
after a second model's review, which cut it down: stable identities and
bounded routing first, protocol naming only where it costs nothing, and no
schema work ahead of an adapter.

**Simple messages, not conversations.** There is no conversation object.
A message is one thing said once; a response is another message that names
the one it answers. The agent's own session is its memory of what it said
and heard, resumed at every wake; the inbox is what arrived since. So at a
wake the agent sees the full inbox view it sees today, everything
undelivered in arrival order, each item fenced, and a message that answers
an earlier one says so in one line ("agent-02, replying to your message
about EMA: ..."). No threading, no grouping, no summaries of past exchanges;
the session already holds those. This is what A2A's context and task ids
are for as well: correlation, so a reader can tell what a message is about,
never a structure the reader must reconstruct.

**The envelope.** Today a message is (seq, kind, source, origin, thread,
arrived, key, payload). The changes are small and each has one job:

- `message_id`: globally unique, deterministic, namespaced by the inbox
  (`<run id>/<key>`), so a message that crosses an adapter or is quoted by
  another run has one name. Deduplication stays first-key-wins per inbox.
- `context_id`: what a message is about, with one rule: the run it concerns.
  A planner's own inbox is its search line's context; a message it sends
  into an author's inbox is about that author's run.
- `from` and `to`: `from` keeps today's category and identity (`source`,
  `origin`) and the kernel sets it, never the sender; `to` is the recipient
  as a kernel identity (a run id, `kernel`, a GitHub thread) and is what
  routing needs. Replies already store their destination when staged; this
  makes every message do so.
- `in_reply_to`: optional, the `message_id` this one answers. Correlation,
  nothing more.
- `kind`, `payload`, `thread`, `arrived`, `seq`: unchanged. The renderer and
  the wake rule read `kind` and `payload` today; they keep doing so. A2A's
  `parts` and `role` are the adapter's business: `role` is relative to which
  side of a protocol exchange one is on and must never imply permission
  inside the kernel, and `parts` needs artifact ownership and size rules
  that no adapter yet asks for.

One versioned decoder reads every inbox file, for delivery, for
deduplication and for appends alike: old files without the new fields get
them on read (`to` = the inbox's own run, `message_id` from the run id and
key); nothing is rewritten; a file the decoder cannot read stops delivery,
as today, and never silently drops out of deduplication.

**One verb for saying things.** Today `reply` posts publicly to the run's
PR or issue and `note` comes back to the author at its next wake, stored as
an inbox message the author itself sent (its origin is already the run; the
header just hides it). A sibling message would be a third spelling of the
same act. So: one verb, `message --to thread|self|<agent> <text>` (or
`--file`), with `--reply-to <message id>` for correlation. The destination
carries the consequences: `thread` is public and permanent, so the kernel's
GitHub delivery path redacts, posts once, and keeps the thread's history,
and the tool's own confirmation says "this will be posted publicly on
PR #17"; `self` is what `note` was; `<agent>` is the sibling case below.
`reply` and `note` retire in the same release (the tool is installed per
session by the kernel, so the change is atomic), with old inbox entries of
kind `note` still readable. The inbox header names the sender by kernel-set
identity, never by category alone: `from: agent-04 (run …, you)` for a note
to self, `from: agent-02 (run …)` for a sibling, `from: alice (GitHub,
member)` for a human. There is no message to the kernel: the kernel reads
structured verbs (launch, submit, sleep, end) and delivers mail; free text to
it would have no reader.

**Routing: the kernel is the hub.** Agents never talk to each other
directly. An author stages `message --to agent-04 …`; the kernel validates
it like every syscall and appends it to the recipient's inbox with `from`
set by the kernel. The tool is untrusted, so the kernel owns the rules: the
recipient must be a live run on the same target; the sender's identity is
the run's, resolved by the kernel; per-run limits on messages, bytes and
backlog, so a prompt-injected author cannot flood a sibling or start a
loop; a message to a run in review queues behind its jobs and grants
nothing; a message to an ended run is refused with one line back; the
kernel never acknowledges a message with a message of its own. Delivery
follows the existing rule: it waits behind the recipient's jobs and arrives
at its next wake. The sibling view stays a derived read of the ledger; a
message is for when an author has something to say to one sibling.

**Sub-agents: not a tier, for now.** A kernel-level agent task (`launch
--agent`: one session under the parent's ceiling, on a sealed snapshot,
metered against the parent, returning one report) would buy kernel-owned
isolation, durable dispatch, explicit spend reservation and uniform
timeouts across backends. Those are real, but nothing measured asks for
them yet: the harness backends already run in-session sub-agents under the
parent's RoleSpec ceiling (`agent-substrate.md`), job arrays already give
parallelism on compute nodes, and a cheaper model is a harness setting. So
the tier is out of the committed sequence. Its trigger is evidence: an
attempt that fails or wastes measurable resources because in-session
sub-agents and jobs cannot meet a need (work across the parent's sleep, an
isolation or accounting the harness cannot give). Before that, verify that
the existing sub-agent ceiling is enforced as declared; that is cheaper and
overdue.

**The planner writes the plan.** Most of a planner's value lands when the
kernel picks a direction for a new climb, not while runs are live. So the
first planner is a plan writer: one bounded invocation per search line, on
a cadence or after a merge, that reads the leader, the board (which now
carries every run's hypothesis), recent reports, lessons and the budget,
and drafts the plan section of the search-line issue. The kernel posts it
(the planner never touches GitHub) and puts the plan's open directions,
beside the current ledger activity, into every new climb's brief as
advisory context under the existing admission and budget rules. No
persistent planner inbox or session, no routine messages to live runs, no
second plan store, no dependency on agent tasks or on the envelope change.
It holds no authority: no starting runs, no spending, no measuring, no
merging, no approving its own plan; the issue's veto window stays the human
gate. Evidence: duplicate hypotheses and duplicated experiment spend against
the weeks before, on the same benchmark. It cannot guarantee non-overlap
(two authors can pick the same open direction from identical briefs before
either claim reaches the ledger); if advisory context proves insufficient,
an atomic claim at admission is the next step, and live redirection through
`message --to` only after observed collisions justify it.

**Sequencing.** Each stage lands as one PR, reviewed and run on one fleet
from its commit, with its own acceptance evidence; "nothing moved" is not
evidence.

1. **Envelope and decoder.** `message_id`, `context_id`, `from`/`to`,
   `in_reply_to`, one versioned decoder, every producer setting them, the
   header naming senders by identity. Fleet evidence: old and new inbox
   files delivered identically, a restart mid-wake, a duplicate append, a
   damaged entry, unchanged wakes.
2. **One `message` verb and sibling routing.** `message --to
   thread|self|<agent>`, `reply` and `note` retired, the kernel's validation
   and limits, delivery into the recipient's inbox, the board showing a
   message's sender. Fleet evidence: a public reply still lands once and
   redacted; a note still comes back; one author tells a sibling something
   and the sibling reads it at its next wake; a forged identity, a flood, a
   message in review and one to an ended run each handled as specified.
3. **The plan-writing planner.** One search line on gpt-speedrun. Fleet
   evidence: fewer duplicate hypotheses and less duplicated spend than the
   unplanned weeks before; the veto window exercised.

Sub-agents wait for their trigger. Stage 1 is mechanical; stages 2 and 3
are the semantic ones and each is read by the owner before it is built.

## Criteria instead of approvals

Nothing in code now. The note itself is the deliverable. What would justify
each later step:

- **An A2A adapter** when a service-shaped agent has a concrete owner and a
  first integration: the cloud backend, an adopter who wants to bring an
  agent with an existing A2A endpoint, the planner from `scaling.md`, or
  another kernel (the market). The
  integration defines the application contract inside A2A's envelope; the
  adapter feeds the inbox; the kernel stays a client and needs no Agent Card
  of its own. Pin the A2A version.
- **Vocabulary and addressing.** Decided above: the envelope takes A2A's
  names, and routing is the kernel's (`from`, `to`), since A2A's ids are
  correlation, not identity.
- **The retriever surface.** Decide CLI verb or MCP server on its own merits
  before either is built.

Not planned: A2A as the transport on Slurm, an agent directory, signed Agent
Cards for our fleet, a state-machine extension (A2A cannot add states), and
any protocol feature that would move authority out of the kernel.

## Questions for the owner

1. Does A2A stay an adapter candidate under the criteria above, with the
   first service-shaped integration deciding it, rather than a schema we
   adopt now?
2. Which retriever surface wins, the CLI verb of `role-cli.md` or the MCP
   server of `agent-substrate.md`? The loser's sentence should be struck.
3. Is there a first integration on the horizon (cloud backend, an adopter's
   agent, the planner) that should set the timeline?

## Sources

- A2A specification 1.0: https://a2a-protocol.org/latest/specification/
- A2A 1.0.1 release: https://github.com/a2aproject/A2A/releases/tag/v1.0.1
- A2A extensions (no new state values): https://a2a-protocol.org/latest/topics/extensions/
- A2A, life of a task: https://a2a-protocol.org/latest/topics/life-of-a-task/
- ACP joins A2A (August 2025): https://lfaidata.foundation/communityblog/2025/08/29/acp-joins-forces-with-a2a-under-the-linux-foundations-lf-ai-data/
- Linux Foundation, A2A first year (April 2026): https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year
- MCP specification 2026-07-28 and changelog: https://modelcontextprotocol.io/specification/latest and https://modelcontextprotocol.io/specification/2026-07-28/changelog
- MCP Tasks extension: https://modelcontextprotocol.io/extensions/tasks/overview
- AGNTCY: https://agntcy.org/
- Kang and Diponegoro, Governance Gaps in Agent Interoperability Protocols (June 2026): https://arxiv.org/abs/2606.31498
- Claude Code MCP configuration: https://code.claude.com/docs/en/mcp
- Codex MCP configuration: https://developers.openai.com/codex/mcp
- hermes MCP: https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp
