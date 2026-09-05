# scripts/

Operational scripts, always committed. Start the loop with `autoresearch start`:
it submits `tick_chain.sbatch` as the resident tick where `sbatch` exists and
runs the local loop elsewhere, reading placement from `~/.config/outerloop/.env`.

| Script | Purpose |
| --- | --- |
| `setup_branch_protection.sh` | Apply/refresh branch protection on `main`. Solo-phase default: 0 approvals, checks required, admins enforced. Re-run with `1` once a second code owner joins. |
| `tick_chain.sbatch` | The Slurm tick entry: per-cadence chain (deploy, one tick, schedule the next) or, with `AUTORESEARCH_RESIDENT=1`, the resident loop (`docs/design/resident-tick.md`). |
| `tick_deploy.sh` | The deploy step both modes source: stale-lock sweep, pull `main`, sync deps, read the operator's `.env` knobs, install backends. |
| `tick_resident.sh` | The resident loop: deploy → one tick under a timeout → sleep to the slot, one `afterany:self` successor, pause exits clean, shim changes resubmit the successor. |
| `requeue_moved_successors.sh` | Cancel pending same-name jobs that are no longer on the requested partition. The tick chain runs this before adding successors. |
| `sweep_git_locks.sh` | Remove stale `.git/*.lock` files from a checkout (older than N minutes, no live git process); the chain runs it before each deploy. |
| `install_codex.sh`, `install_hermes.sh` | Pin and install the non-claude author/judge backends on the tick host. |
