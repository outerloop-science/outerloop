# Reviewer and verifier infrastructure

This note records how we intend to build the reviewer and verifier agents.
The goal is to be able to change the model, the harness, or the search
provider later without a rewrite. It is written before we build so the
seams are agreed first. Nothing here is deployed yet.

## Where we are now

Both roles are one API call. We gather a fixed bundle of context (the diff,
the contract, the eval modules, the tests, the PR thread), send it in a
single request, and post the reply. This is cheap, fast, and safe: the
model runs no tools and executes nothing.

It has one limit. A single call cannot explore the code. It cannot open a
file the diff refers to but does not include, follow a caller, or run the
tests to check that a finding reproduces. On the pilot this is fine. On a
larger target it will miss things.

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

Define the agent's tools as MCP servers, not as harness-specific code. The
tools are `grep`, `read`, and `retrieve`. If they are MCP, they keep working
when we change the model or the harness. If we write them against one
harness's tool API instead, we have traded model lock-in for harness
lock-in.

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
we run. The runner can reach the broker and nothing else. The broker fetches
from an allowlist of permitted destinations and refuses to send data
outward.

This keeps the property we want. The agent can search, but the runner has no
open path to exfiltrate a secret. The control lives at the tool boundary,
which is cleaner than trusting a step that runs before the agent.

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

## Security

The danger is not running untrusted code. It is running untrusted code next
to a secret and a write token. A workflow that holds the reviewer's API key
and can post comments must not run code from the PR, or that code can steal
the key.

Two facts make this manageable.

First, reading the base tree is safe. The base is what the workflow already
checks out and trusts. An agent that greps and reads the base to trace a
claim runs no untrusted code. Only running the PR's own code is dangerous,
and that is a small part of what a reviewer does.

Second, the agent holds no write token. In the split-workflow pattern, the
agent runs sandboxed with no secrets beyond a spend-capped API key and
writes its findings to an artifact. A separate, minimal step with the write
token posts them. If the agent is prompt-injected, there is nothing
privileged to steal, and a poisoned result can at most produce a wrong
finding that a human filters out.

Today this is not even active. `verify.yml` only runs on same-repo,
bot-authored branches, so no untrusted PR reaches the reviewer. An agentic
reviewer that reads files and runs the eval is safe right now for internal
PRs. The sandbox and split-workflow work is what gates accepting fork PRs
after the repo goes public. See `public-surface.md` for that threat model.

## Sequencing

1. Define the MCP tool contracts: `grep`, `read`, `retrieve`.
2. Stand up the harness with the read-only base-tree tools only. These need
   no broker, because reading the trusted base is safe.
3. Add the `retrieve` tool and the broker later, without changing the
   harness.
4. Before making the agentic reviewer the default, score it against the
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
