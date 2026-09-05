# Validating the author sleep/wake substrate (live)

> **VALIDATED 2026-08-25** on Torch (run `tsp-20260825-141345`): all six
> milestones green — the codex author launched + slept unprompted, the same
> session resumed with its launch's post-hibernation results, and the run
> reached an honest negative terminal. The `--author-syscalls` /
> `AUTORESEARCH_AUTHOR_SYSCALLS` arming described by the original runbook has
> since RETIRED: enablement is contract-driven (dispatch coords + a resumable
> backend + `depth_k > 0`; `depth_k: 0` is a benchmark's opt-out). The steps
> below are kept for re-validation after substrate changes.

## Arm it (one climb only)

Enablement is contract-driven; a one-off validation climb just needs:

- **Dispatch coords** (`--image` + account/partition): launches are jailed
  Slurm jobs, so without them nothing launches (and the tool is not offered).
- **A launch budget**: `depth_k` (default 10) caps launches; `depth_k: 0` in
  the target's `.outerloop.yaml` opts that benchmark out.

Then run one climb by hand (not via the tick):

```bash
source env.sh
uv run python -m autoresearch.attempt \
  --target <org/repo> --benchmark <a-cheap-benchmark> \
  --run-root <run-root> --image <image.sif> \
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

Per-benchmark: `depth_k: 0` in the target's `.outerloop.yaml`; per-deployment:
remove the dispatch coords. A parked run with no wake ends with a named
`session-error`, never a silent stuck loop.

## After green (done 2026-08-25)

The follow-ups this validation unblocked have landed: `submit` on the proven
substrate (the panel-revision loop retired with it), the dead
`AUTHOR_SLEEP_WAKE_READY` constant removed, and the env flag migrated into
contract-driven enablement.
