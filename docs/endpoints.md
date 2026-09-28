# Self-hosted model endpoints

Authors, panel judges, and the standalone reviewer use the same named endpoint
profile. Put coordinates and a **key-file path**, never a bearer key, in the
operator `.env`:

```dotenv
OUTERLOOP_ENDPOINT_AUTHOR_URL=https://llm.example.internal/v1
OUTERLOOP_ENDPOINT_AUTHOR_KEY_FILE=/keys/model-author
OUTERLOOP_ENDPOINT_AUTHOR_MODEL=open-model
OUTERLOOP_ENDPOINT_AUTHOR_API=chat
OUTERLOOP_ENDPOINT_JUDGE_URL=https://llm.example.internal/v1
OUTERLOOP_ENDPOINT_JUDGE_KEY_FILE=/keys/model-judge
OUTERLOOP_ENDPOINT_JUDGE_MODEL=open-model
OUTERLOOP_ENDPOINT_JUDGE_API=chat,responses

OUTERLOOP_AUTHOR_BACKEND=hermes
OUTERLOOP_AUTHOR_ENDPOINT=author
OUTERLOOP_PANEL=verify:hermes:[endpoint=judge],review:codex:open-model[endpoint=judge]
REVIEW_HERMES_REPO=/opt/hermes-agent
OUTERLOOP_IMAGE=/opt/agent.sif
```

The files must be readable, nonempty, and private (`chmod 600`). Paths must be
absolute (`~` is expanded). Profile names start with a letter and contain only
letters, digits, and underscores; references are case-insensitive and their env
keys are uppercase. Only `_URL`, `_KEY_FILE`, `_MODEL`, and `_API` are forwarded by the
profile allowlist. URLs cannot contain credentials, a query, or a fragment.

A profile owns its model. `OUTERLOOP_AUTHOR_MODEL` may be omitted; if set, it must
match the profile's served model. Panel syntax is
`kind[:backend[:model[endpoint=profile]]]`, where kind is `verify` or `review`. Omitting the
model before `[endpoint=...]` takes the profile model. Native model IDs containing `@`, `/`, and `:` retain their
meaning. An endpoint author does not lend its profile or credential to an
implicit judge: select a judge profile or an explicit conventional model. Author
and judge credentials must differ, including when two files contain the same key.
Judges can share a judge profile.

For the standalone reviewer/summarizer contract, set `REVIEW_BACKEND`,
`REVIEW_ENDPOINT`, and optionally `REVIEW_MODEL`. The same profile keys apply;
`REVIEW_HERMES_REPO` is still required for Hermes. Endpoint selection replaces the
conventional reviewer key-variable and Hermes provider settings. Library callers
can pass `endpoint="judge"` to `build_harness` for any role specification.

## Backend wiring

The required `_API` declares a comma-separated list of supported APIs: `anthropic`,
`responses`, and/or `chat`. Preflight requires `anthropic` for Claude, `responses`
for Codex, and `chat` for Hermes. This declaration validates configuration; it does
not probe server support.
Use the base URL accepted by that backend's client. A server may expose all three
APIs, but if its API prefixes differ, define profiles with the appropriate URLs.
For example, an Anthropic-compatible client may need
`https://llm.example.internal` while OpenAI-compatible clients need
`https://llm.example.internal/v1`.

- **Claude Code:** `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN`; the profile
  model also sets the default Opus, Sonnet, Haiku, and small-fast model variables.
  Nonessential traffic is disabled. Vertex, Bedrock, and Foundry are disabled;
  API keys, ADC, and ambient authentication are excluded from the session env.
