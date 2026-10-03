# One model gateway per deployment

Status: draft for review. Tracking issue: #464.

## Problem

Harnesses and model servers do not speak the same API. Codex speaks only the
Responses API; self-hosted servers speak Chat Completions reliably and
Responses only partly (tool calls in particular). The kernel translates
between them, and today it does so in two different ways:

- **Per-session bridge** (`docs/endpoints.md`, "Codex on a chat-completions
  endpoint"): for a profile with `API=chat`, every Codex session starts a pinned
  LiteLLM proxy and a streaming shim inside its own container, on loopback,
  with ephemeral tokens. Only the shim holds the endpoint credential. Requests
  the bridge cannot translate faithfully are rejected.
- **Direct endpoint**: a profile that advertises `responses` is called
  directly. A deployment can point such a profile at its own translating
  proxy, which the kernel cannot see or check.

The two paths diverge. When Codex 0.160 added a sub-agent tool and a hosted
web-search tool, the per-session bridge rejected every request ("unsupported
bridge request") while a shared proxy on another deployment let the same
requests through. One harness upgrade, two behaviors, tested on one.

## Proposal

Each deployment runs **one model gateway**: a single pinned service that every
author and judge session reaches by URL, whatever the backend.

- **Surfaces**: Responses, Chat Completions and Anthropic Messages, each mapped
  to the upstream API the model server actually supports. A server with native
  Responses support is passed through.
- **Faithfulness rule**: the request check the bridge applies today becomes a
  gateway rule. A request with a part the gateway cannot translate faithfully
  is refused with a clear error, the same way on every path.
- **Credentials**: the gateway holds the upstream credentials (provider keys,
  self-hosted endpoint keys, cloud service accounts). Sessions receive a
  gateway key scoped to their role (author or judge) and to the models that role
  may use. As today, a session never sees an upstream credential; author and
  judge keys stay distinct, so the existing credential-separation preflight
  keeps its meaning.
- **Accounting**: the gateway logs tokens per model, role and run (from a
  request header the kernel sets), as a second source next to session
  capture.
- **Lifecycle**: an operator service with a health endpoint, pinned like the
  harnesses (`harnesses.toml`), upgraded through `outerloop harness upgrade`.
  The kernel treats it as an endpoint: a dead gateway parks fresh runs and
  defers wakes (the existing endpoint-wait path), it is not a session error.
- **Profiles**: an endpoint profile names the gateway URL and a route, for
  example `OUTERLOOP_ENDPOINT_AUTHOR_URL=http://gateway.internal:4000/v1` with
  `_MODEL=open-model`. `_API` lists the surfaces the gateway offers for that
  route, so preflight stays a local configuration check.

## What changes for sessions

- Codex keeps Responses over HTTP/SSE; the gateway does the translation the
  bridge did. Claude Code keeps Anthropic Messages. Hermes keeps Chat.
- Containers need network access to the gateway (they already reach the model
  server for direct endpoints). The per-session loopback listeners, tokens and
  read-only bridge runtime go away.

## Trade-offs

- **Availability**: the gateway is shared, so if it is down every session on
  the deployment waits. The model server is already shared in the same way, and
  the endpoint-wait path already parks runs instead of failing them.
- **Blast radius of a bad upgrade**: one pinned gateway upgrade reaches every
  session at once. Mitigation: a conformance check (below) before the pin moves,
  and pinning, never floating.
- **Trust boundary**: today the credential lives inside each session's
  container for the session's lifetime; with a gateway it lives in one
  operator-owned process. A gateway key leaks no upstream credential, and its
  scope (role, models, a spend cap) limits what a leaked key can do.

## Migration

1. Ship the gateway as an optional service, with profiles pointing at it, while
   the per-session bridge stays the default for `API=chat`.
2. Run one deployment on the gateway through a soak; compare session outcomes
   and token accounting with session capture.
3. Make the gateway the default; delete the per-session bridge, its runtime
   install and its tests in the same change (one implementation per role).

## Conformance check

A scripted session per (harness version, upstream server) pair: tool calls,
freeform `apply_patch`, resume, streaming, and a refused unsupported part. Run
when either pin changes. It would have caught the Codex 0.160 tool types
before a fleet did.

## Open questions

1. Where the gateway runs on a cluster without a long-lived service host: a
   scheduler job managed like a model server (with a successor before its
   walltime), or a small always-on VM.
2. Whether Claude Code sessions on an Anthropic-compatible server should also
   go through the gateway (uniform accounting) or stay direct (one hop fewer).
3. Gateway key scope: per role per deployment, or per run (revoked at the run's
   end).
