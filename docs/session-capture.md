# Session capture

Each harness invocation, including a resume, retains evidence beside the existing
stdout capture in the run directory (`state/runs/<run-id>/`). The files are outside
`ws/` and `ws-home/`, are not mounted writable in the author's container, and
survive the default 24-hour workspace cleanup. They follow the run record's
retention policy; capture adds no separate expiry. Uncontained sessions still
have the same-user filesystem access described in the threat model. Advisory
callers without a run record retain files beside their supplied workspace; their
caller controls that directory's lifetime.

These private artifacts must never be committed, attached to PRs, or published
(SECURITY.md). Capture applies the same **known-secret redaction** as stdout,
including issued application tokens and pre-launch snapshots of provisioned credential
files (Vertex ADC, Codex auth, and mounted key contents). JSON string values,
escaped forms, and PEM key lines are included; deleting a credential during the
session does not remove it from the redaction set. This is not a general secret scanner.
Artifacts use owner-only permissions. Native reads refuse symlinks and special
files. Capture failures are logged and never change the author's outcome.

A unique `<workspace>-<backend>-<invocation-id>.session.json` records backend,
configured model, backend session ID, resume ID, start/end timestamps, turns when
known, token counters, cost, and the SHA-256 of the delivered prompt before redaction, including rehydrated Hermes
context on resume.
The invocation ID distinguishes wakes sharing a backend session ID. Artifact
entries record paths and SHA-256 hashes of the **stored, redacted bytes** for the
stdout capture, delivered prompt, and native log. Missing native logs have
`status: "missing"`; failed writes have `status: "write-error"`. Hermes also
retains the exact query pointing to its brief file, and its delivered prompt
includes the rehydrated conversation on resume.

Native log lookup uses identifiers, never newest-file selection:

| Backend | Native evidence | Usage |
| --- | --- | --- |
| Claude Code | `$HOME/.claude/projects/*/<session-id>.jsonl`; fresh invocations supply a UUID with `--session-id`, allowing lookup after timeout | Final JSON `usage.input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `output_tokens`; reported `total_cost_usd` |
| Codex | `$HOME/.codex/sessions/**/rollout-*-<thread-id>.jsonl`, using `thread.started.thread_id` | Sum `turn.completed.usage.input_tokens`, `cached_input_tokens`, `output_tokens` across this invocation |
| Hermes | `$HOME/evidence-<session-id>-<invocation-id>.json`, an explicit transcript export from the existing `run_conversation` result | Reported agent `session_prompt_tokens`, `session_completion_tokens`, `session_cache_read_tokens`, `session_cache_write_tokens`; absent usage remains unknown |

Hermes's pinned legacy CLI omits usage from its sample. A small wrapper invokes
that same CLI, retaining all returned messages (including tools), the native
Hermes session ID, and the counters before returning the original result.
It does not estimate tokens. The existing sample remains the author-result parsing channel,
not an identifier for evidence. Interrupted sessions may have no native transcript
or only partial usage; timeouts retain unknown dollars. Claude/Codex resumed native
logs can contain prior turns; their usage counters come from the current
invocation's stdout, so history is not billed again.

`OUTERLOOP_NATIVE_LOG_MAX_BYTES` caps the stored native log (default 33554432,
32 MiB). A cutoff sets `truncated: true` and records `cap_bytes`; truncated logs
may no longer be valid JSON/JSONL. Redaction looks beyond the cutoff to avoid
leaking a secret split by the boundary. An invalid setting uses the default;
zero retains an empty file marked truncated when the source is nonempty.
Unresolved secret prefixes at the lookahead boundary are withheld after replacing complete secrets.
Native discovery refuses directory symlinks and stops at 8 directory levels,
10,000 entries, or 0.25 seconds of elapsed time (checked between filesystem
operations). Reaching a limit records `status: "unavailable"` with
`reason: "discovery-limit"`.

`SessionResult.tokens` is a mapping of reported counters; missing keys mean
unknown. Normalized `input_tokens` includes cache reads and cache writes.
`cost_usd` retains its existing name and numeric values, with `None`/JSON null
for unknown cost. Claude reports dollars directly. For Codex/Hermes, operators
can supply a JSON model price table through `OUTERLOOP_TOKEN_PRICES`. Rates are
USD per million tokens, keyed by the exact configured model:

```sh
export OUTERLOOP_TOKEN_PRICES='{"example-model":{"input_tokens":2,"cached_input_tokens":0.5,"output_tokens":10,"cache_creation_input_tokens":2.5}}'
```

Uncached input is total input minus cache reads and cache writes. Each nonzero
bucket needs a finite, nonnegative rate. Missing model/rates, malformed prices,
or absent input/output usage leave dollars unknown. Codex also leaves invocation
usage and dollars unknown if any completed turn lacks required counters or
reports a different set of counters from the other turns, or the invocation fails. Explicit zero rates support
free/self-hosted models. This is recorded usage pricing, not invoice reconciliation;
existing budget enforcement is unchanged.

Parked records retain tokens and their session record path. Legacy numeric costs
remain readable; absent usage stays unknown and is not backfilled. Status totals
come exclusively from the fixed `session-index.json` in the run directory under
the state root, never from a glob or files in the workspace or session HOME.
Only the kernel writes this index, serializing updates and atomically replacing
it after retaining each session record. Separate status processes and restarted
kernels read the same persisted totals. Containment keeps this directory outside
the author's mounts; owner-only permissions alone are not the trust boundary.
Each record and index entry carries `verified`, true for contained invocations.
If any indexed invocation ran uncontained, totals carry `verified: false` and
text status labels them `unverified`. Local mode shares the operator's filesystem
authority, so those persisted totals can be altered by the author.

`known_session_cost_usd` sums priced sessions, `unpriced_sessions` counts unknown
costs, and `session_cost_usd` is null if any captured session is unpriced or no
indexed records exist. Missing or unreadable indexes report unknown totals and
`verified: false`; status never reconstructs them from sidecars. Pre-upgrade
sidecars are not imported, so history before the index has unknown coverage.
Do not interpret the known subtotal as total historical spend.

Validate fresh and resumed sessions with each pinned CLI on a real deployment,
including timeout, native paths, actual usage fields, Hermes wrapper imports and
container read-only bind, model aliases in the price table, and post-cleanup
artifact access. Synthetic tests do not establish provider invoice parity.
