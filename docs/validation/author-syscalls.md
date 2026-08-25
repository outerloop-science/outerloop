# Validating the author sleep/wake substrate (live)

The author launch/sleep syscalls (`.autoresearch/syscall`, park→wake, artifact
copy-out) are **fake-tested only** — never run on a real cluster. Before the
substrate goes live (and before the `submit` verb, the env-flag decommission, or
the `climb → attempt` rename build on it), run this once on Torch and watch the
whole loop.

## Arm it (one climb only)

`--author-syscalls` arms the feature for a SINGLE climb (ORed with the
`AUTORESEARCH_AUTHOR_SYSCALLS` env flag), so you do **not** arm the whole tick.
Two conditions must both hold:

- **Dispatch coords** (`--image` + account/partition): launches are jailed
  Slurm jobs, so without them nothing launches.
- **A launch budget**: `depth_k` (default 1 = one launch) caps launches; raise
  it in the target's `.autoresearch.yaml` to test more.

Then run one climb by hand (not via the tick):

```bash
source env.sh
uv run python -m autoresearch.climb \
  --target <org/repo> --benchmark <a-cheap-benchmark> \
  --run-root <run-root> --image <image.sif> \
  --author-syscalls \
  <the account/partition/limit args the tick normally passes>
```

Pick a **cheap** benchmark and a task where running an experiment is natural, so
the author actually calls `syscall launch` + `sleep` (the tool is advertised in
the brief only when armed).

## Watch the whole loop

The lifecycle to confirm, in order:

1. **Tool installed:** `<run_dir>/ws/.autoresearch/syscall` exists, and
   `.autoresearch/budget.json` shows `depth_k` / `sleep_k`.
2. **Author sleeps:** the session ends having written
   `.autoresearch/syscall.json` (`type: "sleep"`, one+ launches).
3. **Park:** the run record is `waiting`, `stage.phase == "author-sleep"`, with
   `stage.afterany` naming the submitted launch job(s), `syscall_launches`, and
   `launches_used` / `sleeps_used` counts.
4. **Launch job runs:** `<run_dir>/eval-launch-<name>/` fills with `exit-code`,
   `stdout`, `stderr`, and `artifacts/` (the declared files, copied out).
5. **Wake:** the tick wakes the parked run; `gather_results` delivers artifacts
   into `<ws>/.autoresearch/results/<name>/`, and the SAME session resumes
   (`resume_session_id` unchanged) with the results data-fenced + the author's
   note echoed back.
6. **Terminate:** the woken author either sleeps again (a fresh author-sleep
   park, counts advanced) or finishes → gate measures → candidate → publish.

## Success criteria

- The run parks `author-sleep` and the launch job actually runs on Slurm.
- The wake **resumes the same session** and the author sees the delivered
  results (context survived a REAL hibernation — the thing fakes cannot test).
- Artifacts copied out under apptainer and landed in `results/<name>/`.
- The run reaches a normal terminal (candidate/publish or an honest negative),
  not a stuck `waiting` loop or a `session-error`.

## The three things fakes cannot exercise (verify each)

- **Session recall across real hibernation** — step 5's `resume_session_id`.
- **Artifact copy-out under apptainer** — step 4's `artifacts/`, step 5's
  `results/<name>/`.
- **Walltime / self-deadline interplay on a live node** — the park deadline
  rides the longest launch; confirm the wake fires and does not race the job.

## Kill switch

The flag is per-run: to disable, just don't pass `--author-syscalls` (and keep
`AUTORESEARCH_AUTHOR_SYSCALLS` unset so the tick stays off). A parked run with no
wake ends with a named `session-error`, never a silent stuck loop.

## After green

Report back and the follow-ups unblock: build `submit` on the proven substrate
(retiring the panel-revision loop), remove the dead `AUTHOR_SLEEP_WAKE_READY`
constant, and migrate the env flag into contract-driven enablement.
