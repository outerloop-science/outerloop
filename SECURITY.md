# Security and repo hygiene

This repo operates a bot with write access to lab repos and spends real money on
LLM APIs and GPU hours. Its history may go public with a release.

## Never commit

- The bot's PAT, LLM API keys, or any credential. They live as env vars or 0600
  credential files on the orchestrator host (Torch home dir), with one sanctioned
  exception: the reviewer role's separate, spend-capped API key in GitHub Actions
  secrets (fork PRs run without secrets). Agent sessions never see the PAT or
  billing keys (scrubbed environment); transcripts are secret-scanned before
  storage.
- Agent transcripts or run artifacts — they contain target-repo code
  (confidential until those repos publish). `runs/`, `transcripts/`, `outputs/`
  are gitignored; they stay on lab storage.
- Personal cluster paths or netids; binaries >500 KB.

## Operational rules

- The bot is never a code owner anywhere and never merges code; its PRs pass the
  same gates as everyone's. Sole exception: the prose-only notebook repo, where
  bot PRs auto-merge on a green secret-scan check.
- Budget caps (tokens, dollars, GPU-hours, PRs/week) are enforced in code; a run
  that hits a cap dies.
- Kill switches, fastest first: set the pause sentinel on the state branch (any
  write access, no cluster login — the chain self-terminates next tick); suspend
  the bot account; `scancel` the queued chain and GPU jobs (needs 2FA login).

## If a credential leaks

1. Rotate immediately; the bot PAT and API keys are the crown jewels here.
2. Tell the PI. Check the run ledger for anything the leaked credential touched.

## Reporting a vulnerability

Email the PI: mengye@nyu.edu.

## Codex 0.160.0 lifecycle hooks and project configuration

Every contained Codex role (authors and judges, fresh and resumed sessions)
read-only binds the packaged `codex_requirements.toml` at
`/etc/codex/requirements.toml`, setting `allow_managed_hooks_only = true`.
The packaged policy replaces the requirements file at that container path;
other managed config sources remain subject to Codex's normal precedence.
It contains no managed hooks. Kernel sessions never pass the hook-trust bypass
flag. Each launch atomically replaces `$CODEX_HOME/config.toml` with the
kernel's provider config (or an empty native-provider config), removing old
hook trust without deleting session history. Writes refuse symlinked home
and config directories and replace, rather than truncate, linked config files.

The [0.160.0 requirements loader](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/config/src/loader/mod.rs)
reads Unix requirements from `/etc/codex/requirements.toml`, not `CODEX_HOME`.
Its alternate paths are internal test overrides, not CLI/environment settings.
Putting `allow_managed_hooks_only` in ordinary config does not enforce it.
Consequently, uncontained sessions use `-c features.hooks=false` and
`-c features.plugins=false` after other config arguments, disabling managed
hooks too. Plugins must also be disabled because Codex exempts built-in plugin
cleanup hooks from the hooks feature switch. This fallback needs no privileged
system write. A conflicting managed feature
requirement causes a config error, rather than silently enabling hooks.

**Project-config finding:** the initial loader gates project config on trust,
but the [embedded app-server's thread startup](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/app-server/src/request_processors/thread_processor.rs)
automatically trusts an unspecified-trust writable cwd and reloads config.
Under the kernel's `danger-full-access` launch flags, `.codex/config.toml` is
therefore loaded even with a fresh session home. The real Darwin 0.160.0 test
observed its model setting taking effect when the kernel omitted `--model`.
Project `model_provider` and `model_providers` are filtered by the loader;
the fixture's attempted provider redirect did not take effect. CLI sandbox
settings have higher precedence than project config. Project configuration
can still influence agent behavior and configure process-launching features
such as MCP servers. This is not evidence of escape from Apptainer, but it
means project config is not an inert input or a containment boundary. No
containment redesign is included in this upgrade.

Hook discovery follows the enabled config layers: `.codex/hooks.json` and
`[hooks]` in config TOML, including ancestor/project-root layers; linked Git
worktrees may also source hooks from the root checkout. The
[hook discovery engine](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/hooks/src/engine/discovery.rs)
filters non-managed sources before loading them under managed-hooks-only.
Trust hashes otherwise come from user/session hook state, not project state.

`tests/test_codex_hooks.py` tests the policy, fresh/resume launch wiring,
provider variants, and sanitization in ordinary CI. Its real-binary mutation
probe is opt-in and uses a local mock Responses server, without credentials
or model spend:

```sh
OUTERLOOP_TEST_CODEX=/absolute/path/to/codex uv run pytest -q -n0 tests/test_codex_hooks.py
```

On Linux, also set `OUTERLOOP_TEST_CODEX_IMAGE` to an Apptainer image to exercise
the managed-policy bind and its removal mutation. The uncontained probe
removes only the CLI hook guard. Both controls inject identical persisted
trust after the independently tested config scrub: guarded fresh/resume runs
must leave no marker, and removing the guard must create the marker. The
Darwin probe passed; the Linux/Apptainer probe was not run on the Mac.
