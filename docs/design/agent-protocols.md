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
store; an A2A adapter that feeds it is a candidate for the first agent that
lives as a service, and it earns its place by that integration, not before.
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
| `parked` on jobs, on a submit, or on the next human message | input-required | the reason travels as data; a checkpoint timeout needs no answer and is a kernel-side wake |
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
  wake-based delivery. A2A does not require send deduplication; the adapter
  gets it from the inbox's key rule.
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

## Criteria instead of approvals

Nothing in code now. The note itself is the deliverable. What would justify
each later step:

- **An A2A adapter** when a service-shaped agent has a concrete owner and a
  first integration: the cloud backend, an adopter who wants to bring an
  agent with an existing A2A endpoint, or the planner from `scaling.md`. The
  integration defines the application contract inside A2A's envelope; the
  adapter feeds the inbox; the kernel stays a client and needs no Agent Card
  of its own. Pin the A2A version.
- **Vocabulary.** When the message envelope next changes for a reason of its
  own, prefer A2A's names (message id, context id, task id, role, parts) to
  invented ones. Not a reason to change it now.
- **Team addressing.** A2A's ids are correlation, not identity or routing.
  Sub-agent teams still need their own design of who may send to whom, with
  provenance; that item stays open in `lifecycle.md`.
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