- **Codex:** the per-run `.codex/config.toml` selects a named provider with
  `base_url`, `env_key`, `wire_api = "responses"`, and
  `requires_openai_auth = false`. Custom-provider sessions skip OpenAI login.
  This follows the [official configuration reference](https://developers.openai.com/codex/config-reference).
  Configuration is tested here; live self-hosted Responses interoperability is
  not verified by this change.
- **Hermes:** the per-run `.hermes/config.yaml` selects a named
  `custom_providers` list entry with `base_url`, `key_env`, and
  `api_mode: chat_completions`. **`reasoning_echo: true` belongs under `model`,
  not inside the custom-provider entry.** No `--base_url` override is passed.
  `TERMINAL_CWD` selects the workspace while sample files stay in the private
  session home.

Keys are read by the orchestrator and placed only in the session environment.
Contained sessions receive them through `APPTAINERENV_*`; keys are never put in
argv or generated provider config files. Existing role budgets and containment
requirements still apply. Hermes authors require an endpoint profile and the
pinned source/runtime installation. Configure Hermes author settings directly in
`.env`; the interactive init wizard still provisions native Claude/Codex authors.

Start/tick/climb and reviewer resolution reject unknown profiles, invalid or
missing key files, unsupported backends, and model/profile mismatches before a
model session. Tick also checks judge credential separation and Hermes runtime
readiness before claiming work. These are local configuration checks, not probes
of a server's authentication, API support, or model availability.

## Hermes v2026.9.24 interface audit

Read both tags from a clone outside the worktree. The new tag resolves to
`f97608f178d1ffeca59860195ab7da295f7c8e5f`; the previous v2026.8.13 pin was
`f80f453ae0679347e38abc917c7f94f717bf96c5`.

- `run_agent.py` now delegates to
  [`agent/legacy_cli.py`](https://github.com/NousResearch/hermes-agent/blob/f97608f178d1ffeca59860195ab7da295f7c8e5f/agent/legacy_cli.py).
  **argparse replaced Fire.** The underscore flag spellings remain accepted
  (`query`, `model`, `max_turns`, `save_sample`, enabled/disabled toolsets).
  Embedded Fire quotes must be removed from comma-separated toolsets. The
  harness argv was exercised against the new source's actual parser function.
- [`run_agent.py::_save_sample_trajectory`](https://github.com/NousResearch/hermes-agent/blob/f97608f178d1ffeca59860195ab7da295f7c8e5f/run_agent.py#L1485)
  still writes `sample_<id>.json` in cwd, with `conversations`, timestamp, model,
  completed, and query. The ShareGPT turns still use `from`/`value`; assistant
  turns use `gpt`. The implementation moved into helpers; the envelope remains
  compatible. The new fixture was generated with the actual converter and
  sample writer on synthetic input (no inference).
- [`agent/reasoning_params.py`](https://github.com/NousResearch/hermes-agent/blob/f97608f178d1ffeca59860195ab7da295f7c8e5f/agent/reasoning_params.py)
  reads `model.reasoning_echo`, an opt-in absent at the old pin. It preserves
  reasoning on earlier turns within the native conversation. The kernel's
  existing resume-by-brief mechanism is unchanged; it is not a native replay of
  the full tool/reasoning history across separate harness invocations.
- [`hermes_cli/runtime_provider_custom.py`](https://github.com/NousResearch/hermes-agent/blob/f97608f178d1ffeca59860195ab7da295f7c8e5f/hermes_cli/runtime_provider_custom.py)
  still resolves named `custom_providers` and their `key_env` credential pointers.
  The newer `providers` mapping coexists with the supported legacy list form.
  `model.provider` and `model.default` in `config.yaml` remain the seed contract.
- [`toolsets.py`](https://github.com/NousResearch/hermes-agent/blob/f97608f178d1ffeca59860195ab7da295f7c8e5f/toolsets.py)
  still defines every toolset the harness uses: file, terminal, web, search,
  browser, computer_use, code_execution, delegation, cronjob, skills, memory.
- [`agent/prompt_builder.py`](https://github.com/NousResearch/hermes-agent/blob/f97608f178d1ffeca59860195ab7da295f7c8e5f/agent/prompt_builder.py)
  now recognizes `AGENTS.override.md` before `AGENTS.md`/`agents.md`, walks the
  git-root-to-cwd chain, and avoids accidental install-tree fallback. Its
  `.hermes.md`/`HERMES.md`, lowercase Claude/agents files, and cursor rules are
  also auto-load surfaces (several already existed at the old pin). Judge
  sanitization now covers all these names.

## Upgrade and rollback

The run-record schema is unchanged. New endpoint authors persist
`<served-model>[endpoint=<profile>]` in `author_model` and the resolved key-file path in
`author_key_file`. Resuming uses the saved selector, never the fleet's current
`OUTERLOOP_AUTHOR_ENDPOINT`. Retain referenced profile definitions until those
runs finish; changing a profile's URL/key changes that profile's routing, while
changing its served model makes old runs fail validation rather than silently
switch models.

Legacy records (including records missing backend/model fields), ended runs,
inbox messages, PR branches, and Hermes resume files remain readable without a
backfill. The fixture from kernel `5563c46` exercises the existing record writer's
output through load, repeat, and save/reload retry paths. Existing older run and
panel fixtures remain in the suite. The first tick preserves legacy routes and
validates endpoint settings for new work; it does not migrate parked sessions or
rewrite PRs/messages. Sanitization runs on disposable judge checkouts.

Rollback is safe for conventional runs. Finish endpoint-backed runs and remove
endpoint selections before rolling back to a kernel that does not understand
`[endpoint=profile]`; that kernel cannot safely resume their model selectors. Reinstall the
Hermes version required by the chosen kernel. The Hermes pin and argparse harness
change must ship together; overriding only the pin to the older Fire version is
not supported.

## Offline client validation limits

`test_exact_client_configuration_at_process_boundary` checks the exact argv,
environment, and provider config passed to all three clients, in contained and
uncontained sessions, including client failure. It checks that ambient vendor
credentials/routes are excluded and Codex endpoint sessions never invoke login.
This is configuration coverage, not evidence of the clients' network behavior.
The pinned Linux clients/runtime are not available in the offline test environment
(the installed Claude/Codex binaries have different versions, and Hermes lacks
its pinned runtime). No test claims to prove absence of client-internal vendor
fallback. That requires the pinned clients under network observation.

Legacy records with no model use native model defaults only. A missing native
Codex model fails preflight; it never adopts an endpoint. No backfill is needed.
