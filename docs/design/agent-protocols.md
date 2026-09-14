# Agent protocols: what A2A and MCP would give Outerloop

Status: design note, September 2026. A question from the owner: the
agent-to-agent protocols have settled since we designed the lifecycle. Which
of them fit, what would adoption cost, and what would it buy?

The short answer. A2A's data model (a task with a state machine, messages
made of parts, "input required", artifacts) describes what the kernel and an
author already do, almost line for line. Its transport (JSON-RPC over HTTP)
does not fit Slurm, where nothing serves HTTP and messages are files. So the
recommendation is to adopt A2A's shapes as the schema of our messages and
keep the file store as one transport of it, with HTTP JSON-RPC as a second
transport for agents that live as services. MCP is settled for the
agent-to-tool layer, and the place it fits here is the one
`agent-substrate.md` already reserved for it: the harness-provided read
tools. The author's syscall surface stays a CLI. Nothing needs building
today; the note records the mapping so the next design items land on it.

## The landscape, as of this month

**A2A (Agent2Agent).** Started by Google in April 2025, hosted by the Linux
Foundation since June 2025, version 1.0 in March 2026 and 1.0.1 in May 2026.
Over 150 organizations back it, and it ships in Azure AI Foundry, Copilot
Studio, Amazon Bedrock AgentCore and Google Cloud. An agent publishes an
Agent Card (identity, skills, capabilities, security schemes, endpoint). A
client sends a Message; the agent answers with a Task that moves through
submitted, working, input-required, auth-required, completed, failed,
canceled and rejected. Messages carry Parts (text, file, structured data)
and Tasks carry Artifacts. Long-running work is polled or pushed to a webhook;
a task that needs something goes to input-required and the client sends the
next message with the same task id. Three bindings: JSON-RPC 2.0, gRPC and
HTTP+JSON. Extensions, declared by URI in the Agent Card, may add data, new
methods, or new task states. IBM's ACP was folded into A2A in 2025.

**MCP (Model Context Protocol).** The agent-to-tool layer, adopted by every
harness we run (Claude Code, Codex, hermes; the last two also act as MCP
servers). The 2026-07-28 revision made the core stateless (no session
handshake; requests carry their capabilities), added multi-round-trip
requests so a server can ask the client for input without a persistent
connection, and moved long-running work into a Tasks extension (a durable
task id, polling, input-required, cancel). Servers expose tools, resources
and prompts; the client owns consent.

**The rest.** AGNTCY (Cisco, now under the Linux Foundation) works on agent
discovery and identity with its Open Agent Schema Framework. Agent Network
Protocol and the on-chain identity efforts target decentralized markets. A
June 2026 survey of the governance these protocols can express finds that
none carries membership, deliberation, dissent, escalation or audit; the
authors call governance "a missing architectural layer above current
interoperability standards, not a missing feature within them." That matches
our own line: protocols carry messages, never authority.

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
3. **Kernel to humans.** GitHub: PRs, comments, reviews, checks. The inbox
   already carries these as messages, and the outbox posts replies.
4. **Kernel to compute.** Jobs on Slurm or the local pool. Not an agent
   boundary; no protocol applies.
5. **Kernel to other agents.** A planner assigning directions, an adopter's
   own agent as an author, a steward or maintainer, sub-agent teams inside a
   run. Today this seam does not exist; the lifecycle doc lists it under
   "Later" as the item that will need real addressing.

## A2A maps onto the lifecycle

The mapping is close enough to write down as a table. The one twist is the
direction of asking: in A2A the client sends a task and the agent may answer
"input required"; in Outerloop the author's verbs are exactly those requests,
and the kernel's inbox messages are the client's answers.

| Outerloop | A2A | Note |
| --- | --- | --- |
| a run | a context (`contextId`) | one run, many legs |
| a session leg (wake to sleep) | a task (`taskId`) | the leg's answer is the staged request |
| `running` | working | |
| `parked` on jobs | input-required: the launch results | the kernel is the client that fulfils it |
| `parked` on a submit | input-required: the gate and panel verdicts | |
| `parked` with a PR, no jobs | input-required: the next human message | no deadline, no wake attempt |
| `ended` (six endings) | completed, failed, canceled, rejected | endings carry the report as an artifact |
| inbox message kinds | messages from the client, parts by kind | launch-result, gate-verdict, panel-verdict, comment, check-result, base-moved, head-moved, note |
| `reply` | a message the agent asks the client to relay | the outbox; the client posts it to the thread |
| the report, the PR, the line snapshot | artifacts | |
| budgets and the meter | out of band | A2A has no budgets; the kernel keeps them |
| the gate, launching, publishing | out of band | the client's own work, never delegated |

Two things do not map and should not. Authority: A2A carries no notion of
who may merge, spend, or measure; those stay kernel-side, as
`architecture.md` says. Trust: an agent's messages are data. The inbox
renderer fences every body; an A2A transport would feed the same renderer.

## Transport is the same abstraction

`lifecycle.md` already says the inbox is a store with put, list, get and
conditional put, with a filesystem implementation for Slurm and local
compute and an object store for a cloud backend. A2A fits that sentence
rather than replacing it:

