# Install

autoresearch is **self-hosted**. You run it, with your keys, on your compute,
against your repos. Nothing reports back to us and there is no service to sign
up for.

There are two things you can turn on, in this order. Level 1 takes about five
minutes and needs no bot account, no cluster, and no GPU. Do that first.

---

## Level 1 — advisory PR reviews (~5 minutes)

An automated reviewer comments on your pull requests. It never approves, never
blocks a merge, and never fails your build.

**You need:** an Anthropic API key. That's it.

**Step 1 — add the key as a repository secret.** Repo → Settings → Secrets and
variables → Actions → New repository secret. Name it `ANTHROPIC_REVIEWER_KEY`.
A spend-capped key is strongly recommended.

**Step 2 — add this file** to the repo you want reviewed, at
`.github/workflows/review.yml`:

```yaml
name: advisory-review
on:
  pull_request_target:
    types: [opened, reopened, labeled]
permissions:
  contents: read
  pull-requests: write
jobs:
  advisory:
    # a labeled event only runs for the autoresearch:review label (manual re-review)
    if: github.event.action != 'labeled' || github.event.label.name == 'autoresearch:review'
    uses: agentic-learning-ai-lab/autoresearch/.github/workflows/advisory-review-agent.yml@main
    with:
      bot_login: my-bot            # PRs by this login are never reviewed
    secrets:
      anthropic_reviewer_key: ${{ secrets.ANTHROPIC_REVIEWER_KEY }}
```

**Step 3 — open a pull request.** A comment appears within a minute or two.

**Optional — a second, independent opinion from an open-model backend.** Add
one more job to the same file (and an `OPENROUTER_API_KEY` repo secret). It
runs through the least-token split: the model session holds read-only
permissions; a separate posting job holds the write token. Each opinion posts
its own round, labeled, with the reviewer named in the round stamp:

```yaml
  second-opinion:
    if: github.event.action != 'labeled' || github.event.label.name == 'autoresearch:review'
    uses: agentic-learning-ai-lab/autoresearch/.github/workflows/advisory-second-opinion.yml@main
    with:
      bot_login: my-bot
      backend: hermes              # or codex (needs OPENAI_REVIEWER_KEY)
      model: openai/gpt-5.6-terra  # any OpenRouter id
      lens_label: second opinion — terra
    secrets:
      openrouter_api_key: ${{ secrets.OPENROUTER_API_KEY }}
```

A third opinion is one more block with a distinct `lens_id`. If the backend's
key is missing or expires, each triggering PR gets a visible "could not run"
stub naming that lens — a dead key is never silent.

That's the whole setup. Notes:

- `bot_login` is **required** — the reviewer refuses to run without it, because
  that's how it knows never to review its own (or your bot's) pull requests.
  If you have no bot yet, any placeholder login works.
- **If you forked this repo**, add `reviewer_repo: your-org/autoresearch` under
  `with:` — otherwise your fork's changes never run.
- **Pin the version in production**: `reviewer_ref: v0.1.0` (or a commit SHA).
  The default `main` moves.
- Silence it on one PR with the `autoresearch:no-review` label.
- Fork PRs are skipped by design: they must not reach your API key.
- Nothing from the pull request is ever executed. The workflow checks out the
  reviewer, not your PR's code.

**If no comment appears**, open the workflow run and read the log — the
reviewer logs why it stopped (missing key, skipped PR, model refusal) and
always exits successfully so your PR stays green.

---

## Level 2 — the benchmark climber

The agent proposes improvements to your code and opens PRs when a benchmark
improves. This needs a bot identity and somewhere to run experiments.

### 2a. Write a contract

`.autoresearch.yaml` at your repo root declares what "better" means and where
the agent may write:

```yaml
benchmarks:
  - name: my-benchmark
    command: uv run python -m mypkg.eval --json
    metric: success_rate
    direction: max          # max = higher is better, min = lower is better
budgets:
  gpu_hours_per_run: 8
  runs_per_week: 10
scope:
  allowed: [src/, tests/]   # the ONLY paths the agent may write
roadmap: docs/roadmap.md
```

**Check it before you push:**

```bash
uv run python -m autoresearch.contract_cli .autoresearch.yaml
```

It prints what the agent would be allowed to do, or exactly what is wrong.

Two things to get right:

1. Your `command` must be **deterministic and re-runnable**, and print the
   metric. That is what lets your own CI re-verify any improvement the agent
   claims, instead of taking its word.
2. `scope.allowed` should be as narrow as the work requires. Three paths are
   never writable no matter what you put there: the contract itself, your
   roadmap, and `.github/`.

For one artifact evaluated across many benchmarks (rather than independent
solvers), add a suite aggregate so a change is judged on the whole suite:

```yaml
suite:
  metric: mean_success_rate
  direction: max
```

### 2b. Create a bot identity

A GitHub machine user in your org, with a fine-grained PAT:

- Resource owner: **your organization** (not the bot's personal account —
  this is the step people miss)
- Repository access: only the repos you opt in
- Permissions: contents, pull requests, issues — **read and write**;
  **no workflow permission**
- Expiration: 90 days, with a rotation reminder

Add the bot as a direct collaborator (**Write**) on each opted-in repo. Don't
add it to a team — teams grant more than it needs and inherit future grants.

### 2c. Run the loop

The orchestrator is CPU-only and makes outbound connections only. Anywhere
that can reach GitHub and your LLM provider works.

- **Slurm cluster**: the tick can live in the queue as a self-resubmitting
  job — no daemon and no inbound SSH, which matters when your cluster requires
  2FA.
- **A VM or workstation**: run it on a timer.

Experiments run wherever your `compute` backend says. Slurm is the first
backend (arriving per the roadmap); the interface is small (submit a job, poll
for completion), so a CI runner, a cloud backend, or a hardware rig plugs in
the same way.

---

## Safety defaults

On by default. Think hard before changing any of them:

- The bot never merges and is never a code owner — your branch protection
  applies to it like any contributor.
- Agent sessions run with no credentials in their environment; pushes happen
  after the session ends.
- Only maintainer-authored issues and comments become tasks. Everything else,
  including PR descriptions and diffs, is data — never instructions.
- Budgets (tokens, dollars, GPU-hours, PRs per week) are enforced in code. A
  run that hits a cap dies.
- A pause file in the state branch stops the loop from anywhere with write
  access — no cluster login needed.

## Getting help

Open an issue. If you're reporting something the agent did, include its run
report — every run writes one, success or failure.
