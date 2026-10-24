# Run trajectory

The kernel alone writes `runs/<run-id>/trajectory.jsonl`, outside the session's
workspace and writable sandbox mounts. Operational state, launch ledgers,
inbox/outbox and session records continue unchanged. The trajectory is a
best-effort timeline, not a replacement for those recovery records.

Each line is a JSON object with sorted keys: `seq` (starting at zero), `ts`
(Unix seconds), `type`, `actor`, `data`, `refs`, and `prev`. `prev` is SHA-256
of the previous event's exact bytes excluding its newline; the first event
uses SHA-256 of the UTF-8 run id. Actors use `kernel`, `author:<agent-id>`,
`judge:<name>`, or `human:<login>`; these kernel actions use `kernel`.
Refs contain run-relative `path` and `sha256` fields. Native session logs are
hashed once at session end, and the report is hashed at ending.

Events cover session start/end, syscall acceptance/refusal, launch submission
and results, gate results, acknowledged inbox delivery, wakes, PR opening
(including reconciliation of an already opened PR), and endings. Inbox events
contain sequence ranges, kinds and origins, never message bodies. Session start
may have an empty id until the backend returns one. Exceptions produce a session
end outcome when the host can catch them; process death can leave an unmatched
start. Measurement credit describes the gate decision, not publication or merge.

A sibling `trajectory.lock` serializes appenders with exclusive `flock`. A torn
last line is preserved and separated by a newline; the chain continues from
the last parseable event. Verification reports malformed lines even after
recovery. A complete JSON event missing only its newline is retained and its
newline repaired on the next append. No old bytes are rewritten. Writes stop
at 4 MB and warn once per file per kernel process; I/O failures warn without
failing a tick. Events are not transactional with the operational files, so a
crash or write failure can leave missing events or retries.

`outerloop trajectory <run-id> --verify` checks sequence gaps and byte hashes;
`--json` emits events (and verification problems when requested). Missing legacy
files read as an empty history and verification says "no trajectory". No history
is reconstructed from older operational files. Older kernels ignore this file.
The chain detects internal edits and missing lines, but without a trusted
external anchor cannot detect a rewritten chain or removal of its tail.
Verification checks the event chain, not the contents of referenced artifacts.

`measure` events carry provenance. They record the ledger's ruler digest (the
measurement signature plus the non-solver tree entries, the value a pending
submission also computes), the container image digest when one is configured,
and refs to the evaluation result files the kernel read under the run
directory. Self-reported measures record the ruler alone: they have no
evaluation image or result files. A pending submission does not yet name the
trajectory head: kernels before this one reject unknown pending fields, so the
pending reader now ignores them, and a later release can add the field without
breaking this one. Tool calls and attestation are later steps.
