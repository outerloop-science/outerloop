# Add the Outerloop reviewer to your repo

An advisory AI code reviewer for your pull requests. It posts findings as a PR
review — it **never approves, never blocks your CI, and never runs your PR's
code** (it reads the diff as data). It reviews both same-repo and fork PRs; the
model key lives only in a read-only, write-tokenless job, so a fork PR cannot
reach it.

## Three steps

1. **Pick the bot identity and get a model key.** Choose the account whose PRs
   should be skipped (so the reviewer never reviews its own bot's PRs) — that's
   the `bot_login`. Get an API key for your chosen backend (an Anthropic key for
   `claude`).

2. **Add the workflow.** Copy [`examples/review.yml`](../examples/review.yml) to
   `.github/workflows/review.yml` in your repo and set `bot_login` to your bot's
   login. It calls the public reviewer with `uses:` — you never vendor the
   reviewer code.

3. **Set the secret.** Add a repository secret `ANTHROPIC_REVIEWER_KEY` with your
   Anthropic key (`Settings → Secrets and variables → Actions`). For `hermes` or
   `codex`, set `OPENAI_REVIEWER_KEY` / `OPENROUTER_API_KEY` instead and point
   the `secrets:` block at it.

That's it. Open a PR and the reviewer posts a round.

## Using it

- **First round** runs automatically when a PR opens.
- **Re-review** after you push fixes by (re)applying the `outerloop:review` label
  — the round stamp increments so you can follow the fix→review→fix loop.
- Findings are **advisory**: the code owner decides. The `outerloop:no-review`
  label opts a PR out entirely.

## Backends

`backend: claude` (default) uses Anthropic; `hermes` and `codex` are also
supported — set the matching key and, for `hermes`, the provider. See the inputs
in
[`advisory-review-agent.yml`](../.github/workflows/advisory-review-agent.yml).

## Safety

- The reviewer checks out your PR head **read-only and never executes it** — no
  build, no test, no install of your PR tree. It only reads the diff.
- The job holding the model key has **no write permission**; a separate job with
  `pull-requests: write` runs no model and only posts the review.
- The reviewer is **advisory** — it cannot approve, merge, or fail your CI. The
  most a crafted PR can do is elicit a misleading advisory comment, which you
  read with the same skepticism as any review.
