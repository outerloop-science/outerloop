# Agent syscalls

The syscall tool is the one command-line surface an agent uses to talk to
the kernel. The kernel installs it at `.outerloop/syscall` in the workspace,
so any harness can call it from any directory. This page lists every verb,
who may call it, its arguments and what it refuses. The design behind the
surface is on [the how-it-works page](../how-it-works.md).

A verb is invoked as `python .outerloop/syscall <verb>`. A bad invocation
prints the reason to stderr, exits with code 2 and stages nothing. Which
verbs a role may call is set by its role specification, and the brief names
them. The tool's checks are fast in-session feedback only. The kernel
re-validates every field after the session ends, so a role that writes its
request file by hand is still fully checked.

| Verb | Who | What it does |
| --- | --- | --- |
| [`launch`](#launch) | author | stage a job to run outside the sandbox |
| [`sleep`](#sleep) | author | commit the staged launches and end the session |
| [`message`](#message) | author | send to the PR thread, yourself or a sibling |
| [`submit`](#submit) | author | stage a submit for measurement and review |
| [`stop`](#stop) | author | end the run, or withdraw its open PR |
| [`finding`](#finding) | judge | record one finding |
| [`conclude`](#conclude) | judge | commit the verdict and end the session |
| [`status`](#status) | any | show staged syscalls and remaining budget |
| [`cancel`](#cancel) | any | discard the staged request |
| [`siblings`](#siblings) | any | what the other agents are working on |
| [`queue`](#queue) | any | the kernel's jobs in the cluster queue |
| [`history`](#history) | any | this run's launches and how each ended |
| [`reports`](#reports) | any | past attempts' research reports |
| [`sync`](#sync) | any | refresh origin refs now, inside this session |

## Author verbs

### launch

Stages one job to run outside the sandbox, on a frozen snapshot of the
workspace. Multiple launches accumulate until `sleep` commits them.

| Argument | Default | Constraint |
| --- | --- | --- |
| `--name` | required | 1-32 chars of `a-z`, `0-9` and `-`, starting with a letter or digit; unique among staged launches |
| `--minutes` | 30 | positive integer; clamped to 240 |
| `--why` | empty | at most 200 chars; shown to every agent in `queue` |
| `--array` | 1 | positive integer; clamped to 16; each task runs with `SWEEP_INDEX` 0..N-1 |
| `--concurrency` | 0 | non-negative; applies only with `--array`, ignored otherwise; at most K tasks at once; the contract may cap it |
| `--artifact` | none | repeatable, at most 8; a repo-relative file path, no traversal |
| `command` | required | after `--`; at most 2000 chars |

Refused when a stop is staged, when there is no command after `--`, when a
staged launch already uses the name, or when more than 8 launches would be
staged per sleep.

### sleep

Commits the staged launches and any staged submit, then ends the session.
The kernel submits each launch as a cluster job and wakes the same session
with the results. Takes no arguments. A sleep with no launches is a
checkpoint, and it still spends one sleep from the budget. Refused when a
stop is staged.

### message

Sends a message to the run's PR or issue thread (the default), to yourself,
or to a sibling agent. The message is committed immediately and posted or
delivered by the kernel.

| Argument | Default | Constraint |
| --- | --- | --- |
| `text` | none | the message text, or read it from `--file`; 1 to 20000 chars |
| `--file` | none | a file the text is read from |
| `--to` | `thread` | `thread`, `self` or `agent-NN` |
| `--reply-to` | none | the sequence number of an inbox message |
| `--show` | none | print a message chain instead of sending |

Refused when `--show` is combined with a message, or when the destination is
not `thread`, `self` or `agent-NN`. Inter-agent messages disabled by the
contract refuse a sibling destination, and `--reply-to` must name a message
in the inbox. At most 8 messages may be staged, within 65536 bytes.

### submit

Stages a submit. On the next `sleep` the author's tree is sealed, measured
by the gate and read by the review panel. A credited verdict publishes it.

| Argument | Default | Constraint |
| --- | --- | --- |
| `--report` | empty | a markdown file, 1 to 8000 chars; becomes the PR's research report |
| `--minutes` | none | walltime for each paired gate eval; positive; clamped to 1440 |
| `--claimed-value` | none | a finite number; required for self-reported verification |
| `--claimed-baseline` | none | a finite number; required for self-reported verification |

Refused when a stop is staged, or when a supplied report file is empty or
over the limit. Omitting `--report` is valid. Self-reported verification
additionally requires the two claimed values.

### stop

Ends the run. Whether it ends outright depends on the PR: a run with an open
PR returns to review and parks with it, whatever `--report` says, while a
run without one ends with no PR, and a report cannot create one. The report
supplies the final text posted to the thread. Committed immediately.

| Argument | Default | Constraint |
| --- | --- | --- |
| `--report` | empty | a file containing the final report, 1 to 8000 chars |
| `--withdraw` | none | a reason of 1 to 20000 chars; closes the open PR |

Refused when a submit is staged or committed, when a sleep or launch is
staged (those already end the turn), or when `--withdraw` is used with no
open PR.

## Judge verbs

### finding

Records one finding. Findings accumulate until `conclude` commits the
verdict.

| Argument | Default | Constraint |
| --- | --- | --- |
| `--file` | required | non-empty |
| `--line` | none | 1-indexed; omit for a non-local finding |
| `--confidence` | required | `low`, `medium` or `high` |
| `--summary` | required | 1 to 6000 chars |
| `--detail` | required | 1 to 6000 chars |
| `--kind` | `note` | `change`, `suggestion`, `question` or `note` |
| `--blocking` | off | a confirmed defect that gates merge |
| `--category` | empty | at most 6000 chars; the verifier's gaming taxonomy |

Refused when the confidence, kind, file, summary or detail is empty or
invalid, when more than 200 findings would be staged, or when the finding
would leave too little room for `conclude --notes` within the 1 MB verdict
the kernel reads.

### conclude

Commits the verdict with all staged findings and ends the session. This is
the judge's final answer.

| Argument | Default | Constraint |
| --- | --- | --- |
| `--notes` | empty | at most 6000 chars; the summary the reader sees |

## Shared verbs

### status

Prints the staged syscalls, the staged launches with their reasons, and the
remaining budget as the kernel last wrote it. Takes no arguments.

### cancel

Discards the staged request and any committed syscall, so nothing is acted
on. Takes no arguments.

### siblings

Prints what the other agents of the fleet are working on, as of this wake.
Takes no arguments.

### queue

Prints the kernel's jobs in the cluster queue, every agent's, with each
launch's one-line `--why`. Answered by the session watcher. When no watcher
runs, the queue is visible again at the next wake.

| Argument | Default | Constraint |
| --- | --- | --- |
| `--wait` | 30 | seconds to wait for the kernel's answer |

### history

Prints this run's launches so far, grouped by sleep, with each job's end
state and exit code. Answered by the session watcher like `queue`.

| Argument | Default | Constraint |
| --- | --- | --- |
| `--wait` | 30 | seconds to wait for the kernel's answer |

### reports

Prints the research-report archive the kernel fetched for this run. With no
names, one summary line per report, newest first. With names, those reports
in full, several at once.

| Argument | Default | Constraint |
| --- | --- | --- |
| `names` | none | report file names from the summary list |

Refused when shared reports are disabled by the contract's channels, or
when a name is not in the summary list.

### sync

Asks the kernel to fetch fresh `origin/*` refs and waits inside this
session's own clock. A timeout is not an error; the refs refresh free at
the next wake regardless.

| Argument | Default | Constraint |
| --- | --- | --- |
| `--minutes` | 35 | how long to wait before giving up; 0 probes and returns |

## Staging and committing

The verbs differ in how they reach the kernel. `launch`, `submit` and
`finding` stage into `.outerloop/request.json`, a file the agent may rewrite
until it commits. `message`, `sleep`, `stop` and `conclude` write the typed
request to `.outerloop/syscall.json`, the file the kernel reads after the
session ends. `sleep` and `conclude` also discard the staged file, so
nothing is committed twice. Building a request and committing it are
separate acts: `status` shows what is staged, and `cancel` discards it.
