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

- `session_cost_legacy.json`: synthetic parked-session stage in the
  pre-session-evidence shape (numeric cost and turns, no tokens or artifact
  paths). The production parked-session reader preserves that cost and report,
  tolerates absent new fields, and accepts new null costs without a migration.

## 0.3.0rc1 upgrade matrix (v0.2.1 → release branch)

`rc1_v021/` was produced from tag `v0.2.1`
(`21dfe925b4ac25fd8db24c82fe243ca5fd3c25b4`), not by deleting fields from
current-writer output. The producer is retained as `rc1_v021/generate.py.txt`.
The relevant sources were inspected with
`git show v0.2.1:src/outerloop/<module>.py` for `runstate`, `measure`,
`dispatch`, `launchlog`, `tick`, `brief`, and `attempt`. To reproduce:

```sh
mkdir -p /tmp/rc1-v021-src
git archive v0.2.1 src | tar -x -C /tmp/rc1-v021-src
uv run python tests/fixtures/rc1_v021/generate.py.txt /tmp/rc1-v021-src/src /tmp/rc1-fixtures
```

The script imports those archived modules in a separate interpreter. All
identities, credentials, queue replies, metrics, and file contents are synthetic.
Git commit dates are fixed; snapshot ref UUIDs may vary on regeneration.
Job scripts' output-root coordinates alone are normalized to `/fixture/state`;
tests never execute archived job scripts. The Git bundle contains real objects,
line ancestry and dispatch refs, without an environment-specific remote URL.
Tests substitute temporary-repository SHAs/refs and target coordinates where a
live Git workspace is necessary; they do not seed legacy records with current
`save_record`.

| Surface | Fixture and production | Tests and passes |
| --- | --- | --- |
| #452 records/request | `open.json`, `merged.json`, `ended.json`: archived `RunRecord` + `save_record`, with absent author-history/rebind/candidate-author fields. `rebind.json` is a **new request overlay**, since v0.2.1 did not have rebind. | `test_v021_rebind_retry`: absent request no-op; apply once with lazy history/candidate credit; second pass byte-stable; interrupt before record write or after write/before request removal, reload and retry. Ended records stay byte-identical. |
| #452 eval/launch credit | `eval-provenance/{command.txt,job.sh}`: archived `write_eval_job` (no provenance file existed). `launches.jsonl`: archived `append_submitted` (no author/commit). | `test_v021_rebind_eval_and_launch_provenance`: legacy ledger prefix preserved; new provenance credits the sealed candidate's original author; interrupted provenance replacement and append retry; repeats add neither duplicate rows nor changed provenance. |
| #449 terminal request | The three lifecycle records above, with `end-request.json` as a **new request overlay**. `pr-states.json` is synthetic GitHub response data, not a kernel persistence format; open/merged records intentionally differ only in the external PR state. | `test_v021_operator_end_retry`: absent request preserves bytes; present request ends once; interrupt after report creation/before terminal write, retry; stale writes cannot reopen; ended records ignore requests. `test_v021_pr_lifecycle_without_operator_request`: actual open/merged PR responses through current `close_if_done`, then repeat without another terminal transition. |
| #453 / #436 parks | `sessionless.json`: archived record writer, jobless `author-sleep`, empty native session ID, no `capacity_wait`. This is a synthetic record representable by the release writer, not a claim that v0.2.1 had capacity admission. `open.json` supplies the retained-session variant. | `test_v021_sessionless_author_sleep_resumes_directly`: current wake must start fresh, preserve the candidate, retry an interruption before the author leg, and reuse its pending gate on repeat. `test_v021_capacity_error_becomes_durable_park`: an actual `CapacityError` through the orchestrator retries once then parks, dedups the capacity note, and resumes without charging. `test_v021_capacity_refusal_park_resume`: current refusal/park layered onto either old record; interrupt after inbox append/before park write; retry and repeat dedup the note and preserve meters; wake starts/resumes appropriately; another gate pass does not rerun the author or charge again. |
| #444 branches | `line.bundle`: archived `_checkout_line`, `_push_line_snapshot`, `snapshot_tree` over a synthetic Git repository. The old line intentionally includes a protected-path change that the old writer allowed. | `test_v021_line_snapshot_upgrade_retry`: retain old contents/ancestry and dispatch refs; filter only new protected changes while retaining admitted work and memory; repeat leaves line tip unchanged; crash after successful push/before local acknowledgement then retry produces no extra seal. |
| #436 queue/intake | `queue.json`, `identity.json`: queue replies derived from archived launch/wake naming and `DispatchedMeasurer._job_name`; `pending/*.json`: archived `write_pending`, unsuffixed and agent-slot forms. | `test_v021_job_layout_and_pending_retry`: repeat attribution/queue ownership and queued-slot reservation without writes; terminal jobs free capacity; interruption publishing the new intake marker preserves old markers; retry and repeat produce one new marker and reserve capacity. Eval scheduler recovery also runs against the release slot below. |
| #455 eval slots | `eval-run/eval-*/{command.txt,job.sh,submitted,exit-code,stdout}` and `identity.json`: archived `DispatchedMeasurer._dispatch`, fake submit returning 101, then synthetic completed output. In-flight variant removes only simulated completion outputs. | `test_legacy_slot_is_miss_and_redispatch_is_idempotent[v021-*]`: completed and in-flight old slots miss; new submit once; crash between submit/marker write adopts live new job; repeated pending reads do not submit; new completed result reused; legacy bytes preserved. `test_legacy_eval_redispatch_preserves_run_gpu_meter` checks the resumed gate's charged meter. |
| #455 baseline | `baselines/main@aaaa….json`: archived `write_baseline_cache`. v0.2.1 **already wrote `gpus`**, but omitted `gpu_type`; its dispatched slot key omitted both GPU determinants. | `test_v021_baseline_cache_miss_retry_reuse`: old 0.1 misses, real dispatched reader measures new baseline/candidate exactly once; interrupt baseline-cache atomic replacement after results land; retry reuses those slots; second decision uses new baseline cache without submissions. |
| #430 retained instructions | `brief.txt`: archived `build_brief` + `render`, fixed timestamp and synthetic task, paired with `open.json`'s saved session. | `test_v021_parked_brief_uses_current_rubric`: resume the saved session without calling `build_brief` or redelivering old text; interrupt author before completion then retry; judge through current verifier brief/parser, including aggregation/landscape rubric; repeat on same measured candidate reuses results and gives same judgment. |

**Owner decision for #430:** explicitly tolerate previously delivered v0.2.1
instructions in parked sessions. No correction or initial brief is redelivered;
new judging uses the current rubric. This is tolerance, not a claim that changing
a fresh brief changes an existing session's history.

**Upgrade defect exposed:** the unmarked, sessionless, jobless author-sleep
fixture ended as `session-error` before reaching its author. The release fix is
limited to recognizing that checkpoint as eligible for a fresh author leg; it
adds no persisted fields. Parks with outstanding launches retain the existing
resumability guard.
