# The contract

One file in the target repo. The minimum:

```yaml
benchmarks:
  - name: my-benchmark
    command: uv run python -m mypkg.eval --json   # prints {"success_rate": 0.42}
    metric: success_rate
    direction: max
budgets:
  gpu_hours_per_run: 8
  runs_per_week: 10
scope:
  allowed: [src/]                                # the ONLY paths the agent may write
roadmap: docs/roadmap.md
```

The knobs that shape a climb, all optional:

| Knob | What it decides |
| --- | --- |
| `seed_env`, `min_delta` / `min_delta_rel` | Paired seeding for resampled evals, and the significance floor a delta must clear — calibrate it from seed variance, the gate enforces it |
| `eval_minutes`, `gpus` | Evals that need their own job (and GPUs) are dispatched to the cluster rather than run in the author's job |
| `baseline: paired \| cached` | Re-measure the base tree beside every candidate, or measure it once per base and run only candidates |
| `depth_k`, `sleep_k` | How many experiments an author may launch and how many times it may sleep for results |
| `max_active_attempts`, `attempt_cooldown_minutes` | Width: authors abreast on one target; pacing between attempts (0 for a hot loop) |
| `steward.allowed` | Paths a separate stewardship lane may maintain (the ruler, the harness) — never the solver |
| `merge: manual \| auto` | Whether a gate-and-panel-clean PR waits for a human or merges itself |

For GPU benchmarks `gpu_hours_per_run` is a real budget. An author's
experiment launches and its gate evals (baseline and candidate when paired)
draw on it, and the author sets how long its final eval may run
(`submit --minutes`). Compute is charged to the author that spends it; it
is never the metric. CPU benchmarks are not metered.

```bash
uv run python -m outerloop.contract_cli .outerloop.yaml   # validate before you push
```
