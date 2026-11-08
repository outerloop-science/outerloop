# Operator commands

Every command of the `outerloop` command line, for the person who deploys
and steers runs. Run `outerloop init` once before `outerloop start`; from
then on the commands below are the whole operator surface. Every section
lists its command's flags with their defaults.

Every command that takes `--root` resolves it the same way. The flag wins,
then `OUTERLOOP_ROOT` in the environment, then the operator settings file,
then `~/.outerloop`. Commands read the settings file
(`~/.config/outerloop/.env`, or the file `OUTERLOOP_ENV_FILE` names) only
once at launch. The file must be owned by you and not group- or
world-writable, or it is refused.

## Command summary

| Command | What it does |
| --- | --- |
| [`start`](#start) | start the loop: the resident tick on Slurm, the local loop elsewhere |
| [`init`](#init) | guided setup: write the settings file and the credential files |
| [`upgrade`](#upgrade) | upgrade the installed package |
| [`harness`](#harness) | inspect or upgrade harness installations |
| [`tick`](#tick) | run one tick now, or keep ticking in the foreground |
| [`status`](#status) | show local runs and endpoint outages, read-only |
| [`pr`](#pr) | list, comment on, merge or close local forge pull requests |
| [`limits`](#limits) | show live operator ceilings and fleet GPU usage |
| [`message`](#message) | leave a note in a run's inbox, delivered at its next wake |
| [`end`](#end) | request an operator ending at the next tick |
| [`rebind`](#rebind) | request the slot's current author for the next leg |
| [`permissions`](#permissions) | check and update the App's required permissions |
| [`migrate-ledger`](#migrate-ledger) | seed research-log from a pinned main ledger |

`outerloop --version` prints the installed version.

## start

Submits the resident tick to Slurm, or runs the local loop in the foreground
where there is no `sbatch`. With `sbatch` on PATH the resident job is
submitted with a singleton dependency, so two starts can both submit but
only one chain ever runs. Without it, or with `OUTERLOOP_COMPUTE=local` or
`--local`, the loop runs in the foreground and Ctrl-C stops it. Before
submitting, start checks the author CLI, the configured models and the tick
lease, and refuses with the reason if any fails.

```
outerloop start [--root ROOT] [--account ACCOUNT] [--partition PARTITION]
               [--local] [--tick-host {resident,login}] [--dry-run]
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--root` | `~/.outerloop` locally | state root; on Slurm it must be on the shared filesystem |
| `--account` | unset | Slurm account; unset bills your default association |
| `--partition` | unset | Slurm partition; unset lets Slurm choose. `a,b` lists two, whichever frees up first |
| `--local` | off | run the local loop even where `sbatch` exists |
| `--tick-host` | `resident` | run the Slurm tick in a resident job or on this login host |
| `--dry-run` | off | print the command it would run and exit |

Every flag defaults from the settings file. On success the command prints
the job id, the walltime and where the logs land.

## init

Guided setup. It asks for anything not given as a flag, checks the
credentials, and writes `~/.config/outerloop/.env` plus the credential
files. See [the install guide](../install.md) for the whole procedure.

```
outerloop init [--compute {slurm,local}] [--target TARGET] [--root ROOT]
              [--forge {github,local}] [--forge-source FORGE_SOURCE]
              [--account ACCOUNT] [--partition PARTITION] [--pat-file PAT_FILE]
              [--github-app] [--personal] [--app-name APP_NAME] [--org ORG]
              [--author-backend AUTHOR_BACKEND] [--no-install-harness]
              [--author-model AUTHOR_MODEL] [--claude-model CLAUDE_MODEL]
              [--container-runtime {apptainer,enroot}]
              [--image IMAGE | --no-image] [--author-key-file AUTHOR_KEY_FILE]
              [--yes] [--force]
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--compute` | prompted | `slurm` or `local` |
| `--target` | prompted | the repo the agents work on, `owner/repo` |
| `--root` | prompted | state root; on Slurm it must be on the shared filesystem |
| `--forge` | prompted | `github`, or `local` for a bare repository under the state root |
| `--forge-source` | empty | with `--forge local`: a repository path or URL to seed from; for private repositories, use a local path or a bare clone made with `git clone --bare` on a machine with access and copied over (e.g. `/path/to/repo.git`) |
| `--account` | blank | Slurm account; blank bills your default association |
| `--partition` | blank | Slurm partition; blank is Slurm's default, `a,b` a list |
| `--pat-file` | prompted | path to an existing file holding a PAT |
| `--github-app` | off | create your own GitHub App in a browser instead of a PAT |
| `--personal` | off | post as yourself with your own token (`gh auth token` unless `--pat-file`); GitHub only |
| `--app-name` | `outerloop-<owner>` | name for the created App |
| `--org` | your account | create the App under this org |
| `--author-backend` | `claude` | the climbing author's backend: `claude`, `codex` or `hermes` |
| `--no-install-harness` | off | skip installing a missing author CLI or Hermes runtime |
| `--author-model` | prompted | the climbing author's model |
| `--claude-model` | prompted | model for every Claude role; required, no default |
| `--container-runtime` | `apptainer` | what contains sessions and evals on Linux: `apptainer` or `enroot` (asked when enroot is installed and apptainer is not) |
| `--image` | the published one | path to the agent image for sessions and evals, a `.sif` for Apptainer or a `.sqsh` for enroot |
| `--no-image` | off | no image: local runs stay uncontained even if one is on disk |
| `--author-key-file` | prompted | path to an existing file holding the author's model key |
| `--yes` | off | non-interactive: use flags, do not prompt |
| `--force` | off | overwrite an existing config without asking |

## upgrade

Upgrades the installed package in this environment with pip, then reminds
you to restart the loop to pick up the new code. On Slurm the resident tick
self-updates through `OUTERLOOP_AUTO_UPDATE`. This command is the
equivalent for a local install. After upgrading it runs
`outerloop permissions` in a fresh process, so the permission check runs
the new code.

```
outerloop upgrade [--pre] [--dry-run]
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--pre` | off | include pre-releases even once a stable exists |
| `--dry-run` | off | print the pip command and exit |

## harness

Inspects or upgrades the harness installations the deployment uses, meaning
the author CLIs, their versions and pins, and drift from the settings. It
owns its own subcommands.

```
outerloop harness {status,upgrade}
```

| Subcommand | What it does |
| --- | --- |
| `status` | report pins, overrides, installed paths and drift |
| `upgrade [--root ROOT] [--used] [NAME ...]` | verify new installations before switching paths; `--used` limits to configured backends, `NAME` to the named harnesses |

## tick

Runs one tick of the loop, servicing the open runs, launching new work and
waking parked runs. The chain runs a tick every cadence on its own. This
command runs one by hand, or keeps ticking in the foreground with `--loop`
(the local and login loops are exactly this).

```
outerloop tick --root ROOT [--grace-s GRACE_S] [--lease-ttl-s LEASE_TTL_S]
              [--min-free-gb MIN_FREE_GB] [--loop] [--cadence-min CADENCE_MIN]
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--root` | required | state root on the shared filesystem |
| `--grace-s` | 900 | seconds a finished experiment's delivery job gets before the sweep steps in |
| `--lease-ttl-s` | 5400 | seconds after which a held run lease counts as stale when the scheduler cannot confirm the holder |
| `--min-free-gb` | 10 | skip launching new work below this many GB free on the state filesystem |
| `--loop` | off | run a tick every cadence in the foreground |
| `--cadence-min` | 30 | minutes between `--loop` ticks |

## status

Shows the local runs, their state and any model endpoint outages. Read-only
and safe to run at any time.

```
outerloop status [--root ROOT] [--json]
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--json` | off | print structured JSON instead of text |

## pr

Reviews and merges pull requests on a local deployment
(`OUTERLOOP_FORGE=local`). It reads the settings the way `status` does, and
refuses on a GitHub deployment: use GitHub there.

```
outerloop pr list   [--root ROOT] [--target TARGET]
outerloop pr show   N [--root ROOT] [--target TARGET]
outerloop pr comment N [TEXT ...] [--file FILE] [--root ROOT] [--target TARGET]
outerloop pr merge  N [--root ROOT] [--target TARGET]
outerloop pr close  N [--root ROOT] [--target TARGET]
```

| Subcommand | What it does |
| --- | --- |
| `list` | pull requests in number order, with state, head, base and title |
| `show N` | the record and its comment thread |
| `comment N` | append a comment written as you, with owner standing |
| `merge N` | merge at the current head, refusing if it moved or conflicts |
| `close N` | close the pull request |

`--root` defaults to `OUTERLOOP_ROOT`, then the settings file, then
`~/.outerloop`. `--target` defaults to `OUTERLOOP_TARGET`. The command needs
access to the shared filesystem that holds the bare repository.

## limits

Shows the live operator ceilings (session and job limits, fleet GPU usage)
the orchestrator is enforcing right now.

```
outerloop limits [--root ROOT]
```

## message

Appends one note from you to a run's inbox. The run sees it at its next
wake, so a run sleeping on jobs sees it when they finish, and a paused loop
delivers it when it resumes. No new wake is scheduled.

```
outerloop message [--root ROOT] [--file FILE] run_id [text ...]
```

| Argument | Default | Meaning |
| --- | --- | --- |
| `run_id` | required | the run to message |
| `text` | required | the message text, or read it from `--file` |
| `--file` | none | read the message text from this path |

The note is capped at 6,000 characters, and the command refuses a run that
has already ended.

## end

Requests that a run end as operator at the next tick. The run's pending
jobs are cancelled. A session in flight finishes its leg and its publish is
refused.

```
outerloop end [--root ROOT] [--note NOTE] run_id
```

| Argument | Default | Meaning |
| --- | --- | --- |
| `run_id` | required | the run to end |
| `--note` | empty | the reason for ending the run, kept with the record |

## rebind

Requests that a run's slot rebind to its current author at the next leg, or
cancels a pending request. A paused loop must resume to process it.

```
outerloop rebind [--root ROOT] [--note NOTE] [--cancel] run_id
```

| Argument | Default | Meaning |
| --- | --- | --- |
| `run_id` | required | the run to rebind |
| `--note` | empty | the reason, kept with the record |
| `--cancel` | off | remove a pending rebind request |

## permissions

Checks the GitHub App's required permissions and points to the settings
page that grants the next missing one. With a PAT it reports that no App
permissions are needed.

```
outerloop permissions [--open]
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--open` | off | open the next permission settings page in a browser |

## migrate-ledger

Seeds a research-log branch from a pinned main ledger, for a target whose
history predates the research log.

```
outerloop migrate-ledger --target TARGET --main-sha MAIN_SHA [--force] [--dry-run]
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--target` | required | the target repo, `owner/repo` |
| `--main-sha` | required | the full current default-branch commit SHA |
| `--force` | off | replace an existing branch ledger |
| `--dry-run` | off | print the table without writing |

### `trajectory <run-id> [--verify] [--json] [--root PATH]`

Read the kernel's `runs/<run-id>/trajectory.jsonl` timeline. Text output shows
sequence, time, type, actor and short event data. `--verify` checks the hash chain
and sequence gaps; damaged chains exit 1. Missing legacy files show "no trajectory"
and exit 0. JSON output is an event array, or an object with `events` and `problems`
when verification is requested. This command is read-only.
See [Run trajectory](../design/trajectory.md) for the format and limits.
