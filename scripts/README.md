# scripts/

Operational scripts, always committed.

| Script | Purpose |
| --- | --- |
| `setup_branch_protection.sh` | Apply/refresh branch protection on `main`. Solo-phase default: 0 approvals, checks required, admins enforced. Re-run with `1` once a second code owner joins. |

Planned (docs/roadmap.md): the Torch tick job (`scrontab` entry + self-resubmitting
fallback) and the orchestrator launcher.
