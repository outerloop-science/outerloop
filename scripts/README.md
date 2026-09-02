# scripts/

Operational scripts, always committed.

| Script | Purpose |
| --- | --- |
| `setup_branch_protection.sh` | Apply/refresh branch protection on `main`. Solo-phase default: 0 approvals, checks required, admins enforced. Re-run with `1` once a second code owner joins. |
| `tick_chain.sbatch` | The self-resubmitting Slurm tick: deploys `main` into its checkout, syncs deps, runs one tick, schedules the next (`docs/design/architecture.md`). |
| `requeue_moved_successors.sh` | Cancel same-name pending jobs the site moved off the requested partition (a moved tick successor starves and blocks its twin); the chain runs it before topping up. |
| `sweep_git_locks.sh` | Remove stale `.git/*.lock` files from a checkout (older than N minutes, no live git process); the chain runs it before each deploy. |
| `install_codex.sh`, `install_hermes.sh` | Pin and install the non-claude author/judge backends on the tick host. |