- **Slurm and local compute.** Nothing serves HTTP. The kernel is a resident
  process; a session is a batch job. The store is files, delivery happens at
  a wake, and there is nothing to push to. A2A's shapes ride the files: the
  message file is a Message with Parts, the record's state is the Task
  state, the run id is the context id.
- **Agents that live as services.** A cloud backend, an adopter's own agent,
  a planner. These have an endpoint and an Agent Card, and A2A's HTTP
  JSON-RPC binding is the natural transport: the kernel is the client, the
  agent is the server, and "input required" is how the agent asks for a
  launch, a measurement or a reply. The kernel still runs those itself.

The abstraction is the message store plus the task state machine; the
transport underneath it is a deployment detail. That is the same split as
compute backends (`has_lanes`, `submit`, `status`) and it is why nothing
above the backend should know which transport it is on.

## MCP: where it fits and where it does not

The syscall tool is, functionally, an MCP server with six tools. Turning it
into one would make the calls typed and let every harness discover them the
same way. `role-cli.md` rejected this for a reason that still holds: the
CLI-over-Bash surface is what keeps Claude Code, Codex and hermes identical,
runs inside the jail with no server process, and leaves the kernel-side
validator as the only trust boundary. The parse-repair loop the tool was
built to kill is already gone. So the author's verbs stay a CLI.

MCP does fit the place `agent-substrate.md` reserved for it: harness-provided
read tools (the retriever, PR-context read), where a uniform, kernel-owned
surface across backends is the point and the calls are read-only. All three
harnesses take MCP servers by config, including headless runs (`claude -p
--mcp-config`, Codex's `mcp_servers` table, hermes's MCP client). MCP's Tasks
extension is for waits inside one tool call; our sleeps end the session on
purpose, so it does not apply to the lifecycle.

## What to do, and when

**Now: nothing in code.** Record the mapping (this note) and use A2A's
vocabulary when the message design moves again: message id, context id,
task id, role, parts. The current inbox envelope (kind, source, origin,
thread, key, payload) is a profile of it, and the rename can wait for a
reason.

**When the first service-shaped agent appears** (the cloud backend, an
adopter bringing their own agent, the planner in `scaling.md`): add the A2A
HTTP JSON-RPC transport as a second implementation of the message store,
publish an Agent Card for the kernel's own role in that exchange, and define
one A2A extension for the research task (a state-machine extension: the
park shapes and the six endings). The file transport stays for Slurm and
local compute. This is also the answer to the "Later" item in
`lifecycle.md`: A2A supplies the addressing (context id, task id, message
id, role) that sub-agent teams will need, so that item becomes "adopt the
envelope" rather than "design one".

**When the harness-provided read tools are built:** MCP, per
`agent-substrate.md`.

**Not now, and probably not ever:** A2A as the transport on Slurm, an agent
directory, signed Agent Cards for our own fleet, and any protocol feature
that would move authority (merge, spend, measure) out of the kernel.

## Cost and benefit

| | Cost | Benefit |
| --- | --- | --- |
| A2A vocabulary in the design | a rename in docs, later in the envelope | future items land on a known shape |
| A2A HTTP transport for service agents | one store implementation, one Agent Card, one extension; auth (API key or OAuth) and egress rules for the kernel as a client | adopters plug in any A2A agent as an author or reviewer; the cloud backend needs no bespoke channel |
| MCP for harness read tools | one server, three harness configs | one kernel-owned read surface across backends |
| MCP for the syscall verbs | a server per session inside the jail, backend config drift | typed calls; already not needed |

The risks are the usual ones for a young standard: A2A moved from 0.3 to 1.0
in under a year and its extension registry is still forming, so anything we
publish should pin a version. And every inbound message, whatever the
transport, is data: the renderer's fence and the kernel's validators are the
boundary, not the protocol.

## Decisions asked of the owner

1. Adopt A2A's data model as the reference shape for messages and tasks in
   the design docs (no code change now)?
2. Agree that the file store and an HTTP transport are two implementations
   of one message store, and that the HTTP one waits for the first
   service-shaped agent?
3. Keep the author's syscall surface a CLI, with MCP reserved for the
   harness-provided read tools?

## Sources

- A2A specification 1.0: https://a2a-protocol.org/latest/specification/
- A2A extensions: https://a2a-protocol.org/latest/topics/extensions/
- Linux Foundation, A2A first year (April 2026): https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year
- MCP specification 2026-07-28: https://modelcontextprotocol.io/specification/latest
- MCP Tasks extension: https://modelcontextprotocol.io/extensions/tasks/overview
- Kang and Diponegoro, Governance Gaps in Agent Interoperability Protocols (June 2026): https://arxiv.org/abs/2606.31498
- Survey of agent interoperability protocols (MCP, ACP, A2A, ANP): https://arxiv.org/abs/2505.02279
- Claude Code MCP configuration: https://code.claude.com/docs/en/mcp
- Codex MCP configuration: https://developers.openai.com/codex/mcp
- hermes MCP: https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp
