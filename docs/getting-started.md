# Getting started

This page walks you through one Outerloop run end to end: install the package, declare a contract in your repository, run guided setup, and watch the loop make its first climb. You need one machine, a GitHub repository you administer, and separate Anthropic credentials for the author and the review judges: API keys, or subscription tokens from `claude setup-token`.

The tutorial uses the simplest supported setup: everything runs on your machine. [Install](install.md) covers Slurm clusters, upgrades and the other deployment shapes.

To watch a run before you write a contract, the [quickstart repository](https://github.com/outerloop-science/quickstart) has two CPU benchmarks and runs without GitHub or a cluster; its README has the three commands.

## Use from your coding agent

Install the skills-only Outerloop plugin in Claude Code:

```text
/plugin marketplace add outerloop-science/outerloop
/plugin install outerloop@outerloop
```

Or in Codex:

```bash
codex plugin marketplace add outerloop-science/outerloop
codex plugin add outerloop@outerloop
```

Both use the repository's `.claude-plugin/marketplace.json` and the same skills;
Codex also supports this marketplace layout. Open a new agent session in your
target repository and say "use outerloop on this repo". The setup skill installs
`outerloop-science`, helps validate a contract, and offers local forge with local
or Slurm compute. On a laptop, the plugin runs CPU benchmarks locally without
needing GitHub or a cluster. For GPU benchmarks, SSH to the cluster's login node
and run your coding agent there; the resident loop submits all work to Slurm.
Model access is still needed;
enter secrets only in the CLI's private prompts. Setup does not start the loop.
Ask separately to start, inspect, message or stop a run. The plugin drives the
CLI; budgets, measurement and forbidden paths remain enforced by the kernel.

## Before you start

- Python 3.12 or later on a Linux machine, or any machine you can leave a terminal open on.
- `uv` on PATH. Install it with `curl -LsSf https://astral.sh/uv/install.sh | sh` and check `uv --version`. Every evaluation runs through `uv run`, and `outerloop start` refuses to launch without it.
- A GitHub repository you administer, so you can install a GitHub App on it and merge pull requests.
- A benchmark command in that repository that prints a JSON metric, such as `{"success_rate": 0.42}`.
- An Anthropic credential for the author model and a separate one for the review judges: an API key, or a subscription token from `claude setup-token`. Spend-capped keys are recommended.

The loop opens pull requests, comments and pushes branches as a GitHub identity that is not yours. On the way you create that identity with `outerloop init`.

## Step 1: install the package

```bash
pip install outerloop-science
outerloop --version
```

The second command prints the installed version, such as `outerloop 0.3.0`. If `uv` is your package manager, `uv pip install outerloop-science` works the same way.

## Step 2: write the contract

Add a file named `.outerloop.yaml` to the root of the repository you want improved. It declares what "better" means and the only paths an agent may write:

```yaml
benchmarks:
  - name: my-benchmark
    command: uv run python -m mypkg.eval --json   # prints {"success_rate": 0.42}
    metric: success_rate
    direction: max          # max = higher is better, min = lower is better
budgets:
  gpu_hours_per_run: 8
  runs_per_week: 10
scope:
  allowed: [src/mypkg/solver/]   # the ONLY paths the agent may write
roadmap: docs/roadmap.md   # what the agents read for direction; never written
```

Keep the benchmark's own code, here `mypkg/eval.py`, outside `allowed`. Only
the contract, the roadmap, `.github/` (and a guidance directory you declare)
are forbidden everywhere, so an eval that sits inside the solver's paths is a
ruler the agent can edit.

Check the file before you push it:

```bash
uv run python -m outerloop.contract_cli .outerloop.yaml
```

A valid contract prints what the agent will be allowed to do:

```
✓ .outerloop.yaml is valid

Benchmarks the agent will try to improve:
  • my-benchmark: success_rate (↑ higher is better)
    $ uv run python -m mypkg.eval --json

The agent may write only to:
  • src/mypkg/solver/

Always forbidden, whatever the contract says:
  • .github
  • .outerloop.yaml
  • docs/roadmap.md

Budget: 8.0 GPU-hours per run, 10 runs per week
```

An invalid one names the field and the problem. Commit the file and push.

Two decisions matter most. Your `command` must be deterministic and re-runnable and must print the metric, because that is what lets your CI re-verify an improvement instead of taking the agent's word. And `scope.allowed` should cover the solver's code only, with the benchmark's eval outside it. Every field is described in [the contract](contract.md).

## Step 3: run guided setup

```bash
outerloop init
```

The wizard asks for the deployment settings and writes `~/.config/outerloop/.env`, plus the author's key file, all owner-only. Answer like this:

- `local` for the compute backend.
- Your `owner/repo` for the target.
- Enter for the author backend, keeping `claude`.
- Enter for the author model to take the backend's default, or name one.
- The model ID your key serves for the Claude model.
- Your Anthropic API key or `claude setup-token` token when it asks for the author's key. The prompt hides it.
- Enter for `app` at the auth prompt.

On Linux with a container runtime installed ([Apptainer](https://apptainer.org), or [enroot](https://github.com/NVIDIA/enroot) on a pyxis cluster), init also offers to download the agent image, about 200 MB, so sessions run contained. Accept unless you have a reason not to. Without an image, on macOS for example, runs stay uncontained and the verification panel is off, and a pull request opened without a panel says so.

The App flow is three steps, each printed by init:

1. init prints a URL. Open it in any browser where you are signed in to GitHub, and click **Create GitHub App**. The page that follows shows a one-time code. Paste the code back into init.
2. Open the install URL init prints, choose **Only select repositories**, pick your repo, and click **Install**. Press Enter in init once GitHub shows the App as installed.
3. init checks that the App can write your repo, then prints `wrote ~/.config/outerloop/.env` and `next: outerloop start`.

The bot's login is `<slug>[bot]`, recorded in the settings file. The kernel holds the App's key and performs every GitHub write on the agents' behalf. An agent session holds only its own model key. A contained session gets no GitHub credential in its environment and cannot read your key files; an uncontained local session runs as your user and can read any file you can, key files included.

The wizard does not collect the judges' key. With the image accepted above, the review panel runs, and its judges need their own Anthropic key at `~/.config/outerloop/verifier_key`, never the author's. Create it before starting the loop:

```bash
printf '%s' "sk-ant-..." > ~/.config/outerloop/verifier_key
chmod 600 ~/.config/outerloop/verifier_key
```

Without that key the tick refuses to claim issues and names the panel preflight in its log. Without an image the panel is off and no judge key is needed.

## Step 4: start the loop

```bash
outerloop start
```

With no Slurm scheduler on the machine, the loop runs in the foreground:

```
local loop: state in /home/you/.outerloop; Ctrl-C stops it, the records resume it
```

Leave it running in a terminal, or under `tmux` on a headless machine. Ctrl-C stops it at any time, and starting it again continues the same work from the saved records.

## Step 5: watch the first run

Every tick, the loop scans your repo and starts a climb on the benchmark least recently attempted, within the contract's weekly budget. The default cadence is 30 minutes, so the first run starts within half an hour. Each tick prints a summary line in the loop's output:

```
tick done: paused=False coalesced=False swept=1 woken=0 deferred=0 reaped=0 stuck=0 ...
```

From another terminal, check progress without touching the loop:

```bash
outerloop status
```

Each active run gets one line, ending with its GPU-hour spend against the contract's budget:

```
my-benchmark-20261016-143201-agent-01 target=you/my-repo agent=agent-01 \
state=running phase=candidate author=claude/claude-sonnet-4-5 \
overridden=no GPU-hours=0.5/8.0 session-cost=$1.2044 unpriced-sessions=0
```

The run's report lands on your repo: every attempt is written up, including the ones that failed, and a pull request opens only when your benchmark improves by more than noise and the review panel approves. A run with the panel off opens pull requests that say so. You merge the pull request like any other, and your branch protection applies to the bot exactly as to a human contributor.

Stop the loop with Ctrl-C when you are done, or leave it running and read [operate](operate.md) for steering runs, setting limits and pausing work.

## Next steps

- [Install](install.md) for Slurm clusters, the login-node loop, upgrades and two instances on one account.
- [The contract](contract.md) for every knob: significance floors, eval jobs, width and pacing.
- [Operate](operate.md) for status, messages, endings, operator ceilings and the pause switches.
- [How it works](how-it-works.md) for the trust boundary between the kernel and its agents.
