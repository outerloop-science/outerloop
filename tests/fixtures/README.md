# Endpoint compatibility fixtures

- `dispatched_pre_gpu_identity/`: run directories produced by
  `DispatchedMeasurer._dispatch` from kernel `d4ad7529667de79aef299e2e0ad8b5fb6c9c9598`,
  before GPU count/type entered the determinant. A fake Slurm submit returned
  job `101`; the completed variant adds the job's exit-code and metric stdout.
  Both use a synthetic repo/image, one GPU, and `SEED=7`. `identity.json`
  records that kernel's slot and scheduler name. The new reader ignores and
  preserves the directories (including when interrupted), so old jobs can
  finish writing and
  rollback can still read the old records. Legacy slots are cache misses; the
  new kernel dispatches once under the GPU-aware key without charging the
  resumed gate again. Newly dispatched GPU-aware slots
  are not readable by the old kernel without re-measurement. Cross-run legacy
  baseline entries remain misses.

- `author_route_legacy.json`: produced by `runstate.RunRecord` and
  `runstate.save_record` from kernel commit `5563c46` (the parent tree before the
  endpoint change), with synthetic `owner/repo` and key-file coordinates.
- `hermes_sample_20260924.json`: produced from synthetic user, assistant/tool-call,
  tool-result, and final-assistant messages by
  `agent.agent_runtime_helpers.convert_to_trajectory_format` and
  `run_agent._save_sample_trajectory` at Hermes
  `f97608f178d1ffeca59860195ab7da295f7c8e5f`. Timestamp normalized; no model calls.
  The pure functions were extracted with Python AST to avoid starting Hermes or
  importing its optional runtime integrations.

The other fixtures carry their originating kernel commit in their filenames.

- `author_route_missing_model.json` and `author_route_missing_route.json`: synthetic
  derivatives of `author_route_legacy.json` with model or all route fields omitted,
  exercising pre-field writers through load/save and the new kernel wake entry point.

`pre_contract_switches.json` contains a run record, brief, pending submission and
leader row without verification, channels or provenance fields. Readers default
to gated verification, enabled channels and measured provenance without backfill.

`default_contract_head.json` captures serialized run, brief, pending and leader
records plus rendered brief, ledger and board text from HEAD `9716372`, using
the inputs in `pre_contract_switches.json`. Its pending field list preserves
the strict HEAD reader shape: a newly serialized default submission must
construct that shape without extra keys. Default output is compared byte for
byte; explicit claims continue to carry provenance.
