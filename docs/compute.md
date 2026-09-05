# Where it runs

It is built for your own compute, and the first-class home is a **Slurm
cluster**:

- **No daemon.** The whole system is a chain of short Slurm jobs on a cadence
  you set, each resubmitting the next. Nothing listens and nothing needs
  inbound SSH, so a cluster that requires 2FA is fine.
- **Every role is a job.** Author sessions, gate evals, experiment launches
  and sweeps, and wakes are each their own job, placed where you say: CPU
  roles on your CPU partition, evals and experiments on the GPU lanes you
  name.
- **Waiting costs nothing.** A run that is waiting on jobs parks with no
  process alive; its wake is queued behind those jobs and runs when they
  finish. Preemption and lost jobs heal on the next tick.
- **Jobs are jailed and metered.** Evals and launches run inside your
  Apptainer image, seeing only the checked-out tree and job-local scratch,
  with no credentials; GPU-hours are metered per attempt against the
  contract's budget.

You do not need a cluster to start. Level 1 (advisory PR reviews) is one
workflow file in your repo's CI. The climber's compute interface is small,
and the local backend runs the same job scripts as subprocesses on **one
machine**, so a workstation with a GPU can climb a cheap benchmark before you
point the loop at a cluster. Another backend — a cloud queue, a CI runner, a
hardware rig — plugs in by implementing that interface.
