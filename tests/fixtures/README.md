# Endpoint compatibility fixtures

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
